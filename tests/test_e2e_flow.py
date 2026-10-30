"""全链路回放 e2e：运营商客户端 ↔ 本地 mock 服务器（tests/mock_carrier.py）。

每条用例：mock 注册运营商场景路由 → 客户端用真实 aiohttp 走 登录→查询→解析
→ 断言归一化输出与既有 fixture 期望一致、线上请求形态正确（表单/头/token）。
mock 一律绑定 127.0.0.1 随机端口；除 mock 外不发任何真实网络请求。
"""

from __future__ import annotations

import base64
import binascii
import io
import json

import pytest
from conftest import load_fixture
from mock_carrier import client_session, mock_carrier  # noqa: F401 fixtures

PHONE = "19974410126"
TELECOM_TOKEN = "TOKEN_SMS_V2_ab12cd34"
UNICOM_PACKAGE_PATH = "/servicequerybusiness/operationservice/queryOcsPackageFlowLeftContentRevisedInJune"
UNICOM_BALANCE_PATH = "/servicequerybusiness/balancenew/accountBalancenew.htm"


# ------------------------------------------------------------------ telecom


def _slider_images() -> tuple[str, str]:
    """合成滑块图：缺口在 (150,30)-(190,70)，与 test_telecom 的定位用例同构。"""
    from PIL import Image, ImageDraw

    bg = Image.new("L", (300, 80), 220)
    ImageDraw.Draw(bg).rectangle([150, 30, 190, 70], fill=60)
    piece = Image.new("RGBA", (300, 80), (0, 0, 0, 0))
    ImageDraw.Draw(piece).rectangle([150, 30, 190, 70], fill=(255, 255, 255, 255))
    b1, b2 = io.BytesIO(), io.BytesIO()
    bg.save(b1, format="PNG")
    piece.save(b2, format="PNG")
    return base64.b64encode(b1.getvalue()).decode(), base64.b64encode(b2.getvalue()).decode()


def _register_telecom_v2(mock) -> None:
    """电信 v2 短信链（getLoginRandomCode→slider→userLoginNormal）+ v2 查询族。"""

    def get_random_code(rec):
        field = ((rec.json_dict() or {}).get("content") or {}).get("fieldData") or {}
        if field.get("signSignatureString"):
            # 第二次：滑块通过后重新取 code（签发可下发短信的 key）
            return {"responseData": {
                "resultCode": "0000",
                "resultDesc": "验证成功，正在下发短信验证码",
                "data": {"key": "finalkey", "isChinatelecom": "1"},
            }}
        return {"responseData": {
            "resultCode": "0000", "resultDesc": "成功",
            "data": {"key": "prefetchkey", "isChinatelecom": "0"},
        }}

    bg_b64, piece_b64 = _slider_images()
    mock.register("/login/client/getLoginRandomCode", get_random_code, method="POST")
    mock.register("/login/client/getSliderVerificationPicture", (200, {
        "responseData": {"resultCode": "0000", "data": {
            "backgroundImg": bg_b64, "sliderImg": piece_b64,
            "key": "sliderkey",
            "extra": base64.b64encode(b"0123456789abcdef").decode(),
        }}}, ), method="POST")
    mock.register("/login/client/verificationSliderPicture", (200, {
        "responseData": {"resultCode": "0000",
                         "data": {"signSignatureString": "9a9a4be8sim20261031000000"}}}), method="POST")
    mock.register("/login/client/userLoginNormal", (200, {
        "responseData": {"resultCode": "0000", "data": {"loginSuccessResult": {
            "token": TELECOM_TOKEN, "provinceCode": "600101",
            "cityCode": "8441900", "userId": "u12345"}}}}), method="POST")

    mock.register("/query/queryPhoneBillBalance", (200, {"headerInfos": {"code": "0000"}, "responseData": {
        "resultCode": "0000",
        "data": {
            "chargeBean": {"chargeTitle": "当前余额（元）", "charge": "45.67", "isShowRed": "0"},
            "totalArrears": "0.00",
            "billExpense": {"valueList": [
                {"month": "202609", "amount": "58.00"},
                {"month": "202610", "amount": "39.90"},
            ]}}}}), method="POST")
    mock.register("/query/queryIntegral", (200, {"headerInfos": {"code": "0000"}, "responseData": {
        "resultCode": "0000", "data": {"integral": "1234"}}}), method="POST")
    mock.register("/query/userPackage", (200, {"headerInfos": {"code": "0000"}, "responseData": {
        "resultCode": "0000",
        "data": {"productOFFRatable": {"ratableResourcePackages": [{
            "usageAmount": "1572864", "balanceAmount": "2621440",
            "productInfos": [
                {"productOFFName": "通用流量包", "usageAmount": "1048576",
                 "balanceAmount": "2097152", "ratableAmount": "3145728"},
                {"productOFFName": "定向流量包", "usageAmount": "524288",
                 "balanceAmount": "524288", "ratableAmount": "1048576"},
            ]}]}}}}), method="POST")
    mock.register("/query/qryUserUsage", (200, {"headerInfos": {"code": "0000"}, "responseData": {
        "resultCode": "0000",
        "data": {
            "productOFFInformation": {"content": "畅越冰激凌29元"},
            "ratableResourcePackages": [{
                "usageProgress": {"leftBottomTitle": "100", "rightBottomTitle": "300"},
                "totalData": {"totalValue": "200"}}]}}}), method="POST")


