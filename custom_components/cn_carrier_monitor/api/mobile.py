"""China Mobile (中国移动) client.

登录链路（login.10086.cn，纯 HTTP，移植自 cmcc-unsub/pure_login.py 的已验证实现）：
  1. GET  login.html                → 建立会话 Cookie
  2. POST loadToken.action          → 风控 token（Xa-before 头）
  3. POST sendRandomCodeAction.action → 发送短信验证码
  4. POST login.htm                 → 手机号+验证码/服务密码（RSA PKCS1v15）
  5. GET  assertAcceptURL?artifact  → 落 SSO Cookie（cmccssotoken 等）

查询（wx.10086.cn，响应为 AES-CBC hex 编码，key/iv 见 ChinaMobileMonitor）：
  getMainPlan / getNewMarginInfo / fareBalance

Cookie 粘贴兜底：从浏览器复制 10086.cn 会话 Cookie。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.parse
from typing import Any

import aiohttp

from ..const import (
    MOBILE_BACK_URL,
    MOBILE_CHANNEL_ID,
    MOBILE_FEE_URL,
    MOBILE_LOGIN_HOST,
    MOBILE_LOGIN_PAGE,
    MOBILE_LOGIN_URL,
    MOBILE_LOAD_TOKEN_URL,
    MOBILE_MARGIN_URL,
    MOBILE_PLAN_URL,
    MOBILE_QUERY_REFERER,
    MOBILE_SEND_SMS_URL,
    MOBILE_UNIT_MAP,
)
from .base import (
    CarrierAuthError,
    CarrierClient,
    CarrierError,
    finish,
    to_float,
    usage_block,
)
from .crypto import decrypt_payload, rsa_encrypt_b64

_LOGGER = logging.getLogger(__name__)

# login_qr_fun.js et() 使用的 JSEncrypt 公钥（SPKI/DER，base64）
MOBILE_LOGIN_PUBKEY = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAsgDq4OqxuEisnk2F0EJFmw4xKa5IrcqEYHvqxPs2"
    "CHEg2kolhfWA2SjNuGAHxyDDE5MLtOvzuXjBx/5YJtc9zj2xR/0moesS+Vi/xtG1tkVaTCba+TV+Y5C61iyr"
    "3FGqr+KOD4/XECu0Xky1W9ZmmaFADmZi7+6gO9wjgVpU9aLcBcw/loHOeJrCqjp7pA98hRJRY+MML8MK15mn"
    "C4ebooOva+mJlstW6t/1lghR8WNV8cocxgcHHuXBxgns2MlACQbSdJ8c6Z3RQeRZBzyjfey6JCCfbEKouVrW"
    "IUuPphBL3OANfgp0B+QG31bapvePTfXU48TYK0M5kE+8LgbbWQIDAQAB"
)

AES_KEY = b"1234123412ABCDEF"
AES_IV = b"ABCDEF1234123412"

LOGIN_ERROR_HINTS = {
    "6001": "mobile_code_invalid",
    "3012": "mobile_captcha_required",
    "8009": "mobile_account_locked",
    "8012": "mobile_account_locked",
    "8002": "mobile_password_error",
}


def et(value: str) -> str:
    """Page-side et(): RSA-PKCS1v15 → base64."""
    return rsa_encrypt_b64(MOBILE_LOGIN_PUBKEY, value)


def device_identity(phone: str) -> str:
    """Deterministic deviceId (visitorId_instanceId), 32hex_32hex."""
    visitor = hashlib.md5(("cmcc-v1|" + phone).encode()).hexdigest()
    instance = hashlib.md5(("cmcc-v1-inst|" + phone).encode()).hexdigest()
    return f"{visitor}_{instance}"


def _login_headers(phone: str) -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": MOBILE_LOGIN_PAGE,
        "Origin": MOBILE_LOGIN_HOST,
    }


async def async_send_sms_code(session, phone: str) -> None:
    """Send the SMS login code. Shared by config flow and reauth."""
    headers = _login_headers(phone)
    # 1. open login page to establish the session
    await _simple_get(session, MOBILE_LOGIN_PAGE, headers)
    # 2. risk-control token
    token = ""
    for form in ({"account": phone}, {"account": et(phone)}, {}):
        status, body = await _post_form(session, MOBILE_LOAD_TOKEN_URL, form, headers)
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        candidate = (
            (payload.get("result") or {}).get("token")
            if isinstance(payload.get("result"), dict)
            else None
        ) or payload.get("token") or (payload.get("result") if isinstance(payload.get("result"), str) else "")
        if payload.get("rspCode") in ("0000", "0") and candidate:
            token = str(candidate)
            break
        if "token" in body.lower() and token:
            break
    extra_headers = dict(headers)
    if token:
        extra_headers["Xa-before"] = token
    # 3. send SMS
    form = {
        "account": phone,
        "pwdType": "02",
        "channelID": MOBILE_CHANNEL_ID,
        "loginMode": "",
        "backUrl": MOBILE_BACK_URL,
    }
    status, body = await _post_form(
        session, MOBILE_SEND_SMS_URL, form, extra_headers
    )
    text = body or ""
    lowered = text.lower()
    ok = any(
        marker in lowered or marker in text
        for marker in ("succeed", "成功", '"rspCode":"0000"', '"rspCode":"0"', '"code":"0"', "已发送")
    )
    if not ok:
        snippet = text[:120].replace("\n", " ")
        _LOGGER.debug("移动短信发送响应: HTTP %s %s", status, snippet)
        raise CarrierError(f"移动验证码发送失败: {snippet or 'HTTP ' + str(status)}")


async def _simple_get(session, url: str, headers: dict) -> tuple[int, str]:
    try:
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
            allow_redirects=True,
        ) as resp:
            return resp.status, await resp.text()
    except Exception as err:  # noqa: BLE001 - login page is best-effort
        _LOGGER.debug("GET %s failed: %s", url, err)
        return 0, ""


async def _post_form(session, url: str, form: dict, headers: dict) -> tuple[int, str]:
    try:
        async with session.post(
            url,
            data=form,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=45),
            allow_redirects=False,
        ) as resp:
            return resp.status, await resp.text()
    except aiohttp.ClientError as err:
        raise CarrierError(f"网络错误: {err}") from err
    except TimeoutError as err:
        raise CarrierError(f"请求超时: {url}") from err


class MobileClient(CarrierClient):
    """移动客户端：SMS / password / cookie 三种模式。"""

    carrier = "mobile"

    def __init__(self, session, data: dict[str, Any]) -> None:
        super().__init__(session, data)
        self._mode = str(data.get("auth_mode") or "cookie")
        self._password = str(data.get("password") or "")
        self._cookie = str(data.get("cookies") or "")
        self._device_id = data.get("device_id") or device_identity(self.phone)

    # ------------------------------------------------------------------ login

    async def async_submit_code(self, phone: str, code: str) -> None:
        """Submit the SMS code and establish the SSO session."""
        await self._login_submit(phone, code, "02")

    async def async_login_password(self, phone: str, password: str) -> None:
        """Submit the service password and establish the SSO session."""
        await self._login_submit(phone, password, "01")

    async def _login_submit(self, phone: str, secret: str, pwd_type: str) -> None:
        headers = _login_headers(phone)
        await _simple_get(self._session, MOBILE_LOGIN_PAGE, headers)
        ts = str(int(time.time() * 1000))
        enc = et(secret)
        form = {
            "accountType": "01",
            "account": et(phone),
            "password": enc,
            "smsPwd": enc,
            "pwdType": pwd_type,
            "email_sms": "",
            "inputCode": "",
            "backUrl": MOBILE_BACK_URL,
            "rememberMe": "0",
            "channelID": MOBILE_CHANNEL_ID,
            "loginMode": "",
            "protocol": "https:",
            "isNew": "1",
            "deviceId": self._device_id,
            "deviceIdSource": "cache",
            "timestamp": ts,
        }
        status, body = await _post_form(self._session, MOBILE_LOGIN_URL, form, headers)
        if status != 200:
            raise CarrierError(f"移动登录请求失败 HTTP {status}")
        try:
            result = json.loads(body)
        except ValueError as err:
            raise CarrierError(f"移动登录响应异常: {body[:120]}") from err
        if str(result.get("result")) != "0":
            code = str(result.get("code") or "")
            hint = LOGIN_ERROR_HINTS.get(code)
            if hint:
                raise CarrierAuthError(hint)
            raise CarrierError(
                f"移动登录失败: {result.get('desc') or code or result.get('result')}"
            )
        if result.get("islocal") is True:
            target = MOBILE_BACK_URL
        else:
            base = str(result.get("assertAcceptURL") or "")
            if not base:
                raise CarrierError("移动登录返回缺少跳转地址")
            target = (
                f"{base}?backUrl={urllib.parse.quote(MOBILE_BACK_URL, safe='')}"
                f"&artifact={result.get('artifact', '')}&type={result.get('type', '')}"
            )
        status, _ = await _simple_get(self._session, target, headers)
        jar_names = {c.key for c in self._session.cookie_jar}
        if "cmccssotoken" not in jar_names and "is_login" not in jar_names:
            raise CarrierError("移动登录跳转后未取得 SSO Cookie，请重试")

    # ------------------------------------------------------------------ query

    def _query_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": MOBILE_QUERY_REFERER,
        }
        if self._cookie:
            headers["Cookie"] = self._cookie
        return headers

    async def _wx_query(self, url: str) -> dict[str, Any] | None:
        ts = int(time.time() * 1000)
        sep = "&" if "?" in url else "?"
        status, body = await self._request(
            "GET", f"{url}{sep}t={ts}", headers=self._query_headers()
        )
        if status != 200:
            _LOGGER.debug("移动查询 %s HTTP %s", url, status)
            return None
        text = decrypt_payload(body, AES_KEY, AES_IV)
        try:
            payload = json.loads(text)
        except ValueError:
            _LOGGER.debug("移动查询 %s 响应非 JSON: %s", url, text[:120])
            return None
        if not isinstance(payload, dict):
            return None
        # 登录态校验：部分接口会返回需要登录的标记
        joined = json.dumps(payload, ensure_ascii=False)[:2000]
        if any(mark in joined for mark in ("请登录", "login.htm", "notLogin", "未登录")):
            raise CarrierAuthError("mobile_session_expired")
        return payload

    async def async_fetch(self) -> dict[str, Any]:
        data = empty_data("mobile", self.phone)
        if self._mode == "cookie" and not self._cookie:
            raise CarrierAuthError("缺少登录会话，请重新登录")

        # 套餐名
        plan = await self._wx_query(MOBILE_PLAN_URL)
        if plan:
            data["plan_name"] = (
                ((plan.get("object") or {}).get("resultData") or {}).get("curPlanName")
            ) or None

        # 余额
        fee = await self._wx_query(MOBILE_FEE_URL)
        if fee:
            fee_rsp = (fee.get("data") or {}).get("realFeeQryRsp") or {}
            data["balance"] = to_float(fee_rsp.get("curFeeTotal"))
            if data["balance"] is None:
                data["balance"] = to_float(fee_rsp.get("realFee"))

        # 余量（流量/语音/短信）
        margin = await self._wx_query(MOBILE_MARGIN_URL)
        if margin:
            self._parse_margin(margin, data)

        if data["plan_name"] is None and data["balance"] is None and margin is None:
            raise CarrierAuthError("mobile_session_expired")
        return finish(data)

    def _parse_margin(self, payload: dict[str, Any], data: dict[str, Any]) -> None:
        rd = (payload.get("data") or {}).get("resultData") or {}

        def _item(raw: dict) -> tuple[Any, Any, Any, str]:
            return (
                raw.get("usedNum"),
                raw.get("sumNum"),
                raw.get("remainNum"),
                str(raw.get("unit") or "04"),
            )

        def _scale(value: Any, unit: str) -> Any:
            num = to_float(value)
            if num is None:
                return None
            if unit == "03":  # MB → GB
                return round(num / 1024, 2)
            return round(num, 2)

        flow = rd.get("planRemainFlowInfo") or rd.get("planRemianFlowInfo") or {}
        if flow:
            plan = flow.get("planRemain") or flow.get("planRemian") or {}
            used, total, remain, unit = _item(plan)
            data["flow_general"] = usage_block(_scale(used, unit), _scale(total, unit), _scale(remain, unit))
            directional = flow.get("directionalFlowInfo") or {}
            used, total, remain, unit = _item(directional)
            data["flow_directed"] = usage_block(_scale(used, unit), _scale(total, unit), _scale(remain, unit))
            total_info = flow.get("totalInfo") or {}
            used, total, remain, unit = _item(total_info)
            data["flow_total"] = usage_block(_scale(used, unit), _scale(total, unit), _scale(remain, unit))
            other = flow.get("otherRemian") or {}
            used, total, remain, unit = _item(other)
            block = usage_block(_scale(used, unit), _scale(total, unit), _scale(remain, unit))
            if block:
                data["extra"]["flow_regional"] = block

        voice = rd.get("planRemainVoiceInfo") or rd.get("planRemianVoiceInfo") or {}
        if voice:
            plan = voice.get("planRemain") or voice.get("planRemian") or {}
            used, total, remain, unit = _item(plan)
            data["voice"] = usage_block(used, total, remain)

        sms = rd.get("planRemainMSGInfo") or rd.get("planRemianMSGInfo") or {}
        if sms:
            note = sms.get("notePlanRemain") or sms.get("notePlanRemian") or {}
            used, total, remain, unit = _item(note)
            if used is None and total is None:
                note = sms.get("totalInfo") or {}
                used, total, remain, unit = _item(note)
            data["sms"] = usage_block(used, total, remain)

    def export_credentials(self) -> dict[str, Any]:
        return {"device_id": self._device_id}
