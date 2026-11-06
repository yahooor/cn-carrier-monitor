"""v1.11.0 回归：运营商维护态识别 + 状态传感器停更 + 诊断包过滤。

背景（2026-11-02～11-07 线上故障）：中国移动 wx.10086.cn 整站系统升级，
6 个端点全部返回「升级公告」HTML 页（500/404 各半），持续 5 天。
实测：mobile 被误判 network 278 次、auth_expired 15 次。

覆盖 4 个缺陷：
- 缺陷1：公告页被误判为 protocol_drift / network → 应归 maintenance
- 缺陷2：v1.10.1 自愈重试阈值条件永不成立（failures≥4 且无 anomalies）
- 缺陷3：session_state 传感器停更（HA 只在成功路径通知 listener）
- 缺陷4：诊断包 recent_events 未按账号过滤

测试用真实公告页样本（2026-11-07 实测抓取）。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest
from ha_stub import ConfigEntry, FakeHass, load_integration

PHONE = "13800000000"

# 真实公告页样本（2026-11-07 实测，形态 A：业务端点）
NOTICE_PAGE_A = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>升级公告</title>
</head>
<body>
<div class="main">
<p>尊敬的用户，由于系统升级，部分功能暂无法提供服务。攻城狮正在玩命调试中，请您稍后再试。</p>
</div>
</body>
</html>
"""

# 真实公告页样本（形态 B：站点根路径，正文用 &nbsp; 填充）
NOTICE_PAGE_B = """<!doctype html>
<html>
<head><meta charset="utf-8"><title>升级公告</title></head>
<body>
<div class="main">
<p>&nbsp;&nbsp;&nbsp;&nbsp;为给您提供更优质的服务，我们正在进行系统优化升级。</p>
<p>&nbsp;&nbsp;&nbsp;&nbsp;升级期间将影响业务的查询和办理，由此给您带来的不便，敬请谅解。</p>
<p>&nbsp;&nbsp;&nbsp;&nbsp;中国移动</p>
</div>
</body>
</html>
"""

NORMAL_JSON = '{"resultCode":"0","resultDesc":"success","data":{"balance":100}}'


@pytest.fixture(scope="module")
def ha():
    module = load_integration()
    import importlib

    importlib.import_module("ccm_ha.event_log")
    importlib.import_module("ccm_ha.diagnostics")
    importlib.import_module("ccm_ha.api.base")
    importlib.import_module("ccm_ha.api.mobile")
    return module


def _base(ha):
    """api.base 模块（异常类型与公告页判定）。"""
    import importlib

    return importlib.import_module("ccm_ha.api.base")


def _mobile(ha):
    """api.mobile 模块（MobileClient）。"""
    import importlib

    return importlib.import_module("ccm_ha.api.mobile")


def _event_log(ha):
    import importlib

    return importlib.import_module("ccm_ha.event_log")


def _diagnostics(ha):
    import importlib

    return importlib.import_module("ccm_ha.diagnostics")


class _FakeStateMachine:
    """模拟 HA 的实体状态机：只有收到 listener 通知才写状态快照。

    这是缺陷 3 回归测试的关键。真实 HA 的 states 机里存的是**陈旧快照**，
    只有 `async_update_listeners()` 被调用才会重写。若测试直接读
    `sensor.extra_state_attributes[...]`，读到的是活对象上的实时值，
    测试必然通过——但线上仍然是坏的（QA 已实测证明这个假绿）。

    这里监听协调器的 listener，每次通知就把当前协调器状态抄进 snapshot，
    与 HA 的行为一致。
    """

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.snapshot: dict = {}
        coordinator.async_add_listener(self._write)

    def _write(self) -> None:
        self.snapshot = {
            "fail_streak": getattr(self.coordinator, "fail_streak", None),
            "last_error": getattr(self.coordinator, "last_error", None),
            "session_state": getattr(self.coordinator, "session_state", None),
        }


def _run_async(coro):
    return asyncio.run(coro)


# ==================================================================== 缺陷 1


