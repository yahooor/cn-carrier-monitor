"""v1.6 稳定性修复的回归用例（对应线上 HA 2026.10.4 的 5 个问题）。

E1 实体集合稳定化 / E2 首次刷新失败不整条卸载 / E3 认证失败纳入熔断 /
E4 单端点异常收敛 / E5 掉线一次性通知 / E6 实体翻译键补全。

sensor / coordinator / __init__ 依赖 homeassistant，测试通过 ``ha_stub`` 提供
最小桩实现（CI 环境没有安装 HA），断言的是真实生产代码路径。
"""

from __future__ import annotations

import importlib
import json
import time

import pytest
from conftest import CCM_ROOT
from ha_stub import (
    ConfigEntry,
    ConfigEntryAuthFailed,
    FakeHass,
    UpdateFailed,
    load_integration,
)

DOMAIN = "cn_carrier_monitor"


# ------------------------------------------------------------------ 公共夹具


@pytest.fixture(scope="module")
def ha():
    return load_integration()


def _unicom_entry(entry_id: str = "abc123", *, auth_mode: str = "cookie") -> ConfigEntry:
    return ConfigEntry(
        entry_id,
        {
            "carrier": "unicom",
            "phone": "13800000000",
            "auth_mode": auth_mode,
            "cookies": "duanlianjieabc=1; token_online=x",
        },
        title="中国联通 138****0000",
    )


class _StubCoordinator:
    """sensor 平台用的极简协调器（不建会话，只暴露 sensor 读的字段）。"""

    def __init__(self, hass, entry, data=None, *, success: bool = True) -> None:
        self.hass = hass
        self.entry = entry
        self.carrier = entry.data["carrier"]
        self.data = data
        self.last_update_success = success
        self._listeners = []
        # session_state 实体会读这些（真实 coordinator 上是 property）
        self._fail_streak = 0
        self._auth_fail_streak = 0

    @property
    def fail_streak(self) -> int:
        return self._fail_streak

    @property
    def auth_fail_streak(self) -> int:
        return self._auth_fail_streak

    @property
    def session_state(self) -> str:
        if self._fail_streak >= 5:
            return "circuit_open"
        if self._auth_fail_streak > 0:
            return "degraded" if self.data else "needs_human"
        return "healthy"

    def async_add_listener(self, update_callback, update_before_add=False):
        self._listeners.append(update_callback)

        def _remove() -> None:
            self._listeners.remove(update_callback)

        return _remove

    def notify(self) -> None:
        for listener in list(self._listeners):
            listener()


