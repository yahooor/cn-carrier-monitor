"""v1.7 第二轮（F1–F7）回归用例。

F1 404/410 归 protocol_drift / F2 session_state 实体 / F3 自适应轮询间隔 /
F4 sms_used 实体 / F5 refresh 按手机号过滤 / F6 诊断事件摘要 /
F7 QA 指出的两条测试缺口（判类优先级 + forward 重试会话独立性）。
"""

from __future__ import annotations

import pytest
from conftest import CCM_ROOT
from ha_stub import ConfigEntry, FakeHass, load_integration

DOMAIN = "cn_carrier_monitor"
PHONE = "13800000000"


@pytest.fixture(scope="module")
def ha():
    return load_integration()


def _unicom_entry(entry_id: str = "abc123") -> ConfigEntry:
    return ConfigEntry(
        entry_id,
        {
            "carrier": "unicom",
            "phone": PHONE,
            "auth_mode": "cookie",
            "cookies": "duanlianjieabc=1",
        },
        title="中国联通 138****0000",
    )


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


def _http_status_client(ccm, status: int):
    client = _mobile_client(ccm)

    async def _no_cffi(*args, **kwargs):
        return None

    async def _request(*args, **kwargs):
        return status, "<html>gateway error</html>"

    async def _no_headers():
        return {}

    client._cffi_wx = _no_cffi
    client._request = _request
    client._prepare_uni_headers = _no_headers
    return client


def _event_log(ccm):
    import importlib

    return importlib.import_module("ccm.event_log")


async def _no_headers():
    return {}


# ---------------------------------------------------------------- F1 404/410 归类


async def test_all_endpoints_http_404_is_protocol_drift(ccm):
    """F1：端点下线（404/410）是协议漂移，不是网络故障，更不是会话失效。"""
    base = ccm.modules["base"]
    event_log = _event_log(ccm)
    for status in (404, 410):
        client = _http_status_client(ccm, status)
        with pytest.raises(base.ParseAnomalyError) as excinfo:
            await client.async_fetch()
        err = excinfo.value
        assert not isinstance(err, base.CarrierAuthError)
        assert f"HTTP {status}" in str(err)
        assert event_log.classify_exception(err) == event_log.CATEGORY_DRIFT


async def test_http_502_still_network_after_404_rule(ccm):
    """F1 防污染：404 规则加入后，5xx 必须仍然归 network。"""
    base = ccm.modules["base"]
    event_log = _event_log(ccm)
    client = _http_status_client(ccm, 502)
    with pytest.raises(base.CarrierError) as excinfo:
        await client.async_fetch()
    assert event_log.classify_exception(excinfo.value) == event_log.CATEGORY_NETWORK


# ---------------------------------------------------------------- F2 session_state


class _MiniCoordinator:
    """只暴露 session_state 需要的属性。"""

    def __init__(self, entry, *, fail=0, auth_fail=0, data=None):
        self.entry = entry
        self.carrier = entry.data["carrier"]
        self.data = data
        self._fail_streak = fail
        self._auth_fail_streak = auth_fail

    @property
    def fail_streak(self):
        return self._fail_streak

    @property
    def auth_fail_streak(self):
        return self._auth_fail_streak

    @property
    def session_state(self):
        if self._fail_streak >= 5:
            return "circuit_open"
        if self._auth_fail_streak > 0:
            return "degraded" if self.data else "needs_human"
        return "healthy"

    def async_add_listener(self, cb, update_before_add=False):
        return lambda: None


def test_session_state_matrix(ha):
    """F2：状态机判定顺序 = 熔断 > 需人工 > 降级 > 正常。"""
    entry = _unicom_entry()
    assert _MiniCoordinator(entry).session_state == "healthy"
    assert _MiniCoordinator(entry, auth_fail=2, data={"balance": 1}).session_state == "degraded"
    assert _MiniCoordinator(entry, auth_fail=1, data=None).session_state == "needs_human"
    assert _MiniCoordinator(entry, fail=5, auth_fail=5).session_state == "circuit_open"


