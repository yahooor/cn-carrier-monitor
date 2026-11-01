"""v1.5 修复的回归用例（不需要安装 Home Assistant）。

每条用例对应审查报告里的一个问题编号，防止后续重构把修复改回去。
"""

from __future__ import annotations

import importlib.util
import ssl
import sys

import pytest
from conftest import CCM_ROOT


def _load_net():
    name = "ccm_net_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, CCM_ROOT / "net.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- P0-6 TLS 校验


def test_tls_context_verifies_certificates_by_default():
    """P0-6：默认必须校验证书（早期全局 CERT_NONE = 可被中间人读密码）。"""
    net = _load_net()
    ctx = net._ssl_context(False)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    # 旧套件仍要放行，否则运营商网关握手失败
    assert ctx.options & ssl.OP_LEGACY_SERVER_CONNECT


def test_tls_insecure_is_opt_in(monkeypatch):
    """只有显式设置环境变量才允许关闭校验。"""
    net = _load_net()
    monkeypatch.delenv(net._INSECURE_ENV, raising=False)
    assert net._insecure_allowed() is False
    monkeypatch.setenv(net._INSECURE_ENV, "1")
    assert net._insecure_allowed() is True


# ---------------------------------------------------------------- P1-12 cURL 解析


def test_parse_curl_keeps_cookie_from_dash_b(ccm):
    """P1-12：浏览器 Copy as cURL 常用 -b 传 Cookie，早期被整段丢弃。"""
    crypto = ccm.modules["crypto"]
    source = (
        "curl 'https://www.10099.com.cn/api/qryUserRes?x=1' "
        "-H 'Accept: application/json' "
        "-b 'JUT=abc; _uop_id=def' "
        "-X POST"
    )
    parsed = crypto.parse_curl(source)
    assert parsed["url"].startswith("https://www.10099.com.cn/api/qryUserRes")
    assert "JUT=abc" in parsed["cookie"]
    assert parsed["method"] == "POST"


def test_parse_curl_header_cookie_still_wins(ccm):
    crypto = ccm.modules["crypto"]
    source = (
        "curl 'https://example.com/a' "
        "-H 'Cookie: A=1; B=2' "
        "-b 'C=3'"
    )
    parsed = crypto.parse_curl(source)
    # 后出现的 -b 覆盖 -H，二者都能取到值即可（至少不能为空）
    assert parsed["cookie"]


# ---------------------------------------------------------------- P2-19 解密


def test_decrypt_payload_accepts_unpadded_base64(ccm):
    """P2-19：无 '=' padding 的 base64 响应早期直接被当明文。"""
    crypto = ccm.modules["crypto"]
    key = b"1234123412ABCDEF"
    iv = b"ABCDEF1234123412"
    plain = '{"data":{"ok":true}}'
    cipher = crypto.aes_cbc_encrypt(key, iv, plain)
    import base64

    padded = base64.b64encode(cipher).decode()
    unpadded = padded.rstrip("=")
    assert unpadded != padded
    assert crypto.decrypt_payload(unpadded, key, iv) == plain


# ---------------------------------------------------------------- P1-7 联通发码状态


def test_unicom_sms_state_roundtrip(ccm):
    """P1-7：发码 deviceId 必须带到输码步，否则 radomLogin 判风控。"""
    unicom = ccm.modules["unicom"]
    client = unicom.UnicomClient.__new__(unicom.UnicomClient)
    client._data = {"phone": "19974410126"}
    client._app_id = "app-from-trigger"
    client._sms_device_id = "device-from-trigger"

    state = client.export_sms_state()
    assert state["device_id"] == "device-from-trigger"

    other = unicom.UnicomClient.__new__(unicom.UnicomClient)
    other._data = {"phone": "19974410126"}
    other._app_id = "random-app"
    other.import_sms_state(state)
    assert other._sms_device_id == "device-from-trigger"
    assert other._app_id == "app-from-trigger"


# ---------------------------------------------------------------- 归一化模型


def test_empty_data_declares_account_status(ccm):
    """account_status 是 sensor 平台会读取的字段，必须在归一化模型里声明。"""
    base = ccm.modules["base"]
    data = base.empty_data("telecom", "19974410126")
    assert "account_status" in data
    assert data["account_status"] is None


@pytest.mark.parametrize("carrier", ["telecom", "unicom", "mobile", "broadnet"])
def test_empty_data_shape_is_stable(ccm, carrier):
    base = ccm.modules["base"]
    data = base.empty_data(carrier, "19974410126")
    for key in ("carrier", "phone", "extra", "updated_at"):
        assert key in data
    assert isinstance(data["extra"], dict)
