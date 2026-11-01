"""v1.8 审查修复回归（QA 2026-11-01 报告的 P1/P2 项 + 元测试补缺）。

覆盖：
- P1#1  webhook.json_response → aiohttp.web.json_response（全链路状态码）
- P1#2  reauth 路由指向真实存在的 step（元测试：route 表逐项 getattr）
- P1#3  unicom_sms_code step 签名兼容单位置参数调用（元测试）
- P1#4  广电 parse_import 失败不泄漏 ClientSession
- P2#5  telecom 3005/3007 归 rate_limit 而非 auth_expired
- P2#6  webhook 重登成功后 dismiss 掉线通知
- P3#12 滑块 bw==pw 不再 ZeroDivision 逃逸
- P3#13 非 ASCII webhook secret 不再 TypeError
"""

from __future__ import annotations

import asyncio
import base64
import json as jsonlib

import aiohttp
import pytest
from ha_stub import ConfigEntry, FakeHass, load_integration

DOMAIN = "cn_carrier_monitor"
PHONE = "13800000000"


@pytest.fixture(scope="module")
def ha():
    module = load_integration()
    import importlib

    importlib.import_module("ccm_ha.config_flow")
    importlib.import_module("ccm_ha.event_log")
    return module


def _sms_entry(entry_id: str = "sms1", carrier: str = "telecom") -> ConfigEntry:
    return ConfigEntry(
        entry_id,
        {
            "carrier": carrier,
            "phone": PHONE,
            "auth_mode": "sms",
            "password": "",
        },
        title=f"{carrier} sms",
    )


def _run_async(coro):
    """asyncio.run 包装（避免弃用的 get_event_loop）。"""
    return asyncio.run(coro)


class _FakeRequest:
    """aiohttp Request 鸭子桩：webhook handler 只用 json()/headers/remote。"""

    def __init__(self, payload=None, *, headers=None, remote="192.168.1.50"):
        self._payload = payload
        self.headers = headers or {}
        self.remote = remote

    async def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def _webhook_handler(ha_module):
    """触发一次 async_setup 让 webhook 注册，返回 handler。"""
    from ha_stub import FakeHass as FH

    registered: dict = {}

    class _Svcs:
        @staticmethod
        def async_register(domain, service, handler, schema=None):
            registered[service] = handler

    hass = FH()
    hass.services = _Svcs()
    _run_async(ha_module.async_setup(hass, None))
    # webhook 注册发生在集成 async_setup 里：从桩的存储里拿 handler
    import sys

    state = sys.modules.get("__ha_stub_state")
    assert state is not None and state.webhook_handlers, "webhook 未注册"
    return state.webhook_handlers[f"{DOMAIN}_sms"], hass


def _status(resp) -> tuple[int, dict]:
    return resp.status, jsonlib.loads(resp.text)


# ---------------------------------------------------------------- P1#1 webhook


def test_webhook_invalid_payload_returns_400(ha):
    handler, hass = _webhook_handler(ha)
    resp = _run_async(
        handler(hass, DOMAIN, _FakeRequest(None))
    )
    status, body = _status(resp)
    assert status == 400 and body["error"] == "invalid_payload"


def test_webhook_no_matching_account_returns_404(ha):
    handler, hass = _webhook_handler(ha)
    resp = _run_async(
        handler(hass, DOMAIN, _FakeRequest({"phone": "19900000000", "code": "123456"}))
    )
    status, body = _status(resp)
    assert status == 404 and body["error"] == "no_matching_account"


def test_webhook_public_without_secret_returns_403(ha, monkeypatch):
    """未配置 secret 且请求来自公网 → 403（P1#1 修复前这里 AttributeError→200）。"""
    handler, hass = _webhook_handler(ha)
    entry = _sms_entry()
    monkeypatch.setattr(ha.webhook, "_sms_entries", lambda h: [entry])
    req = _FakeRequest({"code": "123456", "phone": ""}, remote="8.8.8.8")
    resp = _run_async(handler(hass, DOMAIN, req))
    status, body = _status(resp)
    assert status == 403 and body["error"] == "secret_required"