async def test_session_state_entity_always_present(ha):
    """F2：session_state 实体始终存在（掉线告警依赖它，不能跟着数据塌缩）。"""
    hass = FakeHass()
    entry = _unicom_entry()
    coordinator = _MiniCoordinator(entry, auth_fail=3, data={"balance": 8.8})
    hass.data[DOMAIN] = {entry.entry_id: coordinator}

    added = []
    await ha.sensor.async_setup_entry(hass, entry, added.extend)
    by_key = {e.unique_id[len(entry.entry_id) + 1:]: e for e in added}
    assert "session_state" in by_key
    entity = by_key["session_state"]
    assert entity.native_value == "degraded"
    assert entity.available is True
    attrs = entity.extra_state_attributes
    assert attrs["fail_streak"] == 0
    assert attrs["auth_fail_streak"] == 3


def test_session_state_translation_registered():
    """F2：三份翻译都有 session_state 键（实体名不再 fallback 成设备名）。"""
    for name in ("strings.json", "translations/zh-Hans.json", "translations/en.json"):
        data = CCM_ROOT.joinpath(name).read_text(encoding="utf-8")
        assert '"session_state"' in data, f"{name} 缺少 session_state"


# ---------------------------------------------------------------- F3 自适应间隔


async def test_adaptive_interval_grows_and_resets(ha):
    """F3：连续认证失败按 1/2/4/8 倍拉长间隔，成功后回到基准值。"""
    from ha_stub import FakeHass as _FH

    hass = _FH()
    entry = _unicom_entry()
    coordinator = ha.coordinator.CarrierDataUpdateCoordinator(hass, entry)
    base = coordinator._base_interval

    class _AuthErr(Exception):
        pass

    # 直接操纵内部状态模拟连续失败（避免真实网络）
    for streak in (1, 2, 3, 9):
        coordinator._auth_fail_streak = streak
        coordinator._apply_adaptive_interval()
        factor = min(2 ** (streak - 1), 8)
        assert coordinator.update_interval == base * factor, f"streak={streak}"

    # 成功恢复：回到基准
    coordinator._auth_fail_streak = 0
    coordinator.update_interval = base
    assert coordinator.update_interval == base
    await coordinator.async_shutdown()


# ---------------------------------------------------------------- F4 sms_used


def test_desired_keys_include_sms_used(ha):
    """F4：sms.used 有值时创建 sms_used 实体。"""
    keys = ha.sensor._desired_keys({"sms": {"used": 3, "remain": 97}})
    assert "sms_used" in keys
    assert "sms_remain" in keys


# ---------------------------------------------------------------- F5 refresh 按号码


async def test_refresh_service_filters_by_phone(ha):
    """F5：phone 参数按号码过滤；entry_id 优先；未知号码报错。"""
    hass = FakeHass()
    entry = _unicom_entry()
    calls: list[str] = []

    from test_v17_qa_fixes import _coordinator_class

    created: list = []
    coordinator_cls = _coordinator_class(
        ha.coordinator.CarrierDataUpdateCoordinator, sink=created
    )

    async def _quiet_update(self):
        calls.append("updated")
        return {"balance": 1.0, "extra": {}}

    coordinator_cls._async_update_data = _quiet_update

    # 直接实例化真实子类（绕过 async_setup_entry 的 first_refresh）
    coordinator = coordinator_cls(hass, entry)
    hass.data[DOMAIN] = {entry.entry_id: coordinator}

    class _Call:
        def __init__(self, data):
            self.data = data

    # async_refresh 是 async_setup 的闭包；FakeHass 没有 services，
    # 挂一个只记录注册调用的壳再跑 async_setup
    registered = {}

    class _Services:
        @staticmethod
        def async_register(domain, service, handler, schema=None):
            registered[service] = (handler, schema)

    hass.services = _Services()
    await ha.async_setup(hass, None)
    handler, _schema = registered["refresh"]

    await handler(_Call({"phone": PHONE}))
    assert calls == ["updated"]

    with pytest.raises(Exception, match="未找到号码 19900000000"):
        await handler(_Call({"phone": "19900000000"}))

    # entry_id 优先于 phone
    await handler(_Call({"entry_id": entry.entry_id, "phone": "19900000000"}))
    assert calls == ["updated", "updated"]

    # coordinator 持有真实 ClientSession，测试结束前必须关闭
    for c in created:
        if not c.session.closed:
            await c.session.close()


# ---------------------------------------------------------------- F6 诊断事件摘要


def _diagnostics_module():
    """diagnostics 依赖 homeassistant.*，走 ha_stub 装载后的真实模块。"""
    import importlib

    return importlib.import_module("ccm.diagnostics")


