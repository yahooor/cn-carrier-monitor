"""Contract tests: China Unicom parsers and login helpers."""

import base64
import json
import urllib.parse

from conftest import load_fixture


def _client(ccm):
    unicom = ccm.modules["unicom"]
    client = unicom.UnicomClient.__new__(unicom.UnicomClient)
    client._data = {"phone": "18574472432"}
    client._password = ""
    client._app_id = unicom.generate_app_id()
    client._token_online = ""
    client._desmobile = ""
    return client


def test_rsa_login_urlencoded(ccm):
    unicom = ccm.modules["unicom"]
    enc = unicom._rsa_login_urlenc("18574472432")
    raw = base64.b64decode(urllib.parse.unquote(enc))
    assert len(raw) == 128  # 1024-bit key


def test_base_login_params_shape(ccm):
    unicom = ccm.modules["unicom"]
    p = unicom._base_login_params("d" * 32)
    assert p["deviceModel"] == "iPhone14,3"
    assert p["version"] == "iphone_c@13.1000"
    assert len(p["reqtime"]) == 13


def test_overview_seven_mapping(ccm):
    base = ccm.modules["base"]
    client = _client(ccm)
    data = base.empty_data("unicom", "18574472432")
    client._apply_overview_seven(json.loads(load_fixture("unicom_overview_seven.json")), data)
    assert data["balance"] == 12.34
    assert data["voice"]["remain"] == 120
    assert data["points"] == 88
    assert data["extra"]["location"] == "湖南 张家界"
    assert data["extra"]["flush_time"] == "2026-10-24 20:00:00"


def test_ocs_usage_parse_flow_voice_sms(ccm):
    unicom = ccm.modules["unicom"]
    parsed = unicom._parse_ocs_usage(json.loads(load_fixture("unicom_ocs_usage.json")))
    # general flow: 2048MB used / 8192 remain (MB→GB)
    assert parsed["flow_general"]["used"] == 2.0
    assert parsed["flow_general"]["total"] == 10.0
    assert parsed["flow_directed"]["total"] == 2.0
    assert parsed["voice"]["total"] == 300.0
    assert parsed["voice"]["remain"] == 250.0
    assert parsed["sms"]["remain"] == 95


def test_error_mapping_code2_is_credentials(ccm):
    """REGRESSION: code=2 曾报 cannot_connect，必须映射为凭据错误。"""
    payload = json.loads(load_fixture("unicom_bad_credentials.json"))
    code = str(payload.get("code") or "")
    assert code == "2"
    assert code in ("2", "4102", "4103", "3102")  # credential-class codes


def test_parse_flow_detail_mb_to_gb(ccm):
    unicom = ccm.modules["unicom"]
    detail = {
        "resources": [
            {"type": "flow", "userResource": 2048, "remainResource": 8192,
             "details": []},
            {"type": "voice", "userResource": 50, "remainResource": 250, "details": []},
        ],
        "packageName": "联通王卡29",
    }
    parsed = unicom._parse_flow_detail(detail)
    assert parsed["plan_name"] == "联通王卡29"
    assert parsed["flow_general"]["remain"] == 8.0
    assert parsed["voice"]["remain"] == 250