def test_webhook_bad_secret_returns_403(ha, monkeypatch):
    handler, hass = _webhook_handler(ha)
    entry = _sms_entry()
    entry.options["webhook_secret"] = "s3cret"
    monkeypatch.setattr(
        ha.webhook, "_sms_entries", lambda h: [entry]
    )
    req = _FakeRequest(
        {"code": "123456", "phone": "", "secret": "wrong"}, headers={}, remote="192.168.1.50"
    )
    resp = _run_async(handler(hass, DOMAIN, req))
    status, body = _status(resp)
    assert status == 403 and body["error"] == "bad_secret"


def test_webhook_non_ascii_secret_no_typeerror(ha, monkeypatch):
    """P3#13：中文密钥 compare_digest 不再抛 TypeError（表现为 403 而非 500）。"""
    handler, hass = _webhook_handler(ha)
    entry = _sms_entry()
    entry.options["webhook_secret"] = "密钥测试"
    monkeypatch.setattr(ha.webhook, "_sms_entries", lambda h: [entry])
    req = _FakeRequest({"code": "123456", "phone": "", "secret": "不对"})
    resp = _run_async(handler(hass, DOMAIN, req))
    status, _ = _status(resp)
    assert status == 403  # 不是 500


def test_webhook_rate_limit_returns_429(ha, monkeypatch):
    handler, hass = _webhook_handler(ha)
    entry = _sms_entry()
    monkeypatch.setattr(ha.webhook, "_sms_entries", lambda h: [entry])
    monkeypatch.setattr(ha.webhook, "_rate_limited", lambda key: True)
    resp = _run_async(
        handler(hass, DOMAIN, _FakeRequest({"code": "123456", "phone": ""}))
    )
    status, body = _status(resp)
    assert status == 429 and body["error"] == "rate_limited"


def test_webhook_relogin_failure_returns_500(ha, monkeypatch):
    handler, hass = _webhook_handler(ha)
    entry = _sms_entry()
    monkeypatch.setattr(ha.webhook, "_sms_entries", lambda h: [entry])

    async def _boom(hass_, entry_, code_):
        raise RuntimeError("carrier down")

    monkeypatch.setattr(ha.webhook, "_relogin", _boom)
    resp = _run_async(
        handler(hass, DOMAIN, _FakeRequest({"code": "123456", "phone": ""}))
    )
    status, body = _status(resp)
    assert status == 500 and body["error"] == "relogin_failed"


def test_webhook_relogin_success_dismisses_notification(ha, monkeypatch):
    """P2#6：重登成功后必须 dismiss 掉线通知（reload 新建 coordinator 无人撤）。"""
    handler, hass = _webhook_handler(ha)
    entry = _sms_entry()
    monkeypatch.setattr(ha.webhook, "_sms_entries", lambda h: [entry])

    async def _ok(hass_, entry_, code_):
        return {"token": "t"}

    monkeypatch.setattr(ha.webhook, "_relogin", _ok)
    dismissed: list[str] = []


    def _fake_dismiss(hass_, nid):
        dismissed.append(nid)

    monkeypatch.setattr(
        "homeassistant.components.persistent_notification.async_dismiss",
        _fake_dismiss,
    )
    resp = _run_async(
        handler(hass, DOMAIN, _FakeRequest({"code": "123456", "phone": ""}))
    )
    status, body = _status(resp)
    assert status == 200 and body["ok"] is True
    assert dismissed == [f"{DOMAIN}_reauth_{entry.entry_id}"]


# ---------------------------------------------------------------- P1#4 broadnet


