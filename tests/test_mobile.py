"""Contract tests: China Mobile — 12.5 native protocol + wx web-hall fallback."""

import base64
import json

from conftest import load_fixture


def _client(ccm):
    mobile = ccm.modules["mobile"]
    client = mobile.MobileClient.__new__(mobile.MobileClient)
    client._data = {"phone": "17267445980"}
    client._mode = "cookie"
    client._password = ""
    client._cookie = ""
    client._device_id = mobile.device_identity("17267445980")
    client._session = None
    client._prov = "451"
    client._city = "0459"
    return client


def test_native_aes_b64_roundtrip(ccm):
    """REGRESSION: 请求体/令牌是 base64(AES)（曾直接密文 .decode 崩溃）。"""
    mobile = ccm.modules["mobile"]
    crypto = ccm.modules["crypto"]
    env = json.dumps({"tel": "17267445980"}, separators=(",", ":"))
    tok = mobile._native_aes_b64(mobile.NATIVE_REQ_KEY, mobile.NATIVE_IV, env)
    assert crypto.aes_cbc_decrypt(
        mobile.NATIVE_REQ_KEY, mobile.NATIVE_IV, base64.b64decode(tok)
    ).decode() == env


def test_x_token_format(ccm):
    mobile = ccm.modules["mobile"]
    tok = mobile._native_aes_b64(
        mobile.NATIVE_REQ_KEY, mobile.NATIVE_IV,
        f"{mobile._ENVELOPE_DEFAULTS['xk']}_/e_1700000000000_12345678")
    assert len(base64.b64decode(tok)) % 16 == 0


def test_native_plan_real_structure(ccm):
    """真实字段（planRemianFlowRes/unit 02KB 03MB 04GB）；通用排除定向。"""
    client = _client(ccm)
    base = ccm.modules["base"]
    data = base.empty_data("mobile", "17267445980")
    client._parse_native_plan(json.loads(load_fixture("mobile_native_plan.json")), data)
    # 通用: 8GB remain / 10GB total（MB→GB）；定向 2GB 单列；闲时(KB unit=02) 768KB
    assert data["flow_general"]["total"] == 10.0
    assert data["flow_general"]["remain"] == 8.0
    assert data["flow_directed"]["total"] == 2.0
    # 全量含定向与闲时
    assert data["flow_total"]["total"] > data["flow_general"]["total"]
    assert data["voice"]["remain"] == 250.0


def test_native_fee_fields(ccm):
    fee = json.loads(load_fixture("mobile_native_fee.json"))["rspBody"]
    base = ccm.modules["base"]
    assert base.to_float(fee.get("curFeeTotal")) == 229.31
    assert base.to_float(fee.get("realFee")) == 18.75


def test_wx_margin_units(ccm):
    """wx 网厅: unit 03=MB→GB, 01=分钟, 02=条。"""
    client = _client(ccm)
    base = ccm.modules["base"]
    data = base.empty_data("mobile", "17267445980")
    client._parse_margin(json.loads(load_fixture("mobile_wx_margin.json")), data)
    assert data["flow_general"]["total"] == 10.0
    assert data["flow_general"]["used"] == 1.0
    assert data["flow_directed"]["total"] == 1.0
    assert data["voice"]["remain"] == 90.0
    assert data["sms"]["remain"] == 45.0


def test_login_error_hint_mapping(ccm):
    mobile = ccm.modules["mobile"]
    assert mobile.LOGIN_ERROR_HINTS["6001"] == "mobile_code_invalid"
    assert mobile.LOGIN_ERROR_HINTS["8002"] == "mobile_password_error"


def test_expired_codes_contract(ccm):
    mobile = ccm.modules["mobile"]
    for code in ("410000", "400001", "6001"):
        assert code in mobile.NATIVE_AUTH_EXPIRED_CODES