class TestMaintenanceDetection:
    """公告页识别：判据是响应体内容，不是状态码。"""

    def test_notice_page_a_detected(self, ha):
        base = _base(ha)
        assert base.is_maintenance_notice(NOTICE_PAGE_A) is True

    def test_notice_page_b_detected(self, ha):
        """形态 B 正文含 &nbsp; 实体，必须先解码实体再匹配。"""
        base = _base(ha)
        assert base.is_maintenance_notice(NOTICE_PAGE_B) is True

    def test_normal_json_not_notice(self, ha):
        base = _base(ha)
        assert base.is_maintenance_notice(NORMAL_JSON) is False

    def test_empty_body_not_notice(self, ha):
        base = _base(ha)
        assert base.is_maintenance_notice("") is False

    def test_real_404_page_not_notice(self, ha):
        """真正的 404 页面不是公告页 → 仍应归 protocol_drift。"""
        base = _base(ha)
        html_404 = (
            "<!doctype html><html><head><title>404</title></head>"
            "<body><h1>Not Found</h1></body></html>"
        )
        assert base.is_maintenance_notice(html_404) is False

    def test_generic_polite_html_not_notice(self, ha):
        """**误判防护**：通用礼貌语不得判为维护。

        「稍后再试」「敬请谅解」在真实故障页、限流页、帮助页上都很常见。
        若把它们当强特征，一次真实故障会被永久误判成「维护中」——
        用户看到的只是"运营商维护中"，永远不会去查真实原因，比原缺陷更难排查。
        """
        base = _base(ha)
        cases = [
            "<html><body><p>操作失败，请稍后再试</p></body></html>",
            "<html><body><p>感谢您的使用，敬请谅解</p></body></html>",
            "<html><body>本协议涉及设备系统维护说明</body></html>",
        ]
        for body in cases:
            assert base.is_maintenance_notice(body) is False, (
                f"通用 HTML 被误判为维护公告: {body[:60]}"
            )

    def test_weak_marker_with_context_is_notice(self, ha):
        """弱特征词 + 上下文词（升级/维护…）→ 仍判为维护。

        保证收紧特征词后不会漏掉真实公告页：公告页正文通常同时解释原因。
        """
        base = _base(ha)
        body = (
            "<html><head><title>提示</title></head><body>"
            "<p>系统升级中，敬请谅解，稍后再试</p></body></html>"
        )
        assert base.is_maintenance_notice(body) is True

    def test_maintenance_error_is_carrier_error(self, ha):
        """新异常必须继承 CarrierError，否则会被当 internal 吞掉。"""
        base = _base(ha)
        assert issubclass(base.CarrierMaintenanceError, base.CarrierError)
        assert not issubclass(base.CarrierMaintenanceError, base.CarrierAuthError)

    def test_classify_exception_maintenance(self, ha):
        """事件日志分类：maintenance 独立成类，且优先于 auth_expired。"""
        el = _event_log(ha)
        base = _base(ha)
        err = base.CarrierMaintenanceError("移动网厅系统升级维护中")
        assert el.classify_exception(err) == "maintenance"
        # 不能被误判成 network / protocol_drift / auth_expired
        assert el.classify_exception(err) not in (
            "network", "protocol_drift", "auth_expired", "internal",
        )

    def test_classify_exception_auth_still_works(self, ha):
        """回归：认证失败仍归 auth_expired。"""
        el = _event_log(ha)
        base = _base(ha)
        assert el.classify_exception(base.CarrierAuthError("x")) == "auth_expired"


