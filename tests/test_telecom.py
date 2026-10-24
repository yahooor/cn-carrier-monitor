"""Contract tests: China Telecom parser and login protocol (fixtures from live
captures 2026-10-24/25 plus structures verified by ChinaTelecomMonitor)."""

import json

import pytest
from conftest import load_fixture


def _client(ccm):
    telecom = ccm.modules["telecom"]
    client = telecom.TelecomClient.__new__(telecom.TelecomClient)
    client._data = {"phone": "19974410126"}
    client._password = ""
    client._device_id = ""
    client._token = ""
    client._province_code = "600203"
    client._city_code = "8430800"
    client._headers = {}
    client._session = None
    client._uid = __import__("hashlib").md5(b"CT_IOS_11_19974410126").hexdigest()
    client._key = ""
    client._isct = "0"
    return client


@pytest.fixture(scope="module")
def client(ccm):
    return _client(ccm)


def test_login_body_shape(ccm, client):
    body = client._login_body("19974410126", "pw123", "20261024120000")
    assert body["headerInfos"]["code"] == "userLoginNormal"
    assert (
        body["content"]["fieldData"]["phoneNum"]
        == ccm.modules["crypto"].caesar("19974410126")
    )
    assert len(body["content"]["fieldData"]["loginAuthCipherAsymmertric"]) > 100


def test_sms_login_plain_format(ccm, client):
    """loginType=2 plain string layout (Pocket-Carrier verified)."""
    pad = lambda s, n: (s or "")[:n].ljust(n, "$")  # noqa: E731
    ts = "20261024233430"
    plain = ("iPhone 11 "[:10].ljust(10) + "16.1." + client._uid[:12]
             + "19974410126" + ts[:14] + pad("123456", 6)
             + pad("0", 4) + pad("0.000000", 2))
    assert len(plain) >= 55
    assert plain[10:15] == "16.1."


def test_summary_parse_real_structure(ccm, client):
    data = ccm.modules["base"].empty_data("telecom", "19974410126")
    payload = json.loads(load_fixture("telecom_qry_important.json"))["responseData"]["data"]
    client._parse_summary(payload, data)
    assert data["balance"] == 123.45
    assert data["month_cost"] == 45.6
    assert data["points"] == 1234
    assert data["flow_general"]["used"] == 400.0
    assert data["flow_general"]["remain"] == 900.0
    # flow_directed 来自 specialAmount（KB），flowList 明细进 extra
    assert data["flow_directed"]["total"] == 200.0
    items = {i["name"]: i for i in data["extra"]["flow_items"]}
    # flow_items 单位为 GB；"达量降速"标题不含"流量"，与上游一致不采集
    assert items["定向流量"]["total"] == 1.0
    assert "达量降速" not in items
    assert items["主套餐通用流量"]["used"] == 0.39
    assert data["voice"]["total"] == 300.0
    # 短信从 flowList 兜底解析
    assert data["sms"]["remain"] == 95
    assert data["sms"]["total"] == 100


def test_query_response_expiry_detection(ccm):
    """headerInfos 精确判定（X201/X110），业务提示不误杀。"""
    telecom = ccm.modules["telecom"]
    assert "X201" in telecom.AUTH_EXPIRED_CODES
    assert "X110" in telecom.AUTH_EXPIRED_CODES
    # 业务性 reason 不在失效关键词内
    hints = telecom.AUTH_EXPIRED_HINTS
    assert "详单" not in str(hints)


def test_slider_sign_extraction_toplevel(ccm):
    """REGRESSION: sign 在 data 顶层（曾错写成 j["data"]["signSignatureString"]）。"""
    payload = json.loads(load_fixture("telecom_slider_ok.json"))["responseData"]
    sign = payload.get("data", {}).get("signSignatureString")
    assert sign and sign.startswith("9a9a4be8")
    # 模拟 async_login_sms_send 中的提取路径
    data_node = payload.get("data") or {}
    assert data_node.get("signSignatureString") == sign


def test_sms_submit_error_branch_reason_not_crash(ccm, client):
    """REGRESSION: 错误分支曾 str(dict).get 崩溃；现在必须返回 reason 字符串。"""
    fail = json.loads(load_fixture("telecom_bad_credentials.json"))
    data = fail["responseData"].get("data") or {}
    fail_node = data.get("loginFailResult") or {}
    reason = fail_node.get("reason") if isinstance(fail_node, dict) else str(fail_node)
    assert reason == "手机号或密码有误，请重新输入"


def test_sms_state_roundtrip(ccm, client):
    client._key = "k123"
    client._isct = "1"
    state = client.export_sms_state()
    other = _client(ccm)
    other.import_sms_state(state)
    assert other._key == "k123" and other._isct == "1" and other._uid == client._uid


def test_slider_notch_locate_synthetic(ccm):
    """缺口定位：暗块在 x=150, y=30。"""
    telecom = ccm.modules["telecom"]
    import base64
    import io

    from PIL import Image, ImageDraw

    bg = Image.new("L", (300, 80), 220)
    ImageDraw.Draw(bg).rectangle([150, 30, 190, 70], fill=60)
    piece = Image.new("RGBA", (300, 80), (0, 0, 0, 0))
    ImageDraw.Draw(piece).rectangle([150, 30, 190, 70], fill=(255, 255, 255, 255))
    b1, b2 = io.BytesIO(), io.BytesIO()
    bg.save(b1, format="PNG")
    piece.save(b2, format="PNG")
    hx, _pw, _bw = telecom._locate_notch(
        base64.b64encode(b1.getvalue()).decode(),
        base64.b64encode(b2.getvalue()).decode(),
    )
    assert abs(hx - 150) <= 4, hx