async def test_telecom_sms_login_and_v2_fetch_e2e(ccm, mock_carrier, client_session, monkeypatch):
    """完整短信链登录 + v2 查询族 → async_fetch 归一化输出与凭证 token。"""
    telecom = ccm.modules["telecom"]
    monkeypatch.setattr(telecom, "TELECOM_LOGIN_V2_HOSTS", [mock_carrier.base_url])
    monkeypatch.setattr(telecom, "TELECOM_V2_QUERY_HOST", mock_carrier.base_url)
    _register_telecom_v2(mock_carrier)

    login_client = telecom.TelecomClient(client_session, {"phone": PHONE, "auth_mode": "sms"})
    assert await login_client.async_login_sms_send() is True
    creds = await login_client.async_login_sms_submit("123456")
    assert creds["phone"] == PHONE
    assert creds["token"] == TELECOM_TOKEN  # 凭证导出含 token
    assert login_client.export_sms_state()["key"] == "finalkey"

    client = telecom.TelecomClient(client_session, {
        "phone": PHONE, "auth_mode": "sms", "token": creds["token"],
        "province_code": creds["province_code"], "city_code": creds["city_code"],
        "user_id": "u12345"})
    data = await client.async_fetch()

    assert data["carrier"] == "telecom" and data["phone"] == PHONE
    assert data["balance"] == 45.67
    assert data["month_cost"] == 39.9
    assert data["points"] == 1234
    assert data["flow_general"] == {"used": 1.0, "total": 3.0, "remain": 2.0}
    assert data["flow_directed"] == {"used": 0.5, "total": 1.0, "remain": 0.5}
    assert data["flow_total"] == {"used": 1.5, "total": 4.0, "remain": 2.5}
    assert data["voice"] == {"used": 100.0, "total": 300.0, "remain": 200.0}
    assert data["plan_name"] == "畅越冰激凌29元"
    assert data["extra"]["balance_title"] == "当前余额（元）"
    assert {i["name"] for i in data["extra"]["flow_items"]} == {"通用流量包", "定向流量包"}
    assert data["updated_at"]

    # 登录链顺序：randomCode → 滑块图 → 验证滑块 → randomCode(带签) → userLoginNormal
    seen = [r.path for r in mock_carrier.requests]
    assert seen[:5] == [
        "/login/client/getLoginRandomCode",
        "/login/client/getSliderVerificationPicture",
        "/login/client/verificationSliderPicture",
        "/login/client/getLoginRandomCode",
        "/login/client/userLoginNormal",
    ]
    login_call = mock_carrier.calls("/login/client/userLoginNormal")[0]
    body = login_call.json_dict()
    assert body["content"]["fieldData"]["loginType"] == "2"
    assert body["headerInfos"]["source"] == "120002"
    # 滑块轨迹用 extra 里的 key 做了 AES-ECB（密文非明文）
    vp_call = mock_carrier.calls("/login/client/verificationSliderPicture")[0]
    vp_field = vp_call.json_dict()["content"]["fieldData"]
    assert vp_field["key"] == "sliderkey"
    assert vp_field["distance"] != "0.5769" and len(vp_field["distance"]) > 20

    # v2 查询族：token / source / shopId / caesar 账号逐请求核对
    caesar = ccm.modules["crypto"].caesar
    for path in ("/query/queryPhoneBillBalance", "/query/userPackage",
                 "/query/qryUserUsage", "/query/queryIntegral"):
        recs = mock_carrier.calls(path)
        assert len(recs) == 1, path
        payload = recs[0].json_dict()
        hdr = payload["headerInfos"]
        assert hdr["token"] == TELECOM_TOKEN, path
        assert hdr["source"] == "120002" and hdr["shopId"] == "20004", path
        assert hdr["userLoginName"] == caesar(PHONE), path
    bal_call = mock_carrier.calls("/query/queryPhoneBillBalance")[0]
    assert bal_call.json_dict()["content"]["fieldData"]["account"] == caesar(PHONE)


