"""v1.7 QA 回归：QA 在 v1.6 覆盖面之外发现的 5 个问题（Q1–Q5）。

Q1 移动网络全断被误判成"会话失效" / Q2 平台转发失败导致会话泄漏 /
Q3 声明的最低 HA 版本不实 / Q4 `_wx_query` 自身的 ParseAnomalyError 被吞 /
Q5 注册表 `platform` 回落语义错误。

Q1/Q4 走真实 api 层（ccm fixture）；Q2/Q5 走 ha_stub 提供的 HA 桩。
"""

from __future__ import annotations

import importlib
import json

import pytest
from conftest import CCM_ROOT
from ha_stub import (
    ConfigEntry,
    ConfigEntryAuthFailed,
    FakeHass,
    load_integration,
)

DOMAIN = "cn_carrier_monitor"
PHONE = "13800000000"


@pytest.fixture(scope="module")
def ha():
    return load_integration()


# ------------------------------------------------------------------ 公共夹具


def _unicom_entry(entry_id: str = "abc123") -> ConfigEntry:
    return ConfigEntry(
        entry_id,
        {
            "carrier": "unicom",
            "phone": PHONE,
            "auth_mode": "cookie",
            "cookies": "duanlianjieabc=1; token_online=x",
        },
        title="中国联通 138****0000",
    )


def _coordinator_class(real_cls, *, result=None, exc: BaseException | None = None, sink=None):
    """生成"首刷成功/失败"的协调器子类，并把实例收进 sink 便于断言。"""

    class _Coordinator(real_cls):
        def __init__(self, hass, entry):
            super().__init__(hass, entry)
            if sink is not None:
                sink.append(self)

        async def _async_update_data(self):
            if exc is not None:
                raise exc
            return dict(result or {"balance": 1.0, "extra": {}})

    return _Coordinator


async def _boom_forward(*args, **kwargs):
    raise RuntimeError("platform boom")


def _mobile_client(ccm):
    mobile = ccm.modules["mobile"]
    client = mobile.MobileClient.__new__(mobile.MobileClient)
    client._data = {"phone": PHONE}
    client._mode = "cookie"
    client._password = ""
    client._cookie = "duanlianjieabc=1"
    client._device_id = mobile.device_identity(PHONE)
    client._session = None
    client._prov = "451"
    client._city = "0459"
    return client


async def _no_headers():
    return {}


def _event_log(ccm):  # noqa: ARG001 - 仅用于拿到已装载的 ccm 包路径
    return importlib.import_module("ccm.event_log")


# ---------------------------------------------------------------- Q2 会话泄漏


async def test_forward_failure_closes_session_normal_path(ha, monkeypatch):
    """Q2：正常路径 forward 抛异常 → 会话必须关闭，hass.data 不留旧 coordinator。"""
    hass = FakeHass()
    entry = _unicom_entry()
    created: list = []
    monkeypatch.setattr(
        ha, "CarrierDataUpdateCoordinator",
        _coordinator_class(ha.coordinator.CarrierDataUpdateCoordinator, sink=created),
    )
    monkeypatch.setattr(
        hass.config_entries, "async_forward_entry_setups", _boom_forward,
    )

    with pytest.raises(RuntimeError, match="platform boom"):
        await ha.async_setup_entry(hass, entry)

    coordinator = created[-1]
    assert coordinator.session.closed is True
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_forward_failure_closes_session_degraded_path(ha, monkeypatch):
    """Q2：降级路径 forward 抛异常 → 同样回收会话，且不引导用户去重新认证。"""
    hass = FakeHass()
    entry = _unicom_entry()
    hass.entity_registry.add(
        "sensor.unicom_balance", f"{entry.entry_id}_balance",
        config_entry_id=entry.entry_id,
    )
    created: list = []
    monkeypatch.setattr(
        ha, "CarrierDataUpdateCoordinator",
        _coordinator_class(
            ha.coordinator.CarrierDataUpdateCoordinator,
            exc=ConfigEntryAuthFailed("unicom_session_expired"),
            sink=created,
        ),
    )
    monkeypatch.setattr(
        hass.config_entries, "async_forward_entry_setups", _boom_forward,
    )

    with pytest.raises(RuntimeError, match="platform boom"):
        await ha.async_setup_entry(hass, entry)

    coordinator = created[-1]
    assert coordinator.session.closed is True
    assert entry.entry_id not in hass.data.get(DOMAIN, {})
    assert entry.reauth_started == 0  # 平台都没起来，别让用户白填密码