class TestMobileEndpointClassification:
    """端点级：公告页必须抛 CarrierMaintenanceError。"""

    def _client(self, ha, **over):
        """构造一个不带真实连接的 MobileClient（避免 aiohttp 建连）。

        沿用 tests/test_v17_qa_fixes.py 的做法：__new__ 绕过 __init__，
        只设置被测方法真正用到的属性。
        """
        mobile = _mobile(ha)
        client = mobile.MobileClient.__new__(mobile.MobileClient)
        client._session = None
        client._data = {
            "carrier": "mobile",
            "phone": PHONE,
            "auth_mode": "cookie",
            "cookies": "uuid=x; d.sid=y",
            **over,
        }
        client._cookie = client._data.get("cookies") or ""
        client._mode = "cookie"
        client._proxy = client._data.get("proxy_url")
        client._merge_cookies = lambda fresh: None
        return client

    def test_500_with_notice_is_maintenance(self, ha):
        """HTTP 500 + 公告页 → maintenance（不是 network）。"""
        client = self._client(ha)

        async def fake_cffi(url, method="GET", json_body=None, extra_headers=None):
            return 500, NOTICE_PAGE_A

        client._cffi_wx = fake_cffi
        with pytest.raises(_base(ha).CarrierMaintenanceError):
            _run_async(client._wx_query("https://wx.10086.cn/website/fareBalance"))

    def test_404_with_notice_is_maintenance(self, ha):
        """HTTP 404 + 公告页 → maintenance（不是 protocol_drift）。

        修复前 404 一律抛 ParseAnomalyError，会把维护误报成"端点已下线"。
        """
        client = self._client(ha)

        async def fake_cffi(url, method="GET", json_body=None, extra_headers=None):
            return 404, NOTICE_PAGE_A

        client._cffi_wx = fake_cffi
        with pytest.raises(_base(ha).CarrierMaintenanceError):
            _run_async(client._wx_query("https://wx.10086.cn/website/fareBalance"))

    def test_404_without_notice_is_still_drift(self, ha):
        """回归：真正的端点下线仍归 protocol_drift。"""
        client = self._client(ha)

        async def fake_cffi(url, method="GET", json_body=None, extra_headers=None):
            return 404, "<html><title>404</title><body>Not Found</body></html>"

        client._cffi_wx = fake_cffi
        with pytest.raises(_base(ha).ParseAnomalyError):
            _run_async(client._wx_query("https://wx.10086.cn/website/fareBalance"))

    def test_500_without_notice_is_still_network(self, ha):
        """回归：真网关故障仍归 network。"""
        client = self._client(ha)

        async def fake_cffi(url, method="GET", json_body=None, extra_headers=None):
            return 500, "Bad Gateway"

        client._cffi_wx = fake_cffi
        with pytest.raises(_base(ha).CarrierError) as exc:
            _run_async(client._wx_query("https://wx.10086.cn/website/fareBalance"))
        assert not isinstance(exc.value, _base(ha).CarrierMaintenanceError)


# ==================================================================== 缺陷 2