async def test_telecom_v2_session_expired_e2e(ccm, mock_carrier, client_session, monkeypatch):
    """v2 查询返回 X201 → 必须抛 CarrierAuthError（而不是崩或给空数据）。"""
    telecom = ccm.modules["telecom"]
    monkeypatch.setattr(telecom, "TELECOM_V2_QUERY_HOST", mock_carrier.base_url)
    mock_carrier.register_json("/query/queryPhoneBillBalance",
                               {"headerInfos": {"code": "X201", "reason": "token 过期"},
                                "responseData": {}}, method="POST")
    client = telecom.TelecomClient(client_session, {
        "phone": PHONE, "auth_mode": "sms", "token": "expired-token"})
    with pytest.raises(ccm.modules["base"].CarrierAuthError):
        await client.async_fetch()
    rec = mock_carrier.calls("/query/queryPhoneBillBalance")[0]
    assert rec.json_dict()["headerInfos"]["token"] == "expired-token"


# ------------------------------------------------------------------ unicom


def _unicom_wt_payload(package_name: str = "腾讯王卡5G") -> dict:
    """unicom_ocs_usage.json 结构 + WT 渠道顶层汇总字段。"""
    payload = json.loads(load_fixture("unicom_ocs_usage.json"))
    payload.update({
        "packageName": package_name,
        "voiceSumresource": "300", "voiceHeadUsed": "50",
        "canUseSmsAll": "100", "smsHeadUsed": "5",
        "leftMonth": "3",
        "canUseFlowAll": "6.4", "canuseFlowAllUnit": "GB",
        "flowExceed": "0",
    })
    return payload


def _register_unicom_wt(mock, package_payload=None) -> None:
    mock.register_json(UNICOM_PACKAGE_PATH, package_payload or _unicom_wt_payload(), method="POST")
    mock.register_json(UNICOM_BALANCE_PATH, {
        "canusefeecustNew": "45.67", "totalrealfee": "39.90",
        "curntbalancecust": "45.67", "allbowefeecust": "0.00",
        "canuselimitcust": "200.00", "directionalPsntFeeTotal": "10.00",
    }, method="POST")


