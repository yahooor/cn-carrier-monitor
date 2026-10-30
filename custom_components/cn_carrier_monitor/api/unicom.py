"""China Unicom (中国联通) client.

两条鉴权链路：
- password: 手机营业厅 App 协议。POST m.client.10010.com/mobileService/login.htm
  （RSA-PKCS1v15(值+6位随机数字)，公钥硬编码于联通 App），返回 token_online 与
  会话 Cookie；查询接口仅依赖 Cookie；会话失效(999998/999999)时用
  onLine.htm(token_online+appId) 续期。
- openid: 微信小程序链路（mina.10010.com），从既有集成移植，作为无服务密码时
  的兜底。
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.parse
from typing import Any

import aiohttp

from ..const import (
    UNICOM_BALANCE_URL,
    UNICOM_LOGIN_URL,
    UNICOM_MINI_APPID,
    UNICOM_MINI_BALANCE_BROADCAST,
    UNICOM_MINI_FLOW_URL,
    UNICOM_MINI_GET_TICKET,
    UNICOM_MINI_GET_TOKEN,
    UNICOM_MINI_GOODS,
    UNICOM_MINI_OVERVIEW,
    UNICOM_MINI_SERVICE_ENTRANCE,
    UNICOM_ONLINE_URL,
    UNICOM_PACKAGE_URL,
    UNICOM_UA,
    UNICOM_VERSION,
)
from .base import (
    CarrierAuthError,
    CarrierClient,
    CarrierError,
    empty_data,
    finish,
    to_float,
    usage_block,
)
from .crypto import rsa_encrypt_b64

_LOGGER = logging.getLogger(__name__)

# 联通 App 登录 RSA 公钥（SPKI/DER，base64）
UNICOM_LOGIN_PUBKEY = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDc+CZK9bBA9IU+gZUOc6FUGu7yO9WpTNB0PzmgFBh96Mg1WrovD1oqZ+eIF4LjvxKXGOdI79JRdve9NPhQo07+uqGQgE4imwNnRx7PFtCRryiIEcUoavuNtuRVoBAm6qdB0SrctgaqGfLgKvZHOnwTjyNqjBUxzMeQlEC2czEMSwIDAQAB"
)

# 小程序接口 RSA 公钥（openid 字段必须 RSA 加密）
UNICOM_MINI_PUBKEY = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA7vCN/AU3QMTuRcQoOsJP1fo6LbU++DxS1QQbgYrkmatPbb7Hactr7O"
    "cTLWYa/ZoOUNuYeCFQtrJ8P8YIDASn2wjIwFteCIdOeWMUcKahdaNvqiS40epA2jFSiC/4hwZXDFNlPtrWsllcCtVFPV1bEGh3"
    "rYKQNI/ZZQVMKzuccSV0BwIC/EjSwaPY1p7x7ACki5V3VPwBut2xkmVDsJDKrgwBDeevpZHKKdHJKMUV8S9NbO7Mq4MbprnTMw"
    "Gt69no7KP2P38Mh+FMbMBIaIolggjjudziyWE9HdaJQiY9cWb9PFJzwY3/nxDHU5nBOeTI/FEo3sWrKWLEdFILIlxGmQIDAQAB"
)

_MINI_REFERER = f"https://servicewechat.com/{UNICOM_MINI_APPID}/494/page-frame.html"
_MINI_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 MicroMessenger/7.0"
)
MINI_HEADERS_JSON = {
    "Content-Type": "application/json",
    "User-Agent": _MINI_UA,
    "xweb_xhr": "1",
    "X-Tingyun": "c=M|4Nl_NnGbjwY",
    "Accept": "*/*",
    "Referer": _MINI_REFERER,
}
MINI_HEADERS_FORM = {
    "Content-Type": "application/x-www-form-urlencoded",
    "User-Agent": _MINI_UA,
    "xweb_xhr": "1",
    "Accept": "application/json, text/plain, */*",
    "Referer": _MINI_REFERER,
}

APPID_TEMPLATE = (
    "{d}f{d}af{d}{d}ad{d}912d306b5053abf90c7ebbb695887bc"
    "870ae0706d573c348539c26c5c0a878641fcc0d3e90acb9be1e6ef858a"
    "59af546f3c826988332376b7d18c8ea2398ee3a9c3db947e2471d32a49{d}{d}"
)

# 联通 H5 登录风控链路（Pocket-Carrier）
UNICOM_LOGIN_WEB = "https://m.client.10010.com/login-web"
UNICOM_APP_UA = "ChinaUnicom4.x/13.1 CFNetwork/1410 iOS/17.0 unicom{version:iphone_c@13.1000}"
UNICOM_RANDOM_LOGIN_URL = "https://m.client.10010.com/mobileService/radomLogin.htm"
# 首页七宫格概览（Pocket-Carrier 验证：余额/流量/语音/积分一次拿全）
UNICOM_USERINFO_SEVEN_URL = (
    "https://m.client.10010.com/mobileserviceimportant/home/queryUserInfoSeven"
)
UNICOM_SEND_RANDOM_URL = "https://m.client.10010.com/mobileService/sendRadomNum.htm"


def _rand_digit() -> str:
    return str(random.randint(0, 9))


def _base_login_params(device_id: str) -> dict[str, str]:
    return {
        "version": "iphone_c@13.1000", "currentVersion": "iphone_c@13.1000",
        "deviceId": device_id, "deviceCode": device_id,
        "deviceModel": "iPhone14,3", "deviceOS": "ios16.0",
        "netWay": "Wifi", "deviceBrand": "iPhone", "pip": "127.0.0.1",
        "reqtime": str(int(time.time() * 1000)),
    }


def _rsa_login_urlenc(value: str) -> str:
    """App 登录族 RSA → base64 → urlencode（Pocket-Carrier rsa_old）。"""
    return urllib.parse.quote(
        rsa_encrypt_b64(UNICOM_LOGIN_PUBKEY, value), safe=""
    )


def generate_app_id() -> str:
    """Device-stable appId in the format observed in the official app."""
    return APPID_TEMPLATE.format(d=_rand_digit())


def _rsa_login(value: str) -> str:
    """App login RSA: value + 6 random digits, PKCS1v15, base64."""
    suffix = "".join(_rand_digit() for _ in range(6))
    return rsa_encrypt_b64(UNICOM_LOGIN_PUBKEY, value + suffix)


def _parse_flow_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """微厅余量明细 → 通用字段（流量 MB→GB）。"""
    blocks: dict[str, dict] = {}
    for item in detail.get("resources") or []:
        if isinstance(item, dict):
            blocks[str(item.get("type", "")).lower()] = item

    def _num(value: Any) -> float | None:
        return to_float(value)

    def _mb_to_gb(value: Any) -> float | None:
        num = _num(value)
        return round(num / 1024, 2) if num is not None else None

    flow_blk = blocks.get("flow") or {}
    voice_blk = blocks.get("voice") or {}
    sms_blk = blocks.get("smslist") or {}

    flow_used = _mb_to_gb(flow_blk.get("userResource"))
    flow_remain = _mb_to_gb(flow_blk.get("remainResource"))
    voice_used, voice_remain = _num(voice_blk.get("userResource")), _num(voice_blk.get("remainResource"))
    sms_used, sms_remain = _num(sms_blk.get("userResource")), _num(sms_blk.get("remainResource"))

    type_map = {"1": "通用流量", "2": "定向流量", "3": "闲时/区域流量", "4": "国际漫游"}
    sums: dict[str, dict[str, Any]] = {}
    for item in detail.get("fresSumList") or []:
        if not isinstance(item, dict):
            continue
        label = type_map.get(str(item.get("flowtype")))
        if not label:
            continue
        used = _mb_to_gb(item.get("xusedvalue"))
        remain = _mb_to_gb(item.get("xcanusevalue"))
        if used is None and remain is None:
            continue
        sums[label] = {"used": used, "remain": remain, "total": round((used or 0) + (remain or 0), 2)}

    return {
        "plan_name": detail.get("packageName"),
        "flow_general": (
            usage_block(flow_used, round((flow_used or 0) + (flow_remain or 0), 2), flow_remain)
            if flow_used is not None or flow_remain is not None
            else None
        ),
        "flow_sums": sums,
        "voice": (
            usage_block(voice_used, round((voice_used or 0) + (voice_remain or 0), 1), voice_remain)
            if voice_used is not None or voice_remain is not None
            else None
        ),
        "sms": (
            usage_block(sms_used, round((sms_used or 0) + (sms_remain or 0), 1), sms_remain)
            if sms_used is not None or sms_remain is not None
            else None
        ),
    }


def _parse_ocs_usage(payload: dict[str, Any]) -> dict[str, Any]:
    """queryOcsPackageFlowLeftContent 响应解析（App Cookie 链路）。"""
    parsed: dict[str, Any] = {}

    def collect(details: Any) -> tuple[list, list, list]:
        data_items: list = []
        voice_items: list = []
        sms_items: list = []
        if not isinstance(details, list):
            return data_items, voice_items, sms_items
        for item in details:
            if not isinstance(item, dict):
                continue
            elem = item.get("elemType")
            if elem == "3":
                data_items.append(item)
            elif elem == "1":
                voice_items.append(item)
            elif elem == "2":
                sms_items.append(item)
        return data_items, voice_items, sms_items

    all_data: list = []
    all_voice: list = []
    all_sms: list = []
    for source in ("unshared", "shareData", "resources"):
        node = payload.get(source)
        if isinstance(node, dict):
            got = collect(node.get("details"))
            all_data += got[0]
            all_voice += got[1]
            all_sms += got[2]
        elif isinstance(node, list):
            for group in node:
                if isinstance(group, dict):
                    got = collect(group.get("details"))
                    all_data += got[0]
                    all_voice += got[1]
                    all_sms += got[2]

    def flow_row(item: dict) -> dict[str, Any]:
        used, remain = to_float(item.get("use")), to_float(item.get("remain"))
        total = to_float(item.get("total"))
        if total is None and used is not None and remain is not None:
            total = round(used + remain, 2)
        return {
            "name": item.get("addUpItemName"),
            "used": round(used / 1024, 2) if used is not None else None,
            "remain": round(remain / 1024, 2) if remain is not None else None,
            "total": round(total / 1024, 2) if total is not None else None,
            "flow_type": item.get("flowType"),
        }

    if all_data:
        parsed["flow_items"] = [flow_row(i) for i in all_data]
        general = [i for i in all_data if str(i.get("flowType") or "1") == "1"]
        directed = [i for i in all_data if str(i.get("flowType") or "") == "2"]
        for key, items in (("flow_general", general), ("flow_directed", directed)):
            used = sum(to_float(i.get("use")) or 0 for i in items)
            remain = sum(to_float(i.get("remain")) or 0 for i in items)
            if used or remain:
                parsed[key] = usage_block(
                    round(used / 1024, 2), round((used + remain) / 1024, 2), round(remain / 1024, 2)
                )

    for key, items in (("voice", all_voice), ("sms", all_sms)):
        if items:
            used = sum(to_float(i.get("use")) or 0 for i in items)
            total = sum(to_float(i.get("total")) or 0 for i in items)
            remain = sum(to_float(i.get("remain")) or 0 for i in items)
            parsed[key] = usage_block(used, total, remain)
    return parsed


class UnicomClient(CarrierClient):
    """联通客户端：password 模式（App 协议）。"""

    carrier = "unicom"

    def __init__(self, session: aiohttp.ClientSession, data: dict[str, Any]) -> None:
        super().__init__(session, data)
        self._password = str(data.get("password") or "")
        self._app_id = str(data.get("app_id") or "") or generate_app_id()
        self._token_online = str(data.get("token") or "")
        self._desmobile = str(data.get("phone") or "")

    # ------------------------------------------------------------------ sms login

    async def async_sms_trigger(self) -> str:
        """联通短信登录第一步：trigger_risk 拿 mobile_hex（供 H5 滑块页使用）。

        返回提示语：用户在手机 H5 页面过滑块后短信即下发；也可把页面返回的
        resultToken 粘贴到集成里（async_sms_send_with_token）直接下发。
        """
        device_id = self._app_id[:32].replace("-", "") or __import__("uuid").uuid4().hex
        form = {
            **_base_login_params(device_id),
            "resultToken": "", "loginCodeLen": "6",
            "mobile": _rsa_login_urlenc(self.phone),
            "requestLoginCurrentAccount": self.phone,
            "msg_type": "", "ios_open": "1",
        }
        status, body = await self._request(
            "POST", UNICOM_SEND_RANDOM_URL, form=form,
            headers={"User-Agent": UNICOM_APP_UA,
                     "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )
        if status != 200:
            raise CarrierError(f"联通短信下发接口 HTTP {status}")
        try:
            payload = json.loads(body)
        except ValueError as err:
            raise CarrierError(f"联通短信下发响应异常: {body[:120]}") from err
        desc = str(payload.get("dsc") or payload.get("desc") or "")
        code = str(payload.get("code") or "")
        self._sms_device_id = device_id
        if code in ("0", "0000") or "成功" in desc:
            return "短信验证码已下发"
        # 未过滑块时服务端会要求验证 —— 引导用户去 H5 页面操作
        return (
            f"需要先通过官方滑块验证：手机打开 https://m.client.10010.com/login-web "
            f"输入本机号并完成滑块，短信会自动下发；若页面返回 resultToken，可粘贴到集成中直接下发。"
            f"（服务端提示: {desc or code}）"
        )

    async def async_sms_send_with_token(self, result_token: str) -> None:
        """用 H5 滑块成功后的 resultToken 直接下发短信（Pocket-Carrier createSms）。"""
        # 先协商密钥拿 mobile_hex
        status, body = await self._request(
            "POST", f"{UNICOM_LOGIN_WEB}/v1/safeInfo/getKey",
            json_body={"scene": "login"},
            headers={"User-Agent": UNICOM_APP_UA, "Content-Type": "application/json",
                     "Origin": "https://img.client.10010.com",
                     "Referer": "https://img.client.10010.com/loginRisk/index.html"},
        )
        mobile_hex = ""
        try:
            key_payload = json.loads(body)
            mobile_hex = str((key_payload.get("data") or {}).get("mobile") or "")
        except ValueError:
            pass
        headers = {
            "User-Agent": UNICOM_APP_UA, "Content-Type": "application/json",
            "Origin": "https://img.client.10010.com",
            "Referer": "https://img.client.10010.com/loginRisk/index.html",
            "scene-key": "login",
        }
        import uuid as _uuid

        payload = {
            "seq": str(_uuid.uuid4()), "mobile": mobile_hex,
            "resultToken": result_token,
        }
        status, body = await self._request(
            "POST", f"{UNICOM_LOGIN_WEB}/v1/sms/createSms",
            json_body=payload, headers=headers,
        )
        ok = False
        try:
            result = json.loads(body)
            ok = str(result.get("code")) in ("0000", "0")
            if not ok and any(kw in body for kw in ("频繁", "拦截", "已发送")):
                ok = True
        except ValueError:
            pass
        if not ok:
            raise CarrierError("unicom_sms_send_failed", body[:120])

    async def async_sms_login(self, code: str) -> dict[str, Any]:
        """联通短信验证码登录（radomLogin.htm，Pocket-Carrier 验证）。"""
        device_id = getattr(self, "_sms_device_id", "") or self._app_id[:32].replace("-", "")
        ts = time.strftime("%Y%m%d%H%M%S")
        form = {
            **_base_login_params(device_id),
            "yw_code": "", "loginStyle": "0",
            "mobile": _rsa_login_urlenc(self.phone),
            "isRemberPwd": "true",
            "password": _rsa_login_urlenc(code), "keyVersion": "",
            "provinceChanel": "general",
            "voice_code": "", "appId": "ChinaunicomMobileBusiness",
            "voiceoff_flag": "1", "timestamp": ts,
            "requestLoginCurrentAccount": self.phone,
            "loginCodeLen": "6",
        }
        status, body = await self._request(
            "POST", UNICOM_RANDOM_LOGIN_URL, form=form,
            headers={"User-Agent": UNICOM_APP_UA,
                     "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )
        if status != 200:
            raise CarrierError(f"联通短信登录接口 HTTP {status}")
        try:
            result = json.loads(body)
        except ValueError as err:
            raise CarrierError(f"联通短信登录响应异常: {body[:120]}") from err
        code_v = str(result.get("code") or "")
        token = result.get("token_online")
        if code_v in ("0", "0000") and token:
            self._token_online = str(token)
            return {
                "phone": self.phone,
                "password": "",
                "app_id": self._app_id,
                "token": str(token),
            }
        desc = str(result.get("desc") or result.get("dsc") or "")
        raise CarrierAuthError("unicom_sms_login_failed", desc or f"code {code_v}")

    # ------------------------------------------------------------------ login

    async def async_login(self, phone: str, password: str) -> dict[str, Any]:
        """login.htm 服务密码登录，返回可持久化的凭证。"""
        payload = {
            "version": UNICOM_VERSION,
            "mobile": _rsa_login(phone),
            "reqtime": time.strftime("%Y-%m-%d %H:%M:%S"),
            "deviceModel": "Android",
            "netWay": "Wifi",
            "isR4": "0",
            "password": _rsa_login(password),
            "appId": self._app_id,
        }
        status, body = await self._request(
            "POST",
            UNICOM_LOGIN_URL,
            form=payload,
            headers={
                "User-Agent": UNICOM_UA,
                "Referer": "https://m.client.10010.com/",
            },
        )
        if status != 200:
            raise CarrierError(f"联通登录接口 HTTP {status}")
        try:
            result = json.loads(body)
        except ValueError as err:
            raise CarrierError(f"联通登录响应异常: {body[:120]}") from err
        code = str(result.get("code") or "")
        if code not in ("0", "0000"):
            desc = str(result.get("dsc") or result.get("desc") or "")
            # code=2 有双义（实测 2026-10-25）：
            #   "请使用短信验证码登录进行身份验证" = 账号被强制短信登录（新安全策略）
            #   其他 = 密码错误
            if code == "2" and "短信验证码" in desc:
                raise CarrierAuthError(
                    "unicom_forced_sms", "该账号已被联通强制使用短信验证码登录"
                )
            if code in ("2", "4102", "4103", "3102"):
                raise CarrierAuthError("unicom_invalid_credentials", desc)
            raise CarrierError("unicom_login_failed", desc or f"code {code}")
        token = result.get("token_online")
        if not token:
            raise CarrierError("联通登录成功但未返回 token_online")
        return {
            "phone": phone,
            "password": password,
            "app_id": self._app_id,
            "token": token,
        }

    # ------------------------------------------------------------------ renew

    async def _async_renew(self) -> bool:
        """onLine.htm 用 token_online 换新会话 Cookie。"""
        if not self._token_online:
            return False
        form = {
            "token_online": self._token_online,
            "appId": self._app_id,
            "version": UNICOM_VERSION,
            "deviceModel": "Android",
        }
        status, body = await self._request(
            "POST", UNICOM_ONLINE_URL, form=form, headers={"User-Agent": UNICOM_UA}
        )
        if status != 200:
            return False
        try:
            result = json.loads(body)
        except ValueError:
            return False
        if str(result.get("code") or "") != "0":
            return False
        desmobile = str(result.get("desmobile") or "")
        if desmobile and self.phone and desmobile != self.phone:
            raise CarrierAuthError("unicom_session_mismatch")
        if desmobile:
            self._desmobile = desmobile
        new_token = result.get("token_online")
        if new_token:
            self._token_online = str(new_token)
        return True

    # ------------------------------------------------------------------ query

    async def _query_url(self, url: str) -> dict[str, Any]:
        status, body = await self._request(
            "POST", url, form={}, headers={"User-Agent": UNICOM_UA}
        )
        text = body.strip()
        if text in ("999998", "999999") or status in (401, 403):
            raise CarrierAuthError("unicom_session_expired")
        try:
            return json.loads(body)
        except ValueError as err:
            raise CarrierError(f"联通查询响应异常: {body[:120]}") from err

    async def _async_overview_seven(self) -> dict[str, Any] | None:
        """queryUserInfoSeven 概览：code=="Y" 时一次拿到余额/流量/语音/积分。"""
        try:
            status, body = await self._request(
                "GET", UNICOM_USERINFO_SEVEN_URL,
                headers={"User-Agent": UNICOM_UA},
            )
            payload = json.loads(body)
        except (CarrierError, ValueError) as err:
            _LOGGER.debug("queryUserInfoSeven 失败（忽略）: %s", err)
            return None
        if not isinstance(payload, dict) or payload.get("code") != "Y":
            return None
        return payload

    def _apply_overview_seven(self, payload: dict[str, Any], data: dict[str, Any]) -> None:
        """把七宫格概览字段映射进归一化结构。"""
        def _strip_unit(value: Any) -> float | None:
            if value is None:
                return None
            text = str(value)
            num = to_float(__import__("re").match(r"(-?[\d.]+)", text).group(1)) if __import__("re").match(r"(-?[\d.]+)", text) else None
            return num

        data["balance"] = _strip_unit(payload.get("balance"))
        flow = _strip_unit(payload.get("flow"))
        if flow is not None and "GB" in str(payload.get("flow", "")).upper():
            data.setdefault("extra", {})
            if data.get("flow_general") is None and flow:
                data["extra"]["flow_remaining_gb_hint"] = flow
        voice = _strip_unit(payload.get("voice"))
        if voice is not None and data.get("voice") is None:
            data["voice"] = {"used": None, "total": None, "remain": voice}
        points = _strip_unit(payload.get("integral") or payload.get("jifen"))
        if points is not None and data.get("points") is None:
            data["points"] = points
        if payload.get("flush_date_time"):
            data["extra"]["flush_time"] = payload.get("flush_date_time")
        location = payload.get("location") or ""
        if location:
            data["extra"]["location"] = location

    async def async_fetch(self) -> dict[str, Any]:
        data = empty_data("unicom", self.phone)
        if not self._password and not self._token_online:
            raise CarrierAuthError("unicom_credentials_missing")
        if not self._token_online:
            creds = await self.async_login(self.phone, self._password)
            self._token_online = creds["token"]

        # 轻量保活：每次拉取前 onLine 一次（Pocket-Carrier keep_alive 模式）
        try:
            await self._async_renew()
        except CarrierAuthError:
            raise
        except CarrierError as err:
            _LOGGER.debug("联通保活失败（忽略）: %s", err)

        # 概览接口优先（一次拿全），失败不阻断明细链路
        overview = await self._async_overview_seven()
        if overview:
            self._apply_overview_seven(overview, data)

        try:
            package = await self._query_url(UNICOM_PACKAGE_URL)
        except CarrierAuthError:
            if not await self._async_renew():
                raise
            package = await self._query_url(UNICOM_PACKAGE_URL)

        usage = _parse_ocs_usage(package)
        for key, value in usage.items():
            if value:
                data[key] = value

        try:
            balance_payload = await self._query_url(UNICOM_BALANCE_URL)
            balance_data = balance_payload.get("data") if isinstance(balance_payload.get("data"), dict) else {}
            if balance_data:
                if data.get("balance") is None:
                    data["balance"] = to_float(
                        balance_data.get("canusefeecustNew") or balance_data.get("canusefeecust")
                    )
                if data.get("month_cost") is None:
                    data["month_cost"] = to_float(
                        balance_data.get("totalrealfee") or balance_data.get("realfeecustnew")
                    )
                if balance_data.get("packageName"):
                    data["plan_name"] = balance_data.get("packageName")
        except CarrierError as err:
            _LOGGER.debug("联通余额查询失败（忽略）: %s", err)

        if data["plan_name"] is None:
            data["plan_name"] = package.get("packageName") or None
        return finish(data)

    def export_credentials(self) -> dict[str, Any]:
        # onLine 续期会轮换 token_online —— 写回后重启不丢会话
        exported = {"token": self._token_online, "app_id": self._app_id}
        return exported


class UnicomOpenIdClient(CarrierClient):
    """联通客户端：openid 模式（微信小程序链路，移植自 unicom-mini-ha）。"""

    carrier = "unicom"

    def __init__(self, session: aiohttp.ClientSession, data: dict[str, Any]) -> None:
        super().__init__(session, data)
        self._openid = str(data.get("openid") or "")

    def _enc(self, text: str) -> str:
        return rsa_encrypt_b64(UNICOM_MINI_PUBKEY, text)

    async def _mini_post(self, url: str, payload: dict) -> dict[str, Any]:
        status, body = await self._request(
            "POST", url, json_body=payload, headers=MINI_HEADERS_JSON
        )
        try:
            result = json.loads(body)
        except ValueError as err:
            raise CarrierError(f"小程序接口响应异常: {body[:120]}") from err
        if status != 200:
            raise CarrierError(f"小程序接口 HTTP {status}")
        return result

    async def _async_microhall_cookies(self, ticket: str) -> dict[str, str]:
        cookies: dict[str, str] = {}
        url: str | None = UNICOM_MINI_SERVICE_ENTRANCE
        params: dict[str, str] | None = {
            "ticket": ticket,
            "servicecode": "YH10005",
            "ticketChannel": "XCXYLCXYY",
        }
        for _ in range(4):
            if not url:
                break
            try:
                async with self._session.get(
                    url,
                    params=params,
                    headers=MINI_HEADERS_FORM,
                    timeout=aiohttp.ClientTimeout(total=30),
                    allow_redirects=False,
                ) as resp:
                    for key, morsel in resp.cookies.items():
                        cookies[key] = morsel.value
                    location = resp.headers.get("Location")
                    await resp.read()
                    if resp.status in (301, 302, 303, 307, 308) and location:
                        url, params = location, None
                        continue
                    break
            except aiohttp.ClientError as err:
                _LOGGER.debug("微厅会话获取失败: %s", err)
                break
        return cookies

    async def _async_mxx_flow(self, ticket: str, cookie_header: str) -> dict[str, Any]:
        form = {
            "duanlianjieabc": "", "channelCode": "", "serviceType": "", "saleChannel": "",
            "externalSources": "", "contactCode": "", "ticket": ticket, "ticketPhone": "wx",
            "ticketChannel": "XCXYLCXYY", "language": "chinese",
        }
        headers = dict(MINI_HEADERS_FORM)
        if cookie_header:
            headers["Cookie"] = cookie_header
        status, body = await self._request(
            "POST", UNICOM_MINI_FLOW_URL, form=form, headers=headers
        )
        return json.loads(body)

    async def async_fetch(self) -> dict[str, Any]:
        import urllib.parse

        data = empty_data("unicom", self.phone)
        token_result = await self._mini_post(
            UNICOM_MINI_GET_TOKEN,
            {"openid": self._enc(self._openid), "channel": "wxmini"},
        )
        if token_result.get("code") != "0000":
            raise CarrierAuthError("unicom_openid_invalid")
        token = urllib.parse.unquote(str(token_result.get("data") or ""))
        blob = self._enc(token + self._openid)

        overview_raw = await self._mini_post(
            UNICOM_MINI_OVERVIEW, {"openid": blob, "channel": "wxmini"}
        )
        if overview_raw.get("code") != "0000":
            raise CarrierAuthError("unicom_openid_invalid")
        overview = overview_raw.get("data") or {}
        fee_res = overview.get("feeResource") or {}
        flow_res = overview.get("flowResource") or {}
        voice_res = overview.get("voiceResource") or {}
        data["balance"] = to_float(fee_res.get("feePersent"))
        data["extra"]["data_remaining"] = to_float(flow_res.get("flowPersent"))
        data["extra"]["voice_remaining"] = to_float(voice_res.get("voicePersent"))

        try:
            goods = await self._mini_post(
                UNICOM_MINI_GOODS, {"openid": blob, "channel": "wxmini"}
            )
            rows = (goods.get("data") or {}).get("res") or []
            if rows and isinstance(rows[0], dict) and rows[0].get("mainNumber"):
                data["phone"] = rows[0]["mainNumber"]
        except (CarrierError, TypeError, ValueError, IndexError) as err:
            _LOGGER.debug("queryGoodsList 失败（忽略）: %s", err)

        try:
            bc = await self._mini_post(
                UNICOM_MINI_BALANCE_BROADCAST, {"openid": blob, "channel": "wxmini"}
            )
            rows = bc.get("data") or []
            if rows and isinstance(rows[0], dict):
                data["month_cost"] = to_float(rows[0].get("REAL_FEE_CUST"))
                if data["balance"] is None:
                    data["balance"] = to_float(rows[0].get("CANUSE_FEE_CUST"))
        except (CarrierError, TypeError, ValueError, IndexError) as err:
            _LOGGER.debug("余额播报失败（忽略）: %s", err)

        # 微厅余量明细（失败不影响基础数据）
        try:
            t = await self._mini_post(
                UNICOM_MINI_GET_TICKET, {"openId": blob, "channel": "wxmini"}
            )
            if t.get("code") == "0000":
                ticket = str(t.get("data") or "")
                cookies = await self._async_microhall_cookies(ticket)
                if cookies.get("microHallUser"):
                    header = "; ".join(f"{k}={v}" for k, v in cookies.items())
                    detail = await self._async_mxx_flow(ticket, header)
                    parsed = _parse_flow_detail(detail)
                    data["plan_name"] = parsed.get("plan_name") or data["plan_name"]
                    for key in ("flow_general", "voice", "sms"):
                        if parsed.get(key):
                            data[key] = parsed[key]
                    sums = parsed.get("flow_sums") or {}
                    if sums.get("定向流量") and not data["flow_directed"]:
                        data["flow_directed"] = sums["定向流量"]
                    elif sums.get("通用流量") and not data["flow_general"]:
                        data["flow_general"] = sums["通用流量"]
        except (CarrierError, ValueError, TypeError) as err:
            _LOGGER.info("微厅余量明细失败（不影响基础数据）: %s", err)
        return finish(data)


class UnicomCookieClient(CarrierClient):
    """联通客户端：网页 Cookie 模式（新版网厅扫码/短信登录后导出 Cookie）。

    实测（2026-10-31，真实账号）：
    - 查询接口 m.client.10010.com/servicequerybusiness/* 需 version=WT（网页票据渠道）
    - Cookie 来自 www.10010.com 登录会话即可，无需 App token
    - 会话失效返回 999998/999999/1001
    """

    carrier = "unicom"
    WT_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 Mobile/15E148")
    WT_REFERER = "https://imgxx.client.10010.com/yuliangchaxunsfwt/index.html"

    def __init__(self, session, data: dict[str, Any]) -> None:
        super().__init__(session, data)
        self._cookie = str(data.get("cookies") or "")

    def _wt_form(self) -> dict[str, str]:
        return {
            "duanlianjieabc": "", "channelCode": "", "serviceType": "",
            "saleChannel": "", "externalSources": "", "contactCode": "",
            "ticket": "", "ticketPhone": "", "ticketChannel": "", "version": "WT",
        }

    async def _query_wt(self, url: str) -> dict[str, Any]:
        status, body = await self._request(
            "POST", url, form=self._wt_form(),
            headers={
                "User-Agent": self.WT_UA,
                "Referer": self.WT_REFERER,
                "Cookie": self._cookie,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        text = (body or "").strip()
        if text in ("999998", "999999") or status in (401, 403):
            raise CarrierAuthError("unicom_session_expired")
        try:
            payload = json.loads(body)
        except ValueError as err:
            raise CarrierError(f"联通查询响应异常: {body[:120]}") from err
        if isinstance(payload, dict):
            code = str(payload.get("code") or "")
            if code in ("999998", "999999", "1001"):
                raise CarrierAuthError("unicom_session_expired")
        return payload if isinstance(payload, dict) else {}

    async def async_fetch(self) -> dict[str, Any]:
        data = empty_data("unicom", self.phone)
        if not self._cookie:
            raise CarrierAuthError("unicom_session_expired")

        package = await self._query_wt(UNICOM_PACKAGE_URL)
        usage = _parse_ocs_usage(package)
        for key, value in usage.items():
            if value:
                data[key] = value
        if package.get("packageName"):
            data["plan_name"] = package.get("packageName")
        if package.get("flowExceed") is not None:
            data["extra"]["flow_exceed"] = package.get("flowExceed")
        # 顶层汇总（网页版提供，单位随 canuseFlowAllUnit）
        if package.get("canUseFlowAll") is not None:
            data["extra"]["flow_all"] = package.get("canUseFlowAll")
            data["extra"]["flow_all_unit"] = package.get("canuseFlowAllUnit")
        if package.get("canUseSmsAll") is not None:
            data["extra"]["sms_all"] = package.get("canUseSmsAll")

        try:
            balance = await self._query_wt(UNICOM_BALANCE_URL)
            if balance:
                data["balance"] = to_float(
                    balance.get("canusefeecustNew") or balance.get("canusefeecust")
                )
                data["month_cost"] = to_float(
                    balance.get("totalrealfee") or balance.get("realfeecustnew")
                )
                for src_key, dst in (
                    ("curntbalancecust", "balance_current"),
                    ("allbowefeecust", "owed"),
                    ("canuselimitcust", "credit_limit"),
                    ("directionalPsntFeeTotal", "directional_fee"),
                ):
                    val = to_float(balance.get(src_key))
                    if val is not None:
                        data["extra"][dst] = val
        except CarrierError as err:
            _LOGGER.debug("联通余额查询失败（忽略）: %s", err)

        # 总量兜底：通用 + 定向
        if data["flow_total"] is None:
            gen, dire = data["flow_general"], data["flow_directed"]
            if gen or dire:
                data["flow_total"] = usage_block(
                    round((gen or {}).get("used", 0) + (dire or {}).get("used", 0), 2),
                    round((gen or {}).get("total", 0) + (dire or {}).get("total", 0), 2),
                    round((gen or {}).get("remain", 0) + (dire or {}).get("remain", 0), 2),
                )
        if (
            data["balance"] is None
            and data["flow_general"] is None
            and data["voice"] is None
        ):
            raise CarrierAuthError("unicom_session_expired")
        return finish(data)