class TestSelfHealThreshold:
    """自愈重试阈值：按"失败端点/尝试端点"比例判定，而不是 failures 绝对数。"""

    def _client(self, ha, proxy="http://p:1@127.0.0.1:7890"):
        mobile = _mobile(ha)
        client = mobile.MobileClient.__new__(mobile.MobileClient)
        client._session = None
        client._data = {
            "carrier": "mobile",
            "phone": PHONE,
            "auth_mode": "cookie",
            "cookies": "uuid=x; d.sid=y",
            "proxy_url": proxy,
        }
        client._cookie = client._data.get("cookies") or ""
        client._mode = "cookie"
        client._proxy = proxy
        client._merge_cookies = lambda fresh: None
        return client

    async def test_all_endpoints_down_triggers_retry(self, ha):
        """全部端点失败 → 触发重签重试。

        修复前的条件 `len(failures) >= 4 and not anomalies` 在真实故障里
        永不成立（failures=2 / anomalies=3），5 天 380+ 次失败里重试
        一次都没跑过。
        """
        client = self._client(ha)
        client._proxy = "http://p:1@127.0.0.1:7890"
        calls = {"prepare": 0}

        async def counting_prepare():
            calls["prepare"] += 1
            return {}

        client._prepare_uni_headers = counting_prepare

        async def fake_query(url, method="GET", json_body=None, extra_headers=None,
                             top_keys=None):
            # 全部端点传输失败（模拟 d.sid 与出口 IP 不匹配的真实场景）
            raise _base(ha).CarrierError(f"网络错误: {url} HTTP 500")

        client._wx_query = fake_query
        data = {"carrier": "mobile", "phone": PHONE, "extra": {}}
        await client._wx_fetch(data)
        # 首轮自愈前置 1 次 + 重试轮 1 次 = 2 次
        assert calls["prepare"] == 2, "全部端点失败时未触发自愈重试"

    async def test_mixed_failure_triggers_retry(self, ha):
        """**混合失败**（failures + anomalies 混合）→ 必须触发重签重试。

        这是线上真实故障的形态：端点失败会被 failures / anomalies 两类切分，
        维护期实测 failures=2、anomalies=3。旧条件 `len(failures) >= 4 and
        not anomalies` 在这种形态下**永远不成立** → 5 天 380+ 次失败里
        重试一次都没跑过。

        因此本用例必须精确复现「failures=2, anomalies=3」这个分布。
        """
        client = self._client(ha)
        client._proxy = "http://p:1@127.0.0.1:7890"
        calls = {"prepare": 0}

        async def counting_prepare():
            calls["prepare"] += 1
            return {}

        client._prepare_uni_headers = counting_prepare

        async def fake_query(url, method="GET", json_body=None, extra_headers=None,
                             top_keys=None):
            # 2 个传输失败（failures）+ 3 个结构漂移（anomalies）
            if "fareBalance" in url or "getNewMarginInfo" in url:
                raise _base(ha).CarrierError(f"网络错误: {url} HTTP 500")
            raise _base(ha).ParseAnomalyError(f"移动网厅 {url} HTTP 404（端点已下线或迁移）")

        client._wx_query = fake_query
        data = {"carrier": "mobile", "phone": PHONE, "extra": {}}
        await client._wx_fetch(data)

        # 注意：extra 里的清单是**重试轮之后**的（重试轮会 clear 再重填），
        # 重试轮只请求 4 个数据端点（不含 score/news）。所以这里断言的是
        # 「重试确实发生了」这个行为，而不是清单的精确条数。
        assert calls["prepare"] == 2, (
            "混合失败（failures=2, anomalies=3）未触发自愈重试——"
            "这正是线上 5 天 380+ 次失败里重试一次都没跑过的原因"
        )
        # 重试轮仍然全失败 → 清单非空
        assert data.get("extra", {}).get("endpoint_failures") or (
            data.get("extra", {}).get("endpoint_anomalies")
        ), "重试后应记录端点级失败证据"

    async def test_partial_failure_does_not_trigger_retry(self, ha):
        """部分端点失败 → 不触发重试（避免徒增风控暴露）。"""
        client = self._client(ha)
        client._proxy = "http://p:1@127.0.0.1:7890"
        calls = {"prepare": 0}

        async def counting_prepare():
            calls["prepare"] += 1
            return {}

        client._prepare_uni_headers = counting_prepare

        async def fake_query(url, method="GET", json_body=None, extra_headers=None,
                             top_keys=None):
            # 只有部分端点失败：其余返回**合法**载荷（含已知顶层键 data，
            # 否则会被判成 protocol_drift，失败数就会算成全部）
            if "fareBalance" in url or "score" in url:
                raise _base(ha).CarrierError(f"网络错误: {url} HTTP 500")
            return {"data": {"resultData": {}}}

        client._wx_query = fake_query
        data = {"carrier": "mobile", "phone": PHONE, "extra": {}}
        await client._wx_fetch(data)
        assert calls["prepare"] == 1, "部分失败时不应触发自愈重试"

    async def test_maintenance_does_not_trigger_retry(self, ha):
        """维护态 → 绝不触发重签重试（重签 d.sid 对维护无意义）。"""
        client = self._client(ha)
        client._proxy = "http://p:1@127.0.0.1:7890"
        calls = {"prepare": 0}

        async def counting_prepare():
            calls["prepare"] += 1
            return {}

        client._prepare_uni_headers = counting_prepare

        async def fake_query(url, method="GET", json_body=None, extra_headers=None,
                             top_keys=None):
            # 模拟"公告页已被识别为维护"：直接抛 CarrierMaintenanceError
            raise _base(ha).CarrierMaintenanceError(
                "移动网厅系统升级维护中（公告页），稍后重试"
            )

        client._wx_query = fake_query
        data = {"carrier": "mobile", "phone": PHONE, "extra": {}}
        await client._wx_fetch(data)
        assert calls["prepare"] == 1, "维护态不应触发自愈重试"