async def test_unicom_cookie_wt_e2e(ccm, mock_carrier, client_session, monkeypatch):
    """UnicomCookieClient(WT)：package+balance 双查询 → 归一化输出与 version=WT 表单。"""
    unicom = ccm.modules["unicom"]
    monkeypatch.setattr(unicom, "UNICOM_PACKAGE_URL", mock_carrier.base_url + UNICOM_PACKAGE_PATH)
    monkeypatch.setattr(unicom, "UNICOM_BALANCE_URL", mock_carrier.base_url + UNICOM_BALANCE_PATH)
    _register_unicom_wt(mock_carrier)

    cookie = "JSESSIONID=wt-0001; uni_cookie=WTSESS-ABC"
    client = unicom.UnicomCookieClient(client_session, {"phone": "18574472432", "cookies": cookie})
    data = await client.async_fetch()

    assert data["carrier"] == "unicom"
    assert data["balance"] == 45.67
    assert data["month_cost"] == 39.9
    assert data["plan_name"] == "腾讯王卡5G"
    assert data["flow_general"] == {"used": 2.0, "total": 10.0, "remain": 8.0}
    assert data["flow_directed"] == {"used": 1.0, "total": 2.0, "remain": 1.0}
    assert data["flow_total"] == {"used": 3.0, "total": 12.0, "remain": 9.0}
    assert data["voice"] == {"used": 50.0, "total": 300.0, "remain": 250.0}
    assert data["sms"] == {"used": 5.0, "total": 100.0, "remain": 95.0}
    assert data["extra"]["left_month"] == "3"
    assert data["extra"]["flow_all"] == "6.4" and data["extra"]["flow_all_unit"] == "GB"
    assert data["extra"]["flow_exceed"] == "0"
    assert data["extra"]["credit_limit"] == 200.0 and data["extra"]["owed"] == 0.0
    assert data["updated_at"]

    rec = mock_carrier.calls(UNICOM_PACKAGE_PATH, method="POST")[0]
    assert rec.form_dict()["version"] == ["WT"]  # 网页票据渠道标识
    assert rec.headers["cookie"] == cookie       # 显式 Cookie 原样携带
    assert "yuliangchaxunsfwt" in rec.headers["referer"]
    assert rec.headers["content-type"] == "application/x-www-form-urlencoded"
    bal_rec = mock_carrier.calls(UNICOM_BALANCE_PATH, method="POST")[0]
    assert bal_rec.form_dict()["version"] == ["WT"]
    assert bal_rec.headers["cookie"] == cookie


async def test_unicom_cookie_session_expired_e2e(ccm, mock_carrier, client_session, monkeypatch):
    """未认证基线形态 "999999" → CarrierAuthError。"""
    unicom = ccm.modules["unicom"]
    monkeypatch.setattr(unicom, "UNICOM_PACKAGE_URL", mock_carrier.base_url + UNICOM_PACKAGE_PATH)
    mock_carrier.register(UNICOM_PACKAGE_PATH, "999999", method="POST")
    client = unicom.UnicomCookieClient(client_session, {"phone": "18574472432", "cookies": "sid=x"})
    with pytest.raises(ccm.modules["base"].CarrierAuthError):
        await client.async_fetch()


async def test_unicom_two_accounts_no_cookie_crosstalk(ccm, mock_carrier, client_session, monkeypatch):
    """同一 mock、同一 ClientSession 下两个账号交替请求：Cookie 互不串扰。"""
    unicom = ccm.modules["unicom"]
    monkeypatch.setattr(unicom, "UNICOM_PACKAGE_URL", mock_carrier.base_url + UNICOM_PACKAGE_PATH)
    monkeypatch.setattr(unicom, "UNICOM_BALANCE_URL", mock_carrier.base_url + UNICOM_BALANCE_PATH)

    cookie_a = "acctA=1; sid=AAA"
    cookie_b = "acctB=2; sid=BBB"

    def package_handler(rec):
        cookie = rec.headers.get("cookie", "")
        if "acctA" in cookie:
            return _unicom_wt_payload("账户A套餐")
        if "acctB" in cookie:
            return _unicom_wt_payload("账户B套餐")
        return _unicom_wt_payload("未知账户")

    mock_carrier.register(UNICOM_PACKAGE_PATH, package_handler, method="POST")
    mock_carrier.register_json(UNICOM_BALANCE_PATH, {"canusefeecustNew": "1.00"}, method="POST")

    a = unicom.UnicomCookieClient(client_session, {"phone": "18500000001", "cookies": cookie_a})
    b = unicom.UnicomCookieClient(client_session, {"phone": "18500000002", "cookies": cookie_b})
    plans = []
    for _ in range(3):
        plans.append((await a.async_fetch())["plan_name"])
        plans.append((await b.async_fetch())["plan_name"])

    assert plans == ["账户A套餐", "账户B套餐"] * 3

    pkg_calls = mock_carrier.calls(UNICOM_PACKAGE_PATH, method="POST")
    assert len(pkg_calls) == 6
    for i, rec in enumerate(pkg_calls):
        expected = cookie_a if i % 2 == 0 else cookie_b
        assert rec.headers.get("cookie") == expected, f"第{i}次请求 Cookie 串扰"
        cookie = rec.headers.get("cookie", "")
        assert not ("AAA" in cookie and "BBB" in cookie)
    # mock 从不下发 Set-Cookie：会话 jar 必须保持为空（无隐式共享）
    assert len(client_session.cookie_jar) == 0


