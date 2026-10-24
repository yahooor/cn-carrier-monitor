"""Contract tests: China Broadnet userResList parsing + session replay."""

import json

import pytest
from conftest import load_fixture


def _client(ccm):
    broadnet = ccm.modules["broadnet"]
    client = broadnet.BroadnetClient.__new__(broadnet.BroadnetClient)
    client._data = {"phone": "19212345678"}
    client._url = "https://www.10099.com.cn/api/qryUserRes"
    client._method = "GET"
    client._body = ""
    client._cookie = "sid=1"
    client._session = None
    return client


def test_user_res_parse_kb_units_and_unlimited(ccm):
    client = _client(ccm)
    base = ccm.modules["base"]
    data = base.empty_data("broadnet", "19212345678")
    business = json.loads(load_fixture("broadnet_user_res.json"))["data"]
    client._parse_user_res(business, data)
    # balance=剩余KB 20480/51200 -> 20/50GB
    assert data["flow_general"]["total"] == 50.0
    assert data["flow_general"]["remain"] == 20.0
    assert data["flow_directed"]["total"] == 10.0
    # busiType 1 语音: balance 80 剩余
    assert data["voice"]["remain"] == 80
    # 短信包按名称识别
    assert data["sms"]["remain"] == 30
    # 不限量标记
    assert data["extra"]["flow_unlimited"] == ["不限量流量"]


def test_decode_body_plain_json(ccm):
    broadnet = ccm.modules["broadnet"]
    payload = broadnet._decode_body(load_fixture("broadnet_session_expired.json"))
    assert payload["status"] == "701"


def test_decode_body_hex_aes(ccm):
    import binascii

    broadnet = ccm.modules["broadnet"]
    crypto = ccm.modules["crypto"]
    body = json.dumps({"status": "000000", "data": {}})
    ct = crypto.aes_cbc_encrypt(broadnet.AES_KEY, broadnet.AES_IV, body)
    assert broadnet._decode_body(binascii.hexlify(ct).decode())["status"] == "000000"


def test_parse_import_requires_cookie(ccm):
    broadnet = ccm.modules["broadnet"]
    curl = "curl 'https://www.10099.com.cn/api/qryUserRes' -H 'Cookie: sid=1'"
    norm = broadnet.BroadnetClient.parse_import(curl, "")
    assert norm["request_url"].startswith("https://www.10099.com.cn")
    assert norm["cookies"] == "sid=1"
    with pytest.raises(Exception):
        broadnet.BroadnetClient.parse_import("https://www.10099.com.cn/x", "")