# ==================================================================== 缺陷 3


class TestSessionStateRefresh:
    """session_state 传感器停更。

    HA 2026.10.4 `helpers/update_coordinator.py`：async_update_listeners()
    只在成功路径调用 → 协调器抛 UpdateFailed 时实体状态不再重写。
    实测：传感器属性停在 fail_streak=1，协调器已累计 75 次失败。
    """

    def _make_coordinator(self, ha, hass, client):
        entry = ConfigEntry(
            "e1",
            {"carrier": "mobile", "phone": PHONE, "auth_mode": "cookie"},
            title="移动",
        )
        coord = ha.coordinator.CarrierDataUpdateCoordinator(hass, entry)
        coord.client = client
        coord.data = None
        return coord

    async def test_failure_notifies_listeners(self, ha):
        """连续失败时必须通知 listener（实体才能刷新状态）。"""
        hass = FakeHass()
        calls = {"n": 0}

        class _FailClient:
            async def async_fetch(self):
                raise _base(ha).CarrierError("网络错误: boom")

        coord = self._make_coordinator(ha, hass, _FailClient())
        coord.async_add_listener(lambda: calls.__setitem__("n", calls["n"] + 1))
        before = calls["n"]
        with pytest.raises(Exception):
            await coord._async_update_data()
        assert calls["n"] > before, "失败路径未通知 listener，session_state 会停更"

    async def test_state_machine_snapshot_updates_each_failure(self, ha):
        """**HA 状态机快照**必须随每次失败更新（不是活对象属性）。

        QA 实测的假绿陷阱：直接读 `sensor.extra_state_attributes[...]` 读到的是
        活对象上的实时值，测试必然通过；而真实 HA 的 states 机里是**陈旧快照**。
        这里用一个模拟状态机（只有收到 listener 通知才写状态）来复现真实行为。
        """
        hass = FakeHass()

        class _FailClient:
            async def async_fetch(self):
                raise _base(ha).CarrierError("网络错误: boom")

        coord = self._make_coordinator(ha, hass, _FailClient())
        machine = _FakeStateMachine(coord)

        for expected in (1, 2, 3):
            with pytest.raises(Exception):
                await coord._async_update_data()
            assert machine.snapshot.get("fail_streak") == expected, (
                f"HA 状态机快照未随第 {expected} 次失败更新"
                f"（实际 {machine.snapshot.get('fail_streak')}）"
            )
            assert machine.snapshot.get("last_error"), "状态机里 last_error 缺失"
            assert machine.snapshot.get("session_state"), "状态机里 session_state 缺失"

    async def test_state_machine_last_error_refreshes(self, ha):
        """last_error 在连续失败中持续刷新（不停在第一次）。

        用**不同的错误文案**来区分两轮（时间戳是秒级精度，同秒内两次失败
        的 at 字段可能相同，不能用它做判据）。
        """
        hass = FakeHass()
        msgs = iter(["网络错误: 第一轮", "网络错误: 第二轮"])

        class _FailClient:
            async def async_fetch(self):
                raise _base(ha).CarrierError(next(msgs))

        coord = self._make_coordinator(ha, hass, _FailClient())
        machine = _FakeStateMachine(coord)

        with pytest.raises(Exception):
            await coord._async_update_data()
        first = machine.snapshot.get("last_error")
        with pytest.raises(Exception):
            await coord._async_update_data()
        second = machine.snapshot.get("last_error")
        assert first["message"] != second["message"], (
            "last_error 停在首次失败，未持续刷新"
        )
        assert machine.snapshot.get("fail_streak") == 2

    async def test_circuit_branch_is_reachable(self, ha):
        """熔断分支在真实轮询节奏下**能被进入**（这是它作为兜底的意义）。

        修复前：`CIRCUIT_COOLDOWN=900s` < 轮询间隔 1800s，且 `_last_fail_at`
        每轮刷新 → 条件恒假 → 线上 75 次失败一次都没进过熔断。
        """
        hass = FakeHass()

        class _FailClient:
            async def async_fetch(self):
                raise _base(ha).CarrierError("网络错误: boom")

        coord = self._make_coordinator(ha, hass, _FailClient())
        # 连续失败到阈值以上
        for _ in range(ha.coordinator.CarrierDataUpdateCoordinator.FAILURE_THRESHOLD):
            with pytest.raises(Exception):
                await coord._async_update_data()
        assert coord.fail_streak >= (
            ha.coordinator.CarrierDataUpdateCoordinator.FAILURE_THRESHOLD
        )
        # 再调一次：此时应命中熔断分支（**返回**而非抛异常）
        result = await coord._async_update_data()
        assert (result.get("extra") or {}).get("circuit_open") is True, (
            "熔断分支未能进入（条件恒假），兜底机制失效"
        )

    async def test_fail_streak_keeps_growing_and_state_reflects_it(self, ha):
        """连续失败后 fail_streak 增长，session_state 随失败次数推进。"""
        hass = FakeHass()

        class _FailClient:
            async def async_fetch(self):
                raise _base(ha).CarrierError("网络错误: boom")

        coord = self._make_coordinator(ha, hass, _FailClient())
        threshold = ha.coordinator.CarrierDataUpdateCoordinator.FAILURE_THRESHOLD
        seen = []
        for _ in range(threshold):
            with pytest.raises(Exception):
                await coord._async_update_data()
            seen.append(coord.session_state)
        assert coord.fail_streak == threshold
        # 达到阈值后的下一轮命中熔断分支（返回而非抛异常），session_state 变 circuit_open
        result = await coord._async_update_data()
        assert coord.session_state == "circuit_open", (
            f"达到熔断阈值后 session_state 应为 circuit_open，实际 {seen}"
        )
        assert (result.get("extra") or {}).get("circuit_open") is True

    async def test_maintenance_does_not_accumulate_fail_streak(self, ha):
        """维护态不累加熔断计数（熔断针对可自愈故障）。"""
        hass = FakeHass()

        class _MaintClient:
            async def async_fetch(self):
                raise _base(ha).CarrierMaintenanceError("系统升级维护中")

        coord = self._make_coordinator(ha, hass, _MaintClient())
        for _ in range(3):
            await coord._async_update_data()
        assert coord.fail_streak == 0, "维护态不应累加熔断计数"
        assert coord.auth_fail_streak == 0, "维护态不应推高认证失败计数"

    async def test_maintenance_session_state(self, ha):
        """维护态 session_state == maintenance。"""
        hass = FakeHass()

        class _MaintClient:
            async def async_fetch(self):
                raise _base(ha).CarrierMaintenanceError("系统升级维护中")

        coord = self._make_coordinator(ha, hass, _MaintClient())
        await coord._async_update_data()
        assert coord.session_state == "maintenance"

    async def test_maintenance_does_not_notify_needs_human(self, ha):
        """维护态不弹「请重新登录」（对维护是误导）。"""
        hass = FakeHass()
        notified = []

        class _MaintClient:
            async def async_fetch(self):
                raise _base(ha).CarrierMaintenanceError("系统升级维护中")

        coord = self._make_coordinator(ha, hass, _MaintClient())
        coord._notify_needs_human = lambda reason: notified.append(reason)
        await coord._async_update_data()
        assert not notified, "维护态不应触发 needs_human 通知"

    async def test_maintenance_does_not_send_sms(self, ha):
        """维护态不触发自动重登发码（白烧短信费 + 加倍风控暴露）。"""
        hass = FakeHass()
        sent = []

        class _MaintClient:
            async def async_fetch(self):
                raise _base(ha).CarrierMaintenanceError("系统升级维护中")

        coord = self._make_coordinator(ha, hass, _MaintClient())

        async def fake_sms():
            sent.append(1)

        coord._async_safe_send_relogin_sms = fake_sms
        await coord._async_update_data()
        assert not sent, "维护态不应触发自动发码"

    async def test_maintenance_uses_long_interval(self, ha):
        """维护态用小时级退避，不再 30 分钟硬打必然失败的请求。"""
        hass = FakeHass()

        class _MaintClient:
            async def async_fetch(self):
                raise _base(ha).CarrierMaintenanceError("系统升级维护中")

        coord = self._make_coordinator(ha, hass, _MaintClient())
        await coord._async_update_data()
        assert coord.update_interval.total_seconds() >= 3600, (
            "维护态轮询间隔应拉长到小时级"
        )

    async def test_network_failure_backs_off(self, ha):
        """网络类失败要退避（此前只有认证失败才退避）。"""
        hass = FakeHass()

        class _FailClient:
            async def async_fetch(self):
                raise _base(ha).CarrierError("网络错误: boom")

        coord = self._make_coordinator(ha, hass, _FailClient())
        base = coord._base_interval.total_seconds()
        with pytest.raises(Exception):
            await coord._async_update_data()
        assert coord.update_interval.total_seconds() > base, (
            "网络类失败应拉长轮询间隔"
        )

    async def test_circuit_cooldown_exceeds_poll_interval(self, ha):
        """熔断冷却必须大于轮询间隔，否则熔断分支永远进不去。

        旧值硬编码 900s < 轮询间隔 1800s，且 _last_fail_at 每轮刷新，
        熔断分支（唯一会 return 而非 raise 的分支）形同虚设。

        现在冷却是动态的（`_circuit_cooldown()` = max(下限, 轮询间隔×2)），
        因此断言用「冷却 ≥ 轮询间隔」这个关系，而不是某个固定数值——
        用户把轮询间隔改成 60 分钟甚至更长时，固定常数同样会失效。
        """
        hass = FakeHass()

        class _FailClient:
            async def async_fetch(self):
                raise _base(ha).CarrierError("网络错误: boom")

        coord = self._make_coordinator(ha, hass, _FailClient())
        assert coord._circuit_cooldown() >= coord._base_interval.total_seconds(), (
            "熔断冷却应不小于轮询间隔"
        )