# ------------------------------------------------------------------ mobile


async def test_mobile_wx_channel_e2e(ccm, mock_carrier, client_session, monkeypatch):
    """移动 wx 网厅通道：AES-hex 加密响应现场加密重放 → 归一化输出与请求形态。"""
    mobile = ccm.modules["mobile"]
    crypto = ccm.modules["crypto"]

    def enc_hex(payload: dict) -> str:
        plain = json.dumps(payload, ensure_ascii=False)
        return crypto.aes_cbc_encrypt(mobile.WX_AES_KEY, mobile.WX_AES_IV, plain).hex()

    monkeypatch.setattr(mobile, "_curl_requests", None)  # 强制 aiohttp 路径，不依赖 curl_cffi
    monkeypatch.setattr(mobile, "MOBILE_PLAN_URL", mock_carrier.base_url + "/website/serviceMargin/getMainPlan")
    monkeypatch.setattr(mobile, "MOBILE_FEE_URL_V2", mock_carrier.base_url + "/website/fareBalance")
    monkeypatch.setattr(mobile, "MOBILE_MARGIN_URL_V2", mock_carrier.base_url + "/website/uniApi/getNewMarginInfo")
    monkeypatch.setattr(mobile, "MOBILE_MARGIN_QRY_URL_V2",
                        mock_carrier.base_url + "/website/uniApi/getMarginQueryInfo")
    monkeypatch.setattr(mobile, "MOBILE_SCORE_URL", mock_carrier.base_url + "/website/score/news")

    cookie = "JSESSIONID=m-0001; cmccssotoken=SSO-XYZ"
    mock_carrier.register("/website/serviceMargin/getMainPlan", lambda rec: enc_hex({
        "object": {"resultData": {"curPlanName": "8元自由选套餐", "curPlanPrice": "8元/月"}}}), method="GET")
    mock_carrier.register("/website/fareBalance", lambda rec: enc_hex({
        "data": {"realFeeQryRsp": {
            "curFeeTotal": "229.31", "realFee": "18.75", "oweFee": "0.00",
            "payType": "1", "userSpecBalance": "229.31"}}}), method="GET")
    margin_payload = json.loads(load_fixture("mobile_wx_margin.json"))
    mock_carrier.register("/website/uniApi/getNewMarginInfo",
                          lambda rec: enc_hex(margin_payload), method="POST")
    mock_carrier.register("/website/uniApi/getMarginQueryInfo", lambda rec: enc_hex({
        "data": {"resultData": {"customerAssignment": "湖南 张家界",
                                "tariffPackages": "8元自由选套餐"}}}), method="POST")
    mock_carrier.register_json("/website/score/news", {"qryInfoRsp": {"pointValue": "1234"}}, method="GET")

    client = mobile.MobileClient(client_session, {
        "phone": "17267445980", "auth_mode": "cookie", "cookies": cookie})
    data = await client.async_fetch()

    assert data["carrier"] == "mobile"
    assert data["balance"] == 229.31
    assert data["month_cost"] == 18.75
    assert data["plan_name"] == "8元自由选套餐"
    assert data["plan_price"] == "8元/月"
    assert data["points"] == 1234.0
    assert data["location"] == "湖南 张家界"
    assert data["flow_general"] == {"used": 1.0, "total": 10.0, "remain": 9.0}
    assert data["flow_directed"] == {"used": 0.1, "total": 1.0, "remain": 0.9}
    assert data["flow_total"] == {"used": 1.1, "total": 11.0, "remain": 9.9}
    assert data["voice"] == {"used": 10.0, "total": 100.0, "remain": 90.0}
    assert data["sms"] == {"used": 5.0, "total": 50.0, "remain": 45.0}
    assert data["extra"]["owed"] == 0.0
    assert data["extra"]["pay_type"] == 1.0
    assert data["extra"]["user_spec_balance"] == 229.31
    assert data["updated_at"]

    # uniApi POST 形态（已知 bug：uni_headers 因 _cffi_request 未定义而丢失，mock 不校验）
    margin_calls = mock_carrier.calls("/website/uniApi/getNewMarginInfo", method="POST")
    assert margin_calls and margin_calls[0].json_dict() == {"operation": "getCustomerKeyMessage"}
    assert margin_calls[0].headers["content-type"].startswith("application/json")
    # 加密响应确实被解开（mock 收到的请求带的 Cookie 原样透传）
    assert len(mock_carrier.calls("/website/score/news")) == 1
    for rec in mock_carrier.requests:
        assert rec.headers.get("cookie") == cookie