async def test_broadnet_import_failure_closes_session(ha, monkeypatch):
    """P1#4：parse_import 抛错时 ClientSession 必须关闭（此前泄漏）。"""
    broadnet = __import__("ccm_ha.api.broadnet", fromlist=["BroadnetClient"])
    BroadnetClient = broadnet.BroadnetClient

    created: list = []
    real_init = aiohttp.ClientSession.__init__

    def _tracking_init(self, *a, **kw):
        real_init(self, *a, **kw)
        created.append(self)

    monkeypatch.setattr(aiohttp.ClientSession, "__init__", _tracking_init)

    def _boom(source, cookie):
        from ccm_ha.api.base import CarrierError

        raise CarrierError("bad curl")

    monkeypatch.setattr(BroadnetClient, "parse_import", staticmethod(_boom))

    flow = ha.config_flow.ChinaCarrierMonitorConfigFlow()
    flow.hass = FakeHass()
    result = await flow.async_step_broadnet(
        {"request_source": "curl garbage", "cookies": "", "phone": ""}
    )
    assert result["errors"]["base"] == "broadnet_import_invalid"
    assert created, "session 应该被创建过"
    assert all(s.closed for s in created), "parse_import 失败后 session 必须关闭"


# ---------------------------------------------------------------- P1#2 元测试


def test_reauth_route_targets_all_exist(ha):
    """P1#2 元测试：reauth route 表里每个值必须对应真实存在的 step。"""
    import re as _re

    src = (ha.__path__[0] and __import__("pathlib").Path(ha.__file__)).read_text(
        encoding="utf-8"
    ) if False else None
    # 直接解析 route dict
    from pathlib import Path

    src = (Path(ha.__file__).parent / "config_flow.py").read_text(encoding="utf-8")
    m = _re.search(r"route = \{(.*?)\}", src, _re.S)
    assert m, "找不到 reauth route 表"
    targets = _re.findall(r'"([a-z_]+)"[,}]', m.group(1))
    assert targets, "route 表为空"
    for name in targets:
        assert hasattr(ha.config_flow.ChinaCarrierMonitorConfigFlow, f"async_step_{name}"), (
            f"route 指向不存在的 step: async_step_{name}"
        )


def test_flow_steps_accept_single_positional_arg(ha):
    """P1#3 元测试：HA flow manager 以 step(user_input) 单参数调用，
    所有 async_step_* 必须兼容（杜绝 unicom_sms_code 的 hint 形参复发）。"""
    import inspect

    for name, member in vars(ha.config_flow.ChinaCarrierMonitorConfigFlow).items():
        if not (name.startswith("async_step_") and callable(member)):
            continue
        sig = inspect.signature(member)
        positional = [
            p
            for p in list(sig.parameters.values())[1:]  # [0] 是 self
            if p.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        assert len(positional) <= 1, (
            f"{name} 除 self 外有 {len(positional)} 个位置参数（HA 只传 1 个）: {positional}"
        )


# ---------------------------------------------------------------- P2#5 分类


def test_telecom_rate_limit_codes_classify_as_rate_limit(ccm):
    """P2#5：telecom 3005/3007 → rate_limit（此前误归 auth_expired）。"""
    from ccm_ha.api.base import CarrierAuthError
    from ccm_ha.event_log import CATEGORY_RATE_LIMIT, classify_exception

    err = CarrierAuthError("telecom_need_captcha", detail="rate_limit:3005")
    assert classify_exception(err) == CATEGORY_RATE_LIMIT
    err2 = CarrierAuthError("telecom_too_frequent", detail="rate_limit:3007")
    assert classify_exception(err2) == CATEGORY_RATE_LIMIT
    # 普通会话失效仍是 auth
    assert classify_exception(CarrierAuthError("unicom_session_expired")) == "auth_expired"


# ---------------------------------------------------------------- P3#12 滑块


def test_telecom_slider_zero_width_no_crash(ha, monkeypatch):
    """P3#12：bw==pw 时走 continue 重试，不再 ZeroDivision 逃逸。"""
    telecom = __import__("ccm_ha.api.telecom", fromlist=["TelecomClient"])

    # 1x1 透明 PNG：_locate_notch 对背景/滑块同宽场景返回 bw==pw
    tiny_png = base64.b64encode(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000a49444154789c6300010000050001"
        )
    ).decode()

    client = telecom.TelecomClient.__new__(telecom.TelecomClient)
    client._session = None  # 不会真正发请求
    client._uid = "u1"
    client._key = ""
    client._isct = "0"
    client._data = {"phone": PHONE}

    async def _fake_login_v2(endpoint, body, extra=None):
        if endpoint == "getLoginRandomCode":
            return {}, {"key": "k", "isChinatelecom": "1"}
        if endpoint == "getSliderVerificationPicture":
            return {}, {"backgroundImg": tiny_png, "sliderImg": tiny_png,
                        "key": "k", "extra": base64.b64encode(b"x").decode()}
        if endpoint == "checkSliderVerificationPicture":
            # 滑块校验永远不过 → 主循环重试
            return {}, {"responseData": {"resultCode": "3005",
                                          "resultDesc": "need captcha"}}
        raise AssertionError(endpoint)

    client._login_v2 = _fake_login_v2

    async def _run():
        # bw==pw 守卫生效的表现：每轮都 continue 重试，5 轮耗尽后抛
        # CarrierError（telecom_sms_send_failed）——绝不能 ZeroDivisionError 逃逸
        from ccm_ha.api.base import CarrierError

        try:
            await client.async_login_sms_send()
            raise AssertionError("应当重试耗尽抛 CarrierError")
        except CarrierError as err:
            # 5 轮耗尽 → telecom_sms_send_failed（detail 含"自动过滑块"）
            assert "滑块" in str(getattr(err, "detail", "") or "")

    _run_async(
        asyncio.wait_for(_run(), timeout=10)
    )