class _StubClient:
    """coordinator 用的假客户端：要么返回数据，要么抛指定异常。"""

    def __init__(self, *, result=None, error: BaseException | None = None) -> None:
        self.result = result or {}
        self.error = error
        self.calls = 0

    async def async_fetch(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return dict(self.result)

    def export_credentials(self) -> dict:
        return {}


def _sensor_keys(entities, entry) -> list[str]:
    prefix = f"{entry.entry_id}_"
    return [e.unique_id[len(prefix):] for e in entities if e.unique_id.startswith(prefix)]


# ---------------------------------------------------------------- E1 实体集合稳定化


async def test_sensor_entities_rebuilt_from_registry_when_data_empty(ha):
    """E1：coordinator.data 为空时按实体注册表反推 key，实体不得消失。"""
    hass = FakeHass()
    entry = _unicom_entry()
    coordinator = _StubCoordinator(hass, entry, data=None, success=False)
    hass.data[DOMAIN] = {entry.entry_id: coordinator}
    hass.entity_registry.add(
        "sensor.unicom_balance", f"{entry.entry_id}_balance",
        config_entry_id=entry.entry_id,
    )
    hass.entity_registry.add(
        "sensor.unicom_plan_name", f"{entry.entry_id}_plan_name",
        config_entry_id=entry.entry_id,
    )

    added = []
    await ha.sensor.async_setup_entry(hass, entry, added.extend)

    assert sorted(_sensor_keys(added, entry)) == ["balance", "plan_name", "session_state"]
    # 数据缺失 → 数据实体 native_value 为 None（显示 unavailable），但实体本身存在
    by_key = {e.unique_id[len(entry.entry_id) + 1:]: e for e in added}
    assert all(by_key[k].native_value is None for k in ("balance", "plan_name"))
    assert all(by_key[k].available is False for k in ("balance", "plan_name"))
    # session_state 是会话健康度，掉线时恰恰要可读：healthy + available
    assert by_key["session_state"].native_value == "healthy"
    assert by_key["session_state"].available is True


async def test_sensor_registry_rebuild_ignores_dirty_and_foreign_entries(ha):
    """E1：注册表脏数据（未知后缀 / 别的条目 / 非 sensor 平台）必须跳过。"""
    hass = FakeHass()
    entry = _unicom_entry()
    coordinator = _StubCoordinator(hass, entry, data={})
    hass.data[DOMAIN] = {entry.entry_id: coordinator}
    hass.entity_registry.add(
        "sensor.unicom_balance", f"{entry.entry_id}_balance",
        config_entry_id=entry.entry_id,
    )
    # 历史版本留下的未知后缀（ENTITY_FACTORY 里没有）
    hass.entity_registry.add(
        "sensor.unicom_legacy_field", f"{entry.entry_id}_legacy_field",
        config_entry_id=entry.entry_id,
    )
    # 其它条目
    hass.entity_registry.add(
        "sensor.other_balance", "other_entry_balance", config_entry_id="other_entry",
    )
    # 同条目的 button 实体
    hass.entity_registry.add(
        "button.unicom_refresh", f"{entry.entry_id}_refresh",
        domain="button", config_entry_id=entry.entry_id,
    )

    added = []
    await ha.sensor.async_setup_entry(hass, entry, added.extend)

    assert sorted(_sensor_keys(added, entry)) == ["balance", "session_state"]


async def test_sensor_first_install_stays_data_driven(ha):
    """E1：首次安装（注册表为空）不凭空造实体，保持原有的数据驱动语义。"""
    hass = FakeHass()
    entry = _unicom_entry()
    coordinator = _StubCoordinator(
        hass, entry, data={"balance": 1.0, "flow_general": {"used": 1.0, "total": 10.0}},
    )
    hass.data[DOMAIN] = {entry.entry_id: coordinator}

    added = []
    await ha.sensor.async_setup_entry(hass, entry, added.extend)

    assert sorted(_sensor_keys(added, entry)) == ["balance", "flow_general_used", "session_state"]


async def test_sensor_keeps_registry_keys_when_data_is_partial(ha):
    """E1：数据只回来一部分（单端点漂移）时，已有实体同样不能消失。"""
    hass = FakeHass()
    entry = _unicom_entry()
    coordinator = _StubCoordinator(hass, entry, data={"balance": 12.5})
    hass.data[DOMAIN] = {entry.entry_id: coordinator}
    for key in ("balance", "plan_name", "flow_total_used"):
        hass.entity_registry.add(
            f"sensor.unicom_{key}", f"{entry.entry_id}_{key}",
            config_entry_id=entry.entry_id,
        )

    added = []
    await ha.sensor.async_setup_entry(hass, entry, added.extend)

    assert sorted(_sensor_keys(added, entry)) == [
        "balance", "flow_total_used", "plan_name", "session_state",
    ]
    assert len(added) == 4


async def test_sensor_listener_adds_new_keys_after_recovery(ha):
    """E1：恢复数据后 listener 补齐新字段，已建实体不重复。"""
    hass = FakeHass()
    entry = _unicom_entry()
    coordinator = _StubCoordinator(hass, entry, data={})
    hass.data[DOMAIN] = {entry.entry_id: coordinator}
    hass.entity_registry.add(
        "sensor.unicom_balance", f"{entry.entry_id}_balance",
        config_entry_id=entry.entry_id,
    )

    added = []
    await ha.sensor.async_setup_entry(hass, entry, added.extend)
    assert sorted(_sensor_keys(added, entry)) == ["balance", "session_state"]

    coordinator.data = {"balance": 12.5, "month_cost": 8.0}
    coordinator.last_update_success = True
    coordinator.notify()

    assert sorted(_sensor_keys(added, entry)) == [
        "balance", "month_cost", "session_state",
    ]
    assert len(added) == 3  # balance / session_state 没有被重复创建


# ---------------------------------------------------------------- E2 不整条卸载


def _failing_coordinator_class(real_cls, exc: BaseException, sink: list):
    """让 _async_update_data 直接抛异常，走真实的 first_refresh 异常转换流程。"""

    class _FailingCoordinator(real_cls):
        def __init__(self, hass, entry):
            super().__init__(hass, entry)
            sink.append(self)

        async def _async_update_data(self):
            raise exc

    return _FailingCoordinator


async def test_setup_entry_degrades_instead_of_unloading_when_entities_exist(ha, monkeypatch):
    """E2：已有实体的条目首次刷新认证失败 → 保留加载 + 主动重新认证。"""
    hass = FakeHass()
    entry = _unicom_entry()
    hass.entity_registry.add(
        "sensor.unicom_balance", f"{entry.entry_id}_balance",
        config_entry_id=entry.entry_id,
    )
    created: list = []
    monkeypatch.setattr(
        ha, "CarrierDataUpdateCoordinator",
        _failing_coordinator_class(
            ha.coordinator.CarrierDataUpdateCoordinator,
            ConfigEntryAuthFailed("unicom_session_expired"), created,
        ),
    )

    ok = await ha.async_setup_entry(hass, entry)

    assert ok is True
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.last_update_success is False
    assert hass.config_entries.forwarded == [(entry.entry_id, ha.const.PLATFORMS)]
    assert entry.reauth_started == 1
    assert len(entry.update_listeners) == 1
    assert coordinator.session.closed is False
    await coordinator.async_shutdown()
    assert coordinator.session.closed is True


async def test_setup_entry_degrades_on_transient_failure_too(ha, monkeypatch):
    """E2：ConfigEntryNotReady（暂时失败）同样不能卸载已有实体的条目，且不得弹 reauth。"""
    hass = FakeHass()
    entry = _unicom_entry()
    hass.entity_registry.add(
        "sensor.unicom_balance", f"{entry.entry_id}_balance",
        config_entry_id=entry.entry_id,
    )
    created: list = []
    monkeypatch.setattr(
        ha, "CarrierDataUpdateCoordinator",
        _failing_coordinator_class(
            ha.coordinator.CarrierDataUpdateCoordinator,
            UpdateFailed("网络错误: timeout"), created,
        ),
    )

    assert await ha.async_setup_entry(hass, entry) is True
    # 降级加载：coordinator 已就位 + 平台已转发
    assert entry.entry_id in hass.data[DOMAIN]
    assert hass.config_entries.forwarded == [(entry.entry_id, ha.const.PLATFORMS)]
    assert len(entry.update_listeners) == 1
    # 网络问题不该要求用户重新登录
    assert entry.reauth_started == 0
    assert hass.task_qualnames == []
    await hass.data[DOMAIN][entry.entry_id].async_shutdown()


async def test_setup_entry_does_not_start_reauth_on_not_ready(ha, monkeypatch):
    """E2：NotReady 降级后明确不进入重新认证流程（避免误导用户填密码）。"""
    hass = FakeHass()
    entry = _unicom_entry()
    hass.entity_registry.add(
        "sensor.unicom_balance", f"{entry.entry_id}_balance",
        config_entry_id=entry.entry_id,
    )
    monkeypatch.setattr(ConfigEntry, "async_start_reauth", None, raising=False)
    created: list = []
    monkeypatch.setattr(
        ha, "CarrierDataUpdateCoordinator",
        _failing_coordinator_class(
            ha.coordinator.CarrierDataUpdateCoordinator,
            UpdateFailed("网络错误: Connection reset"), created,
        ),
    )

    assert await ha.async_setup_entry(hass, entry) is True
    # 连 flow.async_init 兜底都不该被触发
    assert hass.task_qualnames == []
    assert hass.config_entries.flow.init_calls == []
    await hass.data[DOMAIN][entry.entry_id].async_shutdown()


async def test_setup_entry_raises_for_brand_new_entry(ha, monkeypatch):
    """E2：注册表里没有任何实体的全新条目保持原行为（关会话 + 上抛）。"""
    hass = FakeHass()
    entry = _unicom_entry()
    created: list = []
    monkeypatch.setattr(
        ha, "CarrierDataUpdateCoordinator",
        _failing_coordinator_class(
            ha.coordinator.CarrierDataUpdateCoordinator,
            ConfigEntryAuthFailed("unicom_session_expired"), created,
        ),
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await ha.async_setup_entry(hass, entry)

    assert entry.entry_id not in hass.data.get(DOMAIN, {})
    assert hass.config_entries.forwarded == []
    assert entry.reauth_started == 0
    assert created and created[0].session.closed is True  # 会话必须关掉


async def test_degraded_setup_keeps_entities_in_state_machine(ha, monkeypatch):
    """E1+E2 合体（线上场景）：首次刷新认证失败后实体仍然建出来，只是 unavailable。"""
    hass = FakeHass()
    entry = _unicom_entry()
    for key in ("balance", "plan_name"):
        hass.entity_registry.add(
            f"sensor.unicom_{key}", f"{entry.entry_id}_{key}",
            config_entry_id=entry.entry_id,
        )
    created: list = []
    monkeypatch.setattr(
        ha, "CarrierDataUpdateCoordinator",
        _failing_coordinator_class(
            ha.coordinator.CarrierDataUpdateCoordinator,
            ConfigEntryAuthFailed("unicom_session_expired"), created,
        ),
    )

    assert await ha.async_setup_entry(hass, entry) is True
    coordinator = hass.data[DOMAIN][entry.entry_id]

    added = []
    await ha.sensor.async_setup_entry(hass, entry, added.extend)
    assert sorted(_sensor_keys(added, entry)) == ["balance", "plan_name", "session_state"]
    # session_state 实体永远可读（这正是掉线时最需要的信息）
    unavailable = [e for e in added if e.unique_id.endswith(("_balance", "_plan_name"))]
    assert all(entity.available is False for entity in unavailable)

    await coordinator.async_shutdown()


async def test_setup_entry_falls_back_to_flow_init_without_async_start_reauth(ha, monkeypatch):
    """E2：旧版 HA 无 entry.async_start_reauth → 退回 flow.async_init(reauth)。"""
    hass = FakeHass()
    entry = _unicom_entry()
    hass.entity_registry.add(
        "sensor.unicom_balance", f"{entry.entry_id}_balance",
        config_entry_id=entry.entry_id,
    )
    monkeypatch.setattr(ConfigEntry, "async_start_reauth", None, raising=False)
    created: list = []
    monkeypatch.setattr(
        ha, "CarrierDataUpdateCoordinator",
        _failing_coordinator_class(
            ha.coordinator.CarrierDataUpdateCoordinator,
            ConfigEntryAuthFailed("unicom_session_expired"), created,
        ),
    )

    assert await ha.async_setup_entry(hass, entry) is True
    # 走的是 flow.async_init 兜底（协程不执行，按 qualname 断言调度目标）
    assert hass.task_qualnames == ["FakeConfigFlowManager.async_init"]
    assert entry.reauth_started == 0
    await hass.data[DOMAIN][entry.entry_id].async_shutdown()


# ---------------------------------------------------------------- E3 熔断


def _coordinator(ha, hass, entry):
    return ha.coordinator.CarrierDataUpdateCoordinator(hass, entry)


async def test_auth_failure_counts_toward_circuit_breaker(ha):
    """E3：CarrierAuthError 必须递增 _fail_streak / _last_fail_at。"""
    hass = FakeHass()
    entry = _unicom_entry()
    coordinator = _coordinator(ha, hass, entry)
    coordinator.client = _StubClient(error=ha.api.CarrierAuthError("unicom_session_expired"))
    coordinator.data = None

    try:
        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()
        assert coordinator._fail_streak == 1
        assert coordinator._last_fail_at > 0
        assert coordinator._auth_fail_streak == 1
    finally:
        await coordinator.async_shutdown()


async def test_auth_failure_opens_circuit_and_stops_polling_upstream(ha):
    """E3：连续 5 次认证失败后熔断，不再打运营商接口。"""
    hass = FakeHass()
    entry = _unicom_entry()
    coordinator = _coordinator(ha, hass, entry)
    coordinator.client = _StubClient(error=ha.api.CarrierAuthError("unicom_session_expired"))
    coordinator.data = None

    try:
        for _ in range(coordinator.FAILURE_THRESHOLD):
            with pytest.raises(ConfigEntryAuthFailed):
                await coordinator._async_update_data()
        assert coordinator._fail_streak == coordinator.FAILURE_THRESHOLD

        calls_before = coordinator.client.calls
        degraded = await coordinator._async_update_data()  # 熔断窗口内
        assert degraded["extra"]["circuit_open"] is True
        assert degraded["extra"]["fail_streak"] == coordinator.FAILURE_THRESHOLD
        assert coordinator.client.calls == calls_before  # 没有真的发请求
    finally:
        await coordinator.async_shutdown()


async def test_auth_failure_skips_relogin_sms_when_circuit_open(ha):
    """E3：熔断期内不再调度自动发码（线上 48 次空转重试的根因）。"""
    hass = FakeHass()
    entry = _unicom_entry(auth_mode="sms")
    coordinator = _coordinator(ha, hass, entry)
    coordinator.client = _StubClient(error=ha.api.CarrierAuthError("unicom_session_expired"))
    coordinator.data = None

    try:
        assert coordinator.supports_sms_relogin is True

        # 未熔断：仍然调度发码
        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()
        assert "_async_safe_send_relogin_sms" in hass.tasks

        # 熔断（streak 到阈值）：不再调度
        hass.tasks.clear()
        coordinator._fail_streak = coordinator.FAILURE_THRESHOLD - 1
        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()
        assert coordinator._fail_streak == coordinator.FAILURE_THRESHOLD
        assert "_async_safe_send_relogin_sms" not in hass.tasks
    finally:
        await coordinator.async_shutdown()


async def test_circuit_open_keeps_login_expired_marker(ha):
    """E3：熔断分支要保留 login_expired，实体属性才能区分掉线与无数据。"""
    hass = FakeHass()
    entry = _unicom_entry()
    coordinator = _coordinator(ha, hass, entry)
    coordinator.client = _StubClient(error=ha.api.CarrierAuthError("unicom_session_expired"))
    coordinator.data = {"balance": 9.9, "extra": {}}
    coordinator._auth_fail_streak = 3
    coordinator._fail_streak = coordinator.FAILURE_THRESHOLD
    coordinator._last_fail_at = time.time()
    coordinator.last_error = {"message": "unicom_session_expired"}

    try:
        degraded = await coordinator._async_update_data()
        assert degraded["balance"] == 9.9
        assert degraded["extra"]["circuit_open"] is True
        assert degraded["extra"]["login_expired"] is True
        assert degraded["extra"]["login_error"] == "unicom_session_expired"
    finally:
        await coordinator.async_shutdown()


async def test_degraded_auth_failure_keeps_last_data(ha):
    """E3：有历史数据时返回旧值 + login_expired（实体不消失）。"""
    hass = FakeHass()
    entry = _unicom_entry()
    coordinator = _coordinator(ha, hass, entry)
    coordinator.client = _StubClient(error=ha.api.CarrierAuthError("unicom_session_expired"))
    coordinator.data = {"balance": 5.0, "extra": {}}

    try:
        degraded = await coordinator._async_update_data()
        assert degraded["balance"] == 5.0
        assert degraded["extra"]["login_expired"] is True
        assert degraded["extra"]["fail_streak"] == 1
        assert "circuit_open" not in degraded["extra"]
    finally:
        await coordinator.async_shutdown()


# ---------------------------------------------------------------- E5 掉线通知


async def test_needs_human_notification_sent_once_and_dismissed_on_recovery(ha):
    """E5：掉线发一次性通知（同 entry 只发一次），恢复后自动 dismiss。"""
    hass = FakeHass()
    entry = _unicom_entry()
    coordinator = _coordinator(ha, hass, entry)
    coordinator.client = _StubClient(error=ha.api.CarrierAuthError("unicom_session_expired"))
    coordinator.data = None
    store = hass.notification_store
    notification_id = f"{DOMAIN}_reauth_{entry.entry_id}"

    try:
        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()
        assert store.create_calls == 1
        assert notification_id in store.created
        message = store.created[notification_id]["message"]
        assert "unicom_session_expired" in message
        assert "inject_token" in message  # cookie 模式给出可执行的处理动作

        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()
        assert store.create_calls == 1  # 不刷屏

        coordinator.client = _StubClient(result={"balance": 12.5, "extra": {}})
        data = await coordinator._async_update_data()
        assert data["balance"] == 12.5
        assert store.dismiss_calls(notification_id) == 1
        assert notification_id not in store.created
        assert coordinator._needs_human_notified is False
    finally:
        await coordinator.async_shutdown()


async def test_sms_mode_notification_mentions_webhook(ha):
    """E5：短信模式的通知指向转发器 webhook，而不是 cookie 导入。"""
    hass = FakeHass()
    entry = _unicom_entry(auth_mode="sms")
    coordinator = _coordinator(ha, hass, entry)
    coordinator.client = _StubClient(error=ha.api.CarrierAuthError("unicom_session_expired"))
    coordinator.data = None

    try:
        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()
        message = hass.notification_store.created[
            f"{DOMAIN}_reauth_{entry.entry_id}"
        ]["message"]
        assert "/api/webhook/cn_carrier_monitor_sms" in message
    finally:
        await coordinator.async_shutdown()


async def test_no_notification_while_stale_data_still_served(ha):
    """E5：仍有历史数据可展示（未熔断）时不打扰用户。"""
    hass = FakeHass()
    entry = _unicom_entry()
    coordinator = _coordinator(ha, hass, entry)
    coordinator.client = _StubClient(error=ha.api.CarrierAuthError("unicom_session_expired"))
    coordinator.data = {"balance": 9.9, "extra": {}}

    try:
        degraded = await coordinator._async_update_data()
        assert degraded["extra"]["login_expired"] is True
        assert hass.notification_store.create_calls == 0
    finally:
        await coordinator.async_shutdown()


async def test_notification_failure_does_not_break_update(ha, monkeypatch):
    """E5：通知组件不可用时更新流程照常（降级为 debug 日志）。"""
    hass = FakeHass()
    entry = _unicom_entry()
    coordinator = _coordinator(ha, hass, entry)
    coordinator.client = _StubClient(error=ha.api.CarrierAuthError("unicom_session_expired"))
    coordinator.data = None
    monkeypatch.setattr(
        ha.notification_module, "async_create",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("notification boom")),
    )

    try:
        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()
        assert coordinator._needs_human_notified is False  # 没发出去就允许下次重试
    finally:
        await coordinator.async_shutdown()


# ---------------------------------------------------------------- E4 单端点收敛


def _mobile_client(ccm):
    mobile = ccm.modules["mobile"]
    client = mobile.MobileClient.__new__(mobile.MobileClient)
    client._data = {"phone": "13800000000"}
    client._mode = "cookie"
    client._password = ""
    client._cookie = "duanlianjieabc=1"
    client._device_id = mobile.device_identity("13800000000")
    client._session = None
    client._prov = "451"
    client._city = "0459"
    return client


def test_parse_anomaly_carries_endpoint_and_classifies_as_drift(ccm):
    """E4：解析崩溃要转成带端点名的 ParseAnomalyError（protocol_drift）。"""
    mobile = ccm.modules["mobile"]
    base = ccm.modules["base"]
    event_log = importlib.import_module("ccm.event_log")

    anomaly = mobile._as_parse_anomaly(
        "https://wx.10086.cn/website/fareBalance",
        AttributeError("'NoneType' object has no attribute 'get'"),
    )
    assert isinstance(anomaly, base.ParseAnomalyError)
    text = str(anomaly)
    assert "mobile 端点 https://wx.10086.cn/website/fareBalance" in text
    assert "AttributeError" in text
    assert "'NoneType' object has no attribute 'get'" in text
    assert anomaly.__cause__ is None  # raise ... from None：不带原始栈
    assert event_log.classify_exception(anomaly) == event_log.CATEGORY_DRIFT


async def test_wx_fetch_isolates_endpoint_parse_crashes(ccm):
    """E4：单个端点结构漂移只丢该端点字段，其它端点照常写入。"""
    mobile = ccm.modules["mobile"]
    base = ccm.modules["base"]
    client = _mobile_client(ccm)

    async def _no_headers():
        return {}

    async def _fake_query(url, **kwargs):
        if url == mobile.MOBILE_PLAN_URL:
            # object 是字符串 → 旧代码 "x".get(...) 抛 AttributeError
            return {"object": "unexpected-string"}
        if url == mobile.MOBILE_FEE_URL_V2:
            # data 是字符串 → 同样炸 .get 链
            return {"data": "not-a-dict"}
        if url == mobile.MOBILE_SCORE_URL:
            raise AttributeError("'NoneType' object has no attribute 'get'")
        if url == mobile.MOBILE_MARGIN_QRY_URL_V2:
            return {"data": {"resultData": {"customerAssignment": "湖南张家界"}}}
        return None

    client._prepare_uni_headers = _no_headers
    client._wx_query = _fake_query

    data = base.empty_data("mobile", "13800000000")
    await client._wx_fetch(data)

    # 结构正常的端点照常生效
    assert data["location"] == "湖南张家界"
    assert data["phone_region"] == "湖南张家界"
    assert data["account_status"] == "正常在网"
    # 漂移的端点只留下可定位的痕迹
    anomalies = data["extra"]["endpoint_anomalies"]
    assert len(anomalies) == 3
    assert all("mobile 端点" in item for item in anomalies)
    assert any(mobile.MOBILE_PLAN_URL in item for item in anomalies)
    assert any(mobile.MOBILE_FEE_URL_V2 in item for item in anomalies)
    assert any("AttributeError" in item for item in anomalies)
    assert data["balance"] is None


async def test_wx_fetch_does_not_mask_auth_expiry(ccm):
    """E4：收敛只针对结构异常；认证失效仍必须上抛（否则永远不重新认证）。"""
    mobile = ccm.modules["mobile"]
    base = ccm.modules["base"]
    client = _mobile_client(ccm)

    async def _no_headers():
        return {}

    async def _fake_query(url, **kwargs):
        raise mobile.CarrierAuthError("mobile_session_expired")

    client._prepare_uni_headers = _no_headers
    client._wx_query = _fake_query

    data = base.empty_data("mobile", "13800000000")
    with pytest.raises(mobile.CarrierAuthError):
        await client._wx_fetch(data)


async def test_wx_fetch_success_path_has_no_anomalies(ccm):
    """E4：正常响应不产生 endpoint_anomalies（不引入噪音）。"""
    mobile = ccm.modules["mobile"]
    base = ccm.modules["base"]
    client = _mobile_client(ccm)

    async def _no_headers():
        return {}

    async def _fake_query(url, **kwargs):
        if url == mobile.MOBILE_FEE_URL_V2:
            return {"data": {"realFeeQryRsp": {"curFeeTotal": "45.67", "realFee": "8.00"}}}
        return None

    client._prepare_uni_headers = _no_headers
    client._wx_query = _fake_query

    data = base.empty_data("mobile", "13800000000")
    await client._wx_fetch(data)

    assert data["balance"] == 45.67
    assert data["month_cost"] == 8.0
    assert "endpoint_anomalies" not in data["extra"]


# ---------------------------------------------------------------- E6 翻译键补全

LOCALES = ("strings.json", "translations/zh-Hans.json", "translations/en.json")


def test_every_entity_key_has_translation_in_all_locales(ha):
    """E6：ENTITY_FACTORY 里每个 sensor key 都必须在三份翻译里存在。

    缺 translation_key 时 HA 会 fallback 到设备名，导致同一账号多个实体同名
    （线上：`sensor.zhong_guo_dian_xin_199_0126 = 0.0 min` 直接用了设备名）。
    """
    factory_keys = set(ha.sensor.ENTITY_FACTORY)
    assert factory_keys, "ENTITY_FACTORY 为空，检查 sensor 模块是否加载"

    for name in LOCALES:
        sensor = json.loads((CCM_ROOT / name).read_text(encoding="utf-8"))["entity"]["sensor"]
        missing = sorted(factory_keys - set(sensor))
        assert not missing, f"{name} 缺少 entity.sensor 翻译键: {missing}"
        empty = sorted(key for key, value in sensor.items() if not value.get("name"))
        assert not empty, f"{name} 存在空 name: {empty}"


def test_locale_files_share_the_same_entity_keys():
    """E6：strings.json 与 translations/*.json 的键集合必须完全一致（hassfest 校验）。"""
    key_sets = {}
    for name in LOCALES:
        sensor = json.loads((CCM_ROOT / name).read_text(encoding="utf-8"))["entity"]["sensor"]
        key_sets[name] = set(sensor)
    reference_name = LOCALES[0]
    reference = key_sets[reference_name]
    for name in LOCALES[1:]:
        assert key_sets[name] == reference, (
            f"{name} 与 {reference_name} 键集合不一致: "
            f"多={sorted(key_sets[name] - reference)} 少={sorted(reference - key_sets[name])}"
        )