# ==================================================================== 缺陷 4


class TestDiagnosticsCarrierFilter:
    """诊断包 recent_events 必须按账号过滤。"""

    def _write_events(self, path):
        rows = [
            {"ts": "2026-11-06T10:00:00+08:00", "event": "update_success",
             "carrier": "mobile", "phone": "13800000001"},
            {"ts": "2026-11-06T10:05:00+08:00", "event": "update_failure",
             "category": "network", "carrier": "mobile", "phone": "13800000001"},
            {"ts": "2026-11-06T10:10:00+08:00", "event": "update_failure",
             "category": "auth_expired", "carrier": "unicom",
             "phone": "13800000002"},
            {"ts": "2026-11-06T10:15:00+08:00", "event": "update_success",
             "carrier": "telecom", "phone": "13800000003"},
        ]
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return rows

    def test_filter_by_carrier(self, ha):
        diag = _diagnostics(ha)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "events.jsonl")
            self._write_events(path)

            mob = diag._summarize_events(path, "mobile")
            uni = diag._summarize_events(path, "unicom")

            assert mob["counts"].get("update_failure", {}).get("network") == 1
            assert "auth_expired" not in mob["counts"].get("update_failure", {})
            assert uni["counts"].get("update_failure", {}).get("auth_expired") == 1
            assert "network" not in uni["counts"].get("update_failure", {})

    def test_filtered_results_differ(self, ha):
        """不同账号的诊断包 recent_events 不能完全相同。"""
        diag = _diagnostics(ha)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "events.jsonl")
            self._write_events(path)
            mob = diag._summarize_events(path, "mobile")
            uni = diag._summarize_events(path, "unicom")
            assert mob["recent"] != uni["recent"]

    def test_no_phone_leaked(self, ha):
        """过滤后不得泄露手机号（威胁模型 R2）。"""
        diag = _diagnostics(ha)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "events.jsonl")
            self._write_events(path)
            blob = json.dumps(diag._summarize_events(path, "mobile"), ensure_ascii=False)
            assert "13800000001" not in blob