# ------------------------------------------------------------------ broadnet


async def test_broadnet_qry_user_res_replay_e2e(ccm, mock_carrier, client_session):
    """广电 qryUserRes 明文重放 → userResList 全量解析。"""
    broadnet = ccm.modules["broadnet"]
    payload = json.loads(load_fixture("broadnet_user_res.json"))
    mock_carrier.register_json("/api/qryUserRes", payload, method="GET")

    cookie = "JSESSIONID=bn-0001; sid=BNSESSION"
    client = broadnet.BroadnetClient(client_session, {
        "phone": "19212345678",
        "request_url": mock_carrier.base_url + "/api/qryUserRes",
        "request_method": "GET",
        "request_body": "",
        "cookies": cookie})
    data = await client.async_fetch()

    assert data["carrier"] == "broadnet"
    assert data["flow_general"] == {"used": 30.0, "total": 50.0, "remain": 20.0}
    assert data["flow_directed"] == {"used": 0.0, "total": 10.0, "remain": 10.0}
    assert data["flow_total"] == {"used": 30.0, "total": 60.0, "remain": 30.0}
    assert data["voice"] == {"used": 20.0, "total": 100.0, "remain": 80.0}
    assert data["sms"] == {"used": 70.0, "total": 100.0, "remain": 30.0}
    assert data["extra"]["flow_unlimited"] == ["不限量流量"]
    assert len(data["extra"]["flow_items"]) == 3
    assert data["updated_at"]

    rec = mock_carrier.calls("/api/qryUserRes", method="GET")[0]
    assert rec.headers["cookie"] == cookie
    assert rec.headers["referer"] == "https://www.10099.com.cn/"
    assert rec.method == "GET"


async def test_broadnet_aes_hex_response_e2e(ccm, mock_carrier, client_session):
    """广电 AES-CBC(hex) 加密响应（密钥与移动一致）→ 客户端自动解密。"""
    broadnet = ccm.modules["broadnet"]
    crypto = ccm.modules["crypto"]
    payload = json.loads(load_fixture("broadnet_user_res.json"))
    ct = crypto.aes_cbc_encrypt(
        broadnet.AES_KEY, broadnet.AES_IV, json.dumps(payload, ensure_ascii=False))
    mock_carrier.register("/api/qryUserRes", binascii.hexlify(ct).decode(), method="GET")

    client = broadnet.BroadnetClient(client_session, {
        "phone": "19212345678",
        "request_url": mock_carrier.base_url + "/api/qryUserRes",
        "request_method": "GET",
        "request_body": "",
        "cookies": "sid=BNSESSION"})
    data = await client.async_fetch()

    assert data["flow_general"]["total"] == 50.0
    assert data["voice"]["remain"] == 80
    assert data["extra"]["flow_unlimited"] == ["不限量流量"]