# ---------------------------------------------------------------- Q1/Q4 判类


async def test_wx_query_parse_anomaly_reaches_diagnostics(ccm):
    """Q4：`_wx_query` 自己抛的 ParseAnomalyError 必须进 anomalies（不被 CarrierError 吞）。"""
    base = ccm.modules["base"]
    client = _mobile_client(ccm)

    async def _drift(url, **kwargs):
        raise base.ParseAnomalyError("wx 响应顶层结构未知: []")

    client._prepare_uni_headers = _no_headers
    client._wx_query = _drift

    data = base.empty_data("mobile", PHONE)
    await client._wx_fetch(data)

    anomalies = data["extra"]["endpoint_anomalies"]
    assert len(anomalies) == 5  # plan / fee / margin / score / margin_query
    assert all("mobile 端点" in item for item in anomalies)
    assert "wx 响应顶层结构未知" in anomalies[0]
    assert "endpoint_failures" not in data["extra"]


async def test_all_endpoints_network_failure_is_not_auth_error(ccm):
    """Q1：五个端点全网络失败 → CarrierError(network)，绝不能报"请重新登录"。"""
    base = ccm.modules["base"]
    event_log = _event_log(ccm)
    client = _mobile_client(ccm)

    async def _net_fail(url, **kwargs):
        raise base.CarrierError("网络错误: Cannot connect to host wx.10086.cn:443")

    client._prepare_uni_headers = _no_headers
    client._wx_query = _net_fail

    with pytest.raises(base.CarrierError) as excinfo:
        await client.async_fetch()

    err = excinfo.value
    assert not isinstance(err, base.CarrierAuthError)
    assert "全部请求失败" in str(err)
    # 事件日志必须归 network（而不是 auth_expired）
    assert event_log.classify_exception(err) == event_log.CATEGORY_NETWORK


async def test_all_endpoints_drift_is_protocol_drift(ccm):
    """Q1：端点全部有响应但结构漂移 → ParseAnomalyError(protocol_drift)。"""
    base = ccm.modules["base"]
    event_log = _event_log(ccm)
    client = _mobile_client(ccm)

    async def _drift(url, **kwargs):
        raise base.ParseAnomalyError(f"wx 解密/解析失败: <{url}>")

    client._prepare_uni_headers = _no_headers
    client._wx_query = _drift

    with pytest.raises(base.ParseAnomalyError) as excinfo:
        await client.async_fetch()

    err = excinfo.value
    assert not isinstance(err, base.CarrierAuthError)
    assert "结构全部漂移" in str(err)
    assert event_log.classify_exception(err) == event_log.CATEGORY_DRIFT


async def test_no_evidence_still_reports_auth_expired(ccm):
    """Q1 兜底：既无失败也无漂移（端点直接返回空）→ 仍判会话失效。"""
    base = ccm.modules["base"]
    client = _mobile_client(ccm)

    async def _empty(url, **kwargs):
        return None

    client._prepare_uni_headers = _no_headers
    client._wx_query = _empty

    with pytest.raises(base.CarrierAuthError):
        await client.async_fetch()


async def test_partial_network_failure_keeps_successful_endpoint(ccm):
    """Q1 对照组：一个端点成功、其余网络失败 → 正常返回 + endpoint_failures 留痕。"""
    mobile = ccm.modules["mobile"]
    base = ccm.modules["base"]
    client = _mobile_client(ccm)

    async def _mixed(url, **kwargs):
        if url == mobile.MOBILE_FEE_URL_V2:
            return {"data": {"realFeeQryRsp": {"curFeeTotal": "45.67"}}}
        raise base.CarrierError("网络错误: timeout")

    client._prepare_uni_headers = _no_headers
    client._wx_query = _mixed

    data = await client.async_fetch()

    assert data["balance"] == 45.67
    assert len(data["extra"]["endpoint_failures"]) == 4
    assert all("网络错误" in item for item in data["extra"]["endpoint_failures"])
    assert "endpoint_anomalies" not in data["extra"]