async def _run_slider_guard(client):
    from ccm_ha.api.base import CarrierAuthError

    try:
        await client.async_login_sms_send()
    except CarrierAuthError as err:
        # 3005 → rate_limit 子类，符合预期终点
        assert "rate_limit" in str(getattr(err, "detail", "")) or "need_captcha" in str(err)
    except RuntimeError:
        raise AssertionError("ZeroDivision 或其它崩溃逃逸")
    # 正常 return（重试耗尽放弃）也算通过


# ---------------------------------------------------------------- 回归守卫


def test_build_relogin_client_exists_for_sms_carriers(ha):
    """回归守卫：_build_relogin_client 被误删会让自动发码静默失效。

    async_send_relogin_sms（telecom/unicom 短信模式的自动发码）依赖它构造
    临时客户端。删除 async_complete_sms_relogin 死方法时它曾被连带删除。
    """
    assert hasattr(ha.coordinator.CarrierDataUpdateCoordinator, "_build_relogin_client")


async def test_async_send_relogin_sms_dispatches_by_carrier(ha):
    """telecom → async_login_sms_send；unicom → async_sms_trigger；mobile → unsupported。"""
    from ha_stub import ConfigEntry as CE, FakeHass as FH

    for carrier, expect_sent in (("telecom", True), ("unicom", True), ("mobile", False)):
        entry = CE(
            f"e_{carrier}",
            {"carrier": carrier, "phone": PHONE, "auth_mode": "sms"},
            title=carrier,
        )
        coordinator = ha.coordinator.CarrierDataUpdateCoordinator(FH(), entry)
        try:
            if expect_sent:
                called = []

                def _make(name):
                    async def _send():
                        called.append(name)
                        return True if name == "telecom" else "已下发验证码"

                    return _send

                import types

                client = types.SimpleNamespace(
                    async_login_sms_send=_make("telecom"),
                    async_sms_trigger=_make("unicom"),
                    export_sms_state=lambda: {"k": "v"},
                )
                coordinator._build_relogin_client = lambda: client
                result = await coordinator.async_send_relogin_sms()
                assert result == "sent", (carrier, result)
                assert called, f"{carrier} 未调用发码方法"
            else:
                assert await coordinator.async_send_relogin_sms() == "unsupported"
        finally:
            await coordinator.async_shutdown()