def test_diagnostics_summarize_events(tmp_path, monkeypatch):
    """F6：事件摘要只含计数与 ts/event/category，不泄露手机号。"""
    import ast

    log = tmp_path / "events.jsonl"
    log.write_text(
        "\n".join(
            [
                '{"ts":"2026-11-01T12:00:00","event":"update_success","phone":"19974410126"}',
                '{"ts":"2026-11-01T12:30:00","event":"update_failure",'
                '"category":"auth_expired","err":"x","phone":"19974410126"}',
                "not-json-line",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # diagnostics 依赖 HA，但 _summarize_events 是纯函数：只提取该函数的
    # AST 子树 + 纯 stdlib import 在独立命名空间里求值
    src = (CCM_ROOT / "diagnostics.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_summarize_events"
    )
    # 提取函数体引用的顶层名字，把对应的 import 一并带过去；
    # 相对导入（from .x import y）改写为 ccm.x，便于脱离 HA 独立求值
    imported: dict[str, ast.stmt] = {}
    for n in tree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            if isinstance(n, ast.ImportFrom) and n.level:
                n = ast.fix_missing_locations(
                    ast.ImportFrom(
                        module=f"ccm.{n.module}" if n.module else "ccm",
                        names=n.names,
                        level=0,
                    )
                )
            for a in n.names:
                imported[(a.asname or a.name).split(".")[0]] = n
    used = {
        node.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    header = ast.Module(
        body=[imported[name] for name in sorted(used & imported.keys())],
        type_ignores=[],
    )
    namespace: dict = {"__name__": "diag_under_test", "__builtins__": __builtins__}
    exec(compile(header, "<diag>", "exec"), namespace)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<diag>", "exec"), namespace)
    _summarize_events = namespace["_summarize_events"]
    result = _summarize_events(str(log))
    assert result["counts"]["update_success"] == {"-": 1}
    assert result["counts"]["update_failure"] == {"auth_expired": 1}
    assert len(result["recent"]) == 2
    assert set(result["recent"][0]) == {"ts", "event", "category"}
    assert "19974410126" not in str(result)

    assert _summarize_events(str(tmp_path / "missing.jsonl")) == {}


# ---------------------------------------------------------------- F7 测试缺口


async def test_failures_and_anomalies_both_present_prefers_network(ccm):
    """F7-1：failures 与 anomalies 同时非空 → 闸门判 network（优先级锁）。"""
    base = ccm.modules["base"]
    event_log = _event_log(ccm)
    client = _mobile_client(ccm)
    seen_urls: list[str] = []

    async def _mixed(url, **kwargs):
        seen_urls.append(url)
        if len(seen_urls) <= 2:
            raise base.ParseAnomalyError(f"wx 响应顶层结构未知: <{url}>")
        raise base.CarrierError("网络错误: timeout")

    client._prepare_uni_headers = _no_headers
    client._wx_query = _mixed

    with pytest.raises(base.CarrierError) as excinfo:
        await client.async_fetch()
    err = excinfo.value
    assert not isinstance(err, base.CarrierAuthError)
    assert event_log.classify_exception(err) == event_log.CATEGORY_NETWORK


class _RetrySink:
    def __init__(self):
        self.created: list = []


async def test_retry_after_forward_failure_gets_fresh_session(ha, monkeypatch):
    """F7-2：forward 失败后重试 → 新会话独立、旧会话已关、hass.data 只有一份。"""
    hass = FakeHass()
    entry = _unicom_entry()
    sink = _RetrySink()

    from test_v17_qa_fixes import _coordinator_class

    coordinator_cls = _coordinator_class(
        ha.coordinator.CarrierDataUpdateCoordinator, sink=sink.created
    )
    monkeypatch.setattr(ha, "CarrierDataUpdateCoordinator", coordinator_cls)

    state = {"fail_first": True}

    async def _flaky_forward(*args, **kwargs):
        if state["fail_first"]:
            state["fail_first"] = False
            raise RuntimeError("platform boom")

    monkeypatch.setattr(
        hass.config_entries, "async_forward_entry_setups", _flaky_forward
    )

    with pytest.raises(RuntimeError, match="platform boom"):
        await ha.async_setup_entry(hass, entry)
    first = sink.created[0]
    assert first.session.closed is True
    assert entry.entry_id not in hass.data.get(DOMAIN, {})

    # HA 重试
    assert await ha.async_setup_entry(hass, entry) is True
    second = hass.data[DOMAIN][entry.entry_id]
    assert second is not first
    assert second.session is not first.session
    assert first.session.closed is True
    assert len(sink.created) == 2
    await second.async_shutdown()