# ---------------------------------------------------------------- Q6 非 200 状态码


def _http_status_client(ccm, status: int):
    """让所有 wx 端点都返回指定 HTTP 状态（不经过 curl_cffi）。"""
    client = _mobile_client(ccm)

    async def _no_cffi(*args, **kwargs):
        return None

    async def _request(*args, **kwargs):
        return status, "<html>gateway error</html>"

    client._cffi_wx = _no_cffi
    client._request = _request
    client._prepare_uni_headers = _no_headers
    return client


async def test_all_endpoints_http_502_is_not_auth_error(ccm):
    """Q6：五个端点全 502（网关故障）→ CarrierError(network)，不能报"请重新登录"。"""
    base = ccm.modules["base"]
    event_log = _event_log(ccm)
    client = _http_status_client(ccm, 502)

    with pytest.raises(base.CarrierError) as excinfo:
        await client.async_fetch()

    err = excinfo.value
    assert not isinstance(err, base.CarrierAuthError)
    assert "HTTP 502" in str(err)
    assert event_log.classify_exception(err) == event_log.CATEGORY_NETWORK


async def test_http_401_still_reports_auth_expired(ccm):
    """Q6 反向保护：401/403/3xx 跳登录仍必须判会话失效（否则修 Q6 会埋掉过期检测）。"""
    base = ccm.modules["base"]
    for status in (401, 403, 302):
        client = _http_status_client(ccm, status)
        with pytest.raises(base.CarrierAuthError):
            await client.async_fetch()


# ---------------------------------------------------------------- Q5 注册表回落


class _LegacyRegistryEntry:
    """没有 `domain` 字段的老版注册表条目：只有 entity_id + platform（集成域名）。"""

    def __init__(self, entity_id: str, unique_id: str, config_entry_id: str) -> None:
        self.entity_id = entity_id
        self.unique_id = unique_id
        self.platform = DOMAIN  # 集成域名，**不是**实体域
        self.config_entry_id = config_entry_id


class _BareCoordinator:
    """只暴露 sensor 平台读取字段的协调器桩（不建 aiohttp 会话）。"""

    def __init__(self, entry) -> None:
        self.entry = entry
        self.carrier = entry.data["carrier"]
        self.data: dict = {}
        self.last_update_success = True
        self._listeners: list = []

    def async_add_listener(self, update_callback, update_before_add=False):
        self._listeners.append(update_callback)

        def _remove() -> None:
            self._listeners.remove(update_callback)

        return _remove


async def test_registry_fallback_uses_entity_id_not_platform(ha):
    """Q5：`domain` 缺失时必须从 entity_id 推导实体域，用 platform 会全部 skip。"""
    hass = FakeHass()
    entry = _unicom_entry()
    hass.data[DOMAIN] = {entry.entry_id: _BareCoordinator(entry)}
    hass.entity_registry.entities["sensor.unicom_balance"] = _LegacyRegistryEntry(
        "sensor.unicom_balance", f"{entry.entry_id}_balance", entry.entry_id,
    )

    added = []
    await ha.sensor.async_setup_entry(hass, entry, added.extend)

    # session_state 始终创建（v1.7 起）；legacy 条目推导出 balance
    assert sorted(e.unique_id for e in added) == sorted(
        [f"{entry.entry_id}_balance", f"{entry.entry_id}_session_state"]
    )


# ---------------------------------------------------------------- Q3 版本声明


def test_hacs_minimum_homeassistant_matches_real_api():
    """Q3：声明的 min HA 必须 ≥ 2025.3（AddConfigEntryEntitiesCallback 引入版本）。"""
    data = json.loads((CCM_ROOT.parent.parent / "hacs.json").read_text(encoding="utf-8"))
    version = data["homeassistant"]
    parts = tuple(int(p) for p in version.split(".")[:2])
    assert parts >= (2025, 3), f"hacs.json 声明的 {version} 低于实际最低版本 2025.3"

    # 两个平台的模块顶层都直接导入该名字 → 低于 2025.3 会整体 ImportError
    for module in ("sensor.py", "button.py"):
        source = (CCM_ROOT / module).read_text(encoding="utf-8")
        assert "AddConfigEntryEntitiesCallback" in source
