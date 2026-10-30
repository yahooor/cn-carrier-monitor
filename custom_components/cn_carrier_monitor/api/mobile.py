"""China Mobile (中国移动) client — 三链路合一.

登录（与查询同一套会话，二选一或导入）：
- sms:        login.10086.cn 网厅链路（社区逆向，cmcc-unsub 验证）
- password:   同上链路 pwdType=01
- cookie:     导入浏览器 Cookie（兼容 12.5 App 会话 Cookie）

查询（双通道自动切换）：
- 原生 12.5 App 协议（clientaccess.10086.cn，Pocket-Carrier 验证）：
  三层签名（x-token/xs/x-sign），请求 AES key=bAIgvwAuA4tbDr9d，响应 key=GS7VelkJl5IT1uwQ，
  iv=9791027341711819；refreshSession 保活顺延 TTL；retCode 410000/400001/... 即会话失效。
  只需会话 Cookie（任意来源），与登录方式解耦。
- wx.10086.cn 网厅通道（AES hex 1234123412ABCDEF）：原生协议失败时的兜底。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import random
import re
import time
import urllib.parse
from typing import Any

import aiohttp

try:  # wx.10086.cn 按 TLS 指纹拦截 OpenSSL 客户端
    from curl_cffi import requests as _curl_requests
except ImportError:
    _curl_requests = None

from ..const import (
    MOBILE_BACK_URL,
    MOBILE_CHANNEL_ID,
    MOBILE_LOAD_TOKEN_URL,
    MOBILE_LOGIN_HOST,
    MOBILE_LOGIN_PAGE,
    MOBILE_LOGIN_URL,
    MOBILE_PLAN_URL,
    MOBILE_QUERY_REFERER,
    MOBILE_SEND_SMS_URL,
)
from .base import (
    CarrierAuthError,
    CarrierClient,
    CarrierError,
    ParseAnomalyError,
    finish,
    parse_safe,
    to_float,
    usage_block,
)
from .crypto import aes_cbc_encrypt, decrypt_payload, rsa_encrypt_b64

_LOGGER = logging.getLogger(__name__)

# uniApi 反爬三件套（反编译自 app.5d4ab1a0.js，2026-10-31 裁决：可复现）
LRSBHBG8_VALUE = "ZS93dUFVa2kzaEpQSjM0SG55MUFDdz09"  # bundle 内硬编码常量
FP_HEADER_NAME = "e76ccba5165fa9b4966ad82a0088e520"


def _gen_fingerprint(ua: str, screen_w: int = 1920, screen_h: int = 1080) -> str:
    """e76ccba… 头值：时间戳hex-随机hex-UA哈希-屏幕面积hex-15位随机（无硬件绑定）。"""
    import random as _random
    import re as _re

    def screen_hex():
        t = str(screen_w * screen_h)
        return format(int(t), "x") if _re.match(r"\d{5,}", t) else \
            str(31242 * _random.random()).replace(".", "")[:8]

    def ua_hash():
        r, a = [], 0
        for ch in ua:
            t = ord(ch)
            r.insert(0, t & 255)
            if len(r) >= 4:
                n = 0
                for i in range(4):
                    n |= r[i] << (8 * i)
                a ^= n
                r = []
        if r:
            n = 0
            for i in range(len(r)):
                n |= r[i] << (8 * i)
            a ^= n
        if a < 0:
            a = (a + (1 << 32)) & 0xFFFFFFFF
        return format(a, "x")

    def rand16():
        s = str(_random.random()).replace(".", "")
        return s[:7] or "0"

    return "-".join([
        format(int(time.time() * 1000), "x"),
        rand16(),
        ua_hash(),
        screen_hex(),
        "".join(str(_random.randint(0, 9)) for _ in range(15)),
    ])

# ---------------------------------------------------------------- login (web)
MOBILE_LOGIN_PUBKEY = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAsgDq4OqxuEisnk2F0EJFmw4xKa5IrcqEYHvqxPs2"
    "CHEg2kolhfWA2SjNuGAHxyDDE5MLtOvzuXjBx/5YJtc9zj2xR/0moesS+Vi/xtG1tkVaTCba+TV+Y5C61iyr"
    "3FGqr+KOD4/XECu0Xky1W9ZmmaFADmZi7+6gO9wjgVpU9aLcBcw/loHOeJrCqjp7pA98hRJRY+MML8MK15mn"
    "C4ebooOva+mJlstW6t/1lghR8WNV8cocxgcHHuXBxgns2MlACQbSdJ8c6Z3RQeRZBzyjfey6JCCfbEKouVrW"
    "IUuPphBL3OANfgp0B+QG31bapvePTfXU48TYK0M5kE+8LgbbWQIDAQAB"
)

LOGIN_ERROR_HINTS = {
    "6001": "mobile_code_invalid",
    "3012": "mobile_captcha_required",
    "8009": "mobile_account_locked",
    "8012": "mobile_account_locked",
    "8002": "mobile_password_error",
}

# ---------------------------------------------------------------- native 12.5
NATIVE_BASE = "https://clientaccess.10086.cn"
NATIVE_REQ_KEY = b"bAIgvwAuA4tbDr9d"
NATIVE_RESP_KEY = b"GS7VelkJl5IT1uwQ"
NATIVE_IV = b"9791027341711819"
NATIVE_UA = "ChinaMobile/12.5.5 (iPhone; iOS 16.1.1; Scale/3.00)"

NATIVE_AUTH_EXPIRED_CODES = {"410000", "400001", "400006", "8051", "8052", "6001"}
NATIVE_AUTH_EXPIRED_HINTS = ("请重新登录", "重新登录", "未进行任何操作", "会话已失效", "请先登录", "凭据已失效")

_ENVELOPE_DEFAULTS = {
    "sv": "16.1.1", "nt": "3", "sn": "iPhone12,5", "en": "0", "sp": "1242x2688",
    "t": "83216123546aa27f10780c5c645b0e30",
    "imei": "A903F8A96F7048B69764722BCF90EE8",
    "ctid": "A903F8A96F7048B69764722BCF90EE81",
    "cid": "bf8649694f1e86dbbe9fbcd3e26f92407695b550aa7b4d3e0f51934ac10c6666",
    "ak": "F5AA34B89513F0D087CA0EF11A3277469DC74905",
    "xk": "2408b2a5d0956f22ad08471f3c63d5d8e31dbca45e16a53875c4154317b270adacb3c15d",
    "xc": "B2000", "ak2": None, "cv": "12.5.5", "sb": "apple", "st": 2,
}

# 2026-10-31 实测：旧 serviceMargin/fareBalance、getNewMarginInfo 已下线（返回 SPA 页面）
MOBILE_FEE_URL_V2 = "https://wx.10086.cn/website/fareBalance"
MOBILE_MARGIN_URL_V2 = "https://wx.10086.cn/website/uniApi/getNewMarginInfo"
MOBILE_MARGIN_QRY_URL_V2 = "https://wx.10086.cn/website/uniApi/getMarginQueryInfo"
MOBILE_SCORE_URL = "https://wx.10086.cn/website/score/news"
WX_AES_KEY = b"1234123412ABCDEF"
WX_AES_IV = b"ABCDEF1234123412"
# wx 网厅各接口的已知顶层键（套餐=object，其余=data；native 兜底=rspBody）
_WX_TOP_KEYS = {"object", "data", "rspBody"}


def _native_aes_b64(key: bytes, iv: bytes, plain: str) -> str:
    """移动 12.5 请求体/令牌：AES-CBC 密文 → base64 文本。"""
    return base64.b64encode(aes_cbc_encrypt(key, iv, plain)).decode("ascii")


def et(value: str) -> str:
    """Web-login RSA helper."""
    return rsa_encrypt_b64(MOBILE_LOGIN_PUBKEY, value)


def device_identity(phone: str) -> str:
    visitor = hashlib.md5(("cmcc-v1|" + phone).encode()).hexdigest()
    instance = hashlib.md5(("cmcc-v1-inst|" + phone).encode()).hexdigest()
    return f"{visitor}_{instance}"


def _login_headers() -> dict[str, str]:
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


async def _simple_get(session, url: str, headers: dict) -> tuple[int, str]:
    try:
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=30),
            allow_redirects=True,
        ) as resp:
            return resp.status, await resp.text()
    except Exception as err:  # noqa: BLE001 - login page is best-effort
        _LOGGER.debug("GET %s failed: %s", url, err)
        return 0, ""


async def _post_form(session, url: str, form: dict, headers: dict) -> tuple[int, str]:
    try:
        async with session.post(
            url, data=form, headers=headers,
            timeout=aiohttp.ClientTimeout(total=45), allow_redirects=False,
        ) as resp:
            return resp.status, await resp.text()
    except aiohttp.ClientError as err:
        raise CarrierError(f"网络错误: {err}") from err
    except TimeoutError as err:
        raise CarrierError(f"请求超时: {url}") from err


async def async_send_sms_code(session, phone: str) -> None:
    """Send the web-hall SMS login code (best-effort community-reverse-engineered)."""
    headers = _login_headers()
    await _simple_get(session, MOBILE_LOGIN_PAGE, headers)
    token = ""
    for form in ({"account": phone}, {"account": et(phone)}, {}):
        _status, body = await _post_form(session, MOBILE_LOAD_TOKEN_URL, form, headers)
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        candidate = ""
        if isinstance(payload.get("result"), dict):
            candidate = payload["result"].get("token") or ""
        candidate = candidate or payload.get("token") or (
            payload.get("result") if isinstance(payload.get("result"), str) else ""
        )
        if payload.get("rspCode") in ("0000", "0") and candidate:
            token = str(candidate)
            break
    extra_headers = dict(headers)
    if token:
        extra_headers["Xa-before"] = token
    form = {
        "account": phone,
        "pwdType": "02",
        "channelID": MOBILE_CHANNEL_ID,
        "loginMode": "",
        "backUrl": MOBILE_BACK_URL,
    }
    status, body = await _post_form(session, MOBILE_SEND_SMS_URL, form, extra_headers)
    text = body or ""
    ok = any(
        marker in text.lower() or marker in text
        for marker in ("succeed", "成功", '"rspCode":"0000"', '"rspCode":"0"', '"code":"0"', "已发送")
    )
    if not ok:
        snippet = text[:120].replace("\n", " ")
        _LOGGER.debug("移动短信发送响应: HTTP %s %s", status, snippet)
        raise CarrierError(f"mobile_sms_send_failed: {snippet or 'HTTP ' + str(status)}")


class MobileClient(CarrierClient):
    """移动客户端：登录（web 链路）+ 查询（原生 12.5 优先，wx 网厅兜底）。"""

    carrier = "mobile"

    def __init__(self, session, data: dict[str, Any]) -> None:
        super().__init__(session, data)
        self._mode = str(data.get("auth_mode") or "cookie")
        self._password = str(data.get("password") or "")
        self._cookie = str(data.get("cookies") or "")
        self._device_id = data.get("device_id") or device_identity(self.phone)
        # native envelope keeps a per-phone deterministic prov
        self._prov = "451"
        self._city = "0459"

    # ------------------------------------------------------------------ login

    async def async_submit_code(self, phone: str, code: str) -> None:
        await self._login_submit(phone, code, "02")

    async def async_login_password(self, phone: str, password: str) -> None:
        await self._login_submit(phone, password, "01")

    async def _login_submit(self, phone: str, secret: str, pwd_type: str) -> None:
        headers = _login_headers()
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
        await _simple_get(self._session, target, headers)
        jar_names = {c.key for c in self._session.cookie_jar}
        if "cmccssotoken" not in jar_names and "is_login" not in jar_names:
            raise CarrierError("移动登录跳转后未取得 SSO Cookie，请重试")
        # persist for query use
        self._cookie = "; ".join(
            f"{c.key}={c.value}" for c in self._session.cookie_jar
        )

    # ------------------------------------------------------------------ native 12.5

    def _merge_cookies(self, fresh: dict[str, str]) -> None:
        """把响应下发的 Set-Cookie 合并回会话串（uuid 自愈链路的回收端）。"""
        if not fresh:
            return
        merged = self._parse_cookie_header()
        changed = False
        for k, v in fresh.items():
            if merged.get(k) != v:
                merged[k] = v
                changed = True
        if changed:
            self._cookie = "; ".join(f"{k}={v}" for k, v in merged.items())

    def _parse_cookie_header(self) -> dict[str, str]:
        jar: dict[str, str] = {}
        for part in self._cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name:
                jar[name] = value
        # merge live session cookies (login path)
        for c in self._session.cookie_jar:
            jar[c.key] = c.value
        return jar

    async def _native_request(self, endpoint: str, req_body: dict | None = None) -> dict[str, Any]:
        cookies = self._parse_cookie_header()
        envelope = {
            "tel": self.phone, "reqBody": req_body or {},
            **{k: v for k, v in _ENVELOPE_DEFAULTS.items() if v is not None},
            "prov": self._prov, "city": self._city,
        }
        plain = json.dumps(envelope, separators=(",", ":"))
        enc_body = _native_aes_b64(NATIVE_REQ_KEY, NATIVE_IV, plain)

        now_ms = str(int(time.time() * 1000))
        nonce = str(random.randint(10000000, 99999999))
        full_url = f"{NATIVE_BASE}/{endpoint.lstrip('/')}"
        x_token = _native_aes_b64(
            NATIVE_REQ_KEY, NATIVE_IV,
            f"{_ENVELOPE_DEFAULTS['xk']}_/{endpoint.lstrip('/')}_{now_ms}_{nonce}")
        xs = hashlib.md5(f"{full_url}_{plain}_Leadeon/SecurityOrganization".encode()).hexdigest()
        jsessionid = cookies.get("JSESSIONID", "")
        x_sign = hashlib.md5(f"{x_token}_{now_ms}_{nonce}_{jsessionid}".encode()).hexdigest()

        headers = {
            "User-Agent": NATIVE_UA,
            "Content-Type": "application/Json",
            "Accept": "*/*",
            "Accept-Language": "zh-Hans;q=1, zh-Hans-CN;q=0.9, en-CN;q=0.8",
            "Connection": "keep-alive",
            "x-qen": "2",
            "x-time": now_ms,
            "x-nonce": nonce,
            "x-token": x_token,
            "xs": xs,
            "x-sign": x_sign,
        }
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

        status, body = await self._request(
            "POST", full_url, headers=headers,
            data=enc_body,
            content_type_override="application/Json",
        )
        if status in (401, 403):
            raise CarrierAuthError("mobile_session_expired", f"HTTP {status}")
        text = body.strip()
        if not text:
            return {}
        # response is base64 AES; try native decrypt then fall back to plain JSON
        payload: dict[str, Any] | None = None
        try:
            from .crypto import aes_cbc_decrypt

            cipher_raw = base64.b64decode(text)
            payload = json.loads(aes_cbc_decrypt(NATIVE_RESP_KEY, NATIVE_IV, cipher_raw))
        except Exception:  # noqa: BLE001 - fall back below
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
        if not isinstance(payload, dict):
            raise CarrierError("移动原生接口响应无法解析")
        ret_code = str(payload.get("retCode") or payload.get("code") or "")
        ret_desc = str(payload.get("retDesc") or payload.get("desc") or "")
        if ret_code in NATIVE_AUTH_EXPIRED_CODES or any(
            kw in ret_desc for kw in NATIVE_AUTH_EXPIRED_HINTS
        ):
            raise CarrierAuthError("mobile_session_expired", f"[{ret_code}] {ret_desc}")
        return payload

    async def _native_fetch(self, data: dict[str, Any]) -> bool:
        """Fill data via native endpoints; returns True on any success."""
        ok = False
        # 0. 保活顺延 TTL（失败忽略）
        try:
            await self._native_request("biz-orange/DN/refreshSession", {"cellNum": self.phone})
        except CarrierAuthError:
            raise
        except CarrierError as err:
            _LOGGER.debug("移动 refreshSession 忽略: %s", err)
        # 1. 余额
        try:
            fee = await self._native_request(
                "biz-orange/BN/realFeeQuery/getRealFee", {"cellNum": self.phone}
            )
            body = fee.get("rspBody") or {}
            if isinstance(body, dict) and body:
                balance = to_float(
                    body.get("curFeeTotal") or body.get("curFee") or body.get("realBalanceFee")
                )
                if balance is not None:
                    data["balance"] = balance
                    ok = True
                real_fee = to_float(body.get("realFee"))
                if real_fee is not None:
                    data["month_cost"] = real_fee
                owe = to_float(body.get("oweFee"))
                if owe:
                    data["extra"]["owe_fee"] = owe
        except CarrierAuthError:
            raise
        except CarrierError as err:
            _LOGGER.debug("移动 getRealFee 失败: %s", err)
        # 2. 套餐余量
        try:
            plan = await self._native_request(
                "biz-orange/BH/newPlanRemainQry/getNewPlanRemainQry", {"cellNum": self.phone}
            )
            self._parse_native_plan(plan, data)
            ok = True
        except CarrierAuthError:
            raise
        except CarrierError as err:
            _LOGGER.debug("移动 getNewPlanRemainQry 失败: %s", err)
        # 3. 积分
        try:
            score = await self._native_request(
                "biz-orange/BN/scoreQueryService/getScoreQuery", {"cellNum": self.phone}
            )
            body = score.get("rspBody") or {}
            if isinstance(body, dict):
                points = to_float(body.get("score") or body.get("totalScore"))
                if points is not None:
                    data["points"] = points
        except CarrierError as err:
            _LOGGER.debug("移动积分查询失败: %s", err)
        return ok

    @parse_safe
    def _parse_native_plan(self, payload: dict[str, Any], data: dict[str, Any]) -> None:
        """解析 12.5 getNewPlanRemainQry 响应（Pocket-Carrier 验证的结构）。

        rspBody.newPlanRemainQryRes.planRemianFlowListRes.planRemianFlowRes[]:
          flowRemainNum/flowUsdNum/flowSumNum + unit（02=KB 03=MB 04=GB）
        rspBody.newPlanRemainQryRes.planRemianVoiceListRes.planRemianVoiceInfoRes[]:
          voiceRemainNum/voiceUsdNum/voiceSumNum（分钟）
        """
        rsp = payload.get("rspBody")
        if not isinstance(rsp, dict):
            return
        qry = rsp.get("newPlanRemainQryRes")
        if not isinstance(qry, dict):
            return

        def _to_mb(value: Any, unit: str) -> float:
            num = to_float(value) or 0.0
            if unit == "04":   # GB → MB
                return num * 1024.0
            if unit == "02":   # KB → MB
                return num / 1024.0
            return num          # 03 MB

        flow_node = (qry.get("planRemianFlowListRes") or {}).get("planRemianFlowRes") or []
        if isinstance(flow_node, list) and flow_node:
            remain_mb = used_mb = sum_mb = 0.0          # 全量（含定向）
            gen_remain = gen_used = gen_sum = 0.0       # 通用（排除定向）
            items: list[dict[str, Any]] = []
            for item in flow_node:
                if not isinstance(item, dict):
                    continue
                unit = str(item.get("unit") or "03")
                rem = _to_mb(item.get("flowRemainNum"), unit)
                usd = _to_mb(item.get("flowUsdNum"), unit)
                sm = _to_mb(item.get("flowSumNum"), unit)
                remain_mb += rem
                used_mb += usd
                sum_mb += sm
                name = str(item.get("mealTypeDesc") or item.get("flowTypeDesc") or "流量套餐")
                directed = "定向" in name or "专属" in name
                if not directed:
                    gen_remain += rem
                    gen_used += usd
                    gen_sum += sm
                items.append({
                    "name": name, "used": round(usd / 1024, 2),
                    "remain": round(rem / 1024, 2), "total": round(sm / 1024, 2),
                })
                if directed and data["flow_directed"] is None:
                    data["flow_directed"] = usage_block(
                        round(usd / 1024, 2), round(sm / 1024, 2), round(rem / 1024, 2)
                    )
            data["flow_general"] = usage_block(
                round(gen_used / 1024, 2), round(gen_sum / 1024, 2), round(gen_remain / 1024, 2)
            )
            data["flow_total"] = usage_block(
                round(used_mb / 1024, 2), round(sum_mb / 1024, 2), round(remain_mb / 1024, 2)
            )
            if items:
                data["extra"]["flow_items"] = items

        voice_node = (qry.get("planRemianVoiceListRes") or {}).get("planRemianVoiceInfoRes") or []
        if isinstance(voice_node, list) and voice_node:
            v_rem = v_usd = v_sum = 0.0
            for item in voice_node:
                if not isinstance(item, dict):
                    continue
                v_rem += to_float(item.get("voiceRemainNum")) or 0
                v_usd += to_float(item.get("voiceUsdNum")) or 0
                v_sum += to_float(item.get("voiceSumNum")) or 0
            data["voice"] = usage_block(round(v_usd), round(v_sum), round(v_rem))

    # ------------------------------------------------------------------ wx fallback

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

    async def _wx_query(
        self, url: str, *, method: str = "GET", json_body: dict | None = None,
        extra_headers: dict | None = None,
        top_keys: frozenset[str] | set[str] | None = None,
    ) -> dict[str, Any] | None:
        ts = int(time.time() * 1000)
        target = url
        if method == "GET":
            target = f"{url}{('&' if '?' in url else '?')}t={ts}"
        # wx.10086.cn 按 TLS 指纹拦截 OpenSSL（aiohttp）客户端：优先 curl_cffi
        cffi = await self._cffi_wx(target, method, json_body, extra_headers)
        if cffi is not None:
            status, body = cffi
        else:
            headers = self._query_headers()
            if extra_headers:
                headers.update(extra_headers)
            status, body = await self._request(
                method, target, json_body=json_body, headers=headers
            )
        if status != 200:
            _LOGGER.debug("移动网厅查询 %s HTTP %s", url, status)
            return None
        text = decrypt_payload(body, WX_AES_KEY, WX_AES_IV)
        try:
            payload = json.loads(text)
        except ValueError:
            _LOGGER.debug("移动网厅查询 %s 响应非 JSON: %s", url, text[:120])
            raise ParseAnomalyError(f"wx 解密/解析失败: {text[:80]}") from None
        if not isinstance(payload, dict) or not payload:
            raise ParseAnomalyError(f"wx 响应顶层结构未知: {str(payload)[:80]}")
        if not (top_keys or _WX_TOP_KEYS).intersection(payload):
            raise ParseAnomalyError(f"wx 响应缺少已知顶层键: {sorted(payload)[:8]}")
        joined = json.dumps(payload, ensure_ascii=False)[:2000]
        if any(mark in joined for mark in ("请登录", "login.htm", "notLogin", "未登录")):
            raise CarrierAuthError("mobile_session_expired")
        return payload


    async def _cffi_wx(self, url: str, method: str = "GET",
                       json_body: dict | None = None,
                       extra_headers: dict | None = None):
        """curl_cffi（Chrome TLS 指纹）请求；不可用或失败返回 None。"""
        if _curl_requests is None:
            return None

        def _do():
            headers = {
                k: v for k, v in self._query_headers().items()
                if k.lower() != "cookie"
            }
            if extra_headers:
                headers.update(extra_headers)
            cookies: dict[str, str] = {}
            for part in self._cookie.split(";"):
                if "=" in part:
                    k, _, v = part.strip().partition("=")
                    if k:
                        cookies[k] = v
            try:
                if method == "GET":
                    r = _curl_requests.get(url, headers=headers, cookies=cookies,
                                           impersonate="chrome", timeout=25)
                else:
                    r = _curl_requests.post(url, json=json_body, headers=headers,
                                            cookies=cookies,
                                            impersonate="chrome", timeout=25)
                # Set-Cookie 回收：wx.10086.cn 用 uuid 令牌在任意认证 GET 上
                # 重签 d.sid（72h 自愈链路）——不回收则续期成果丢失
                try:
                    fresh = r.cookies.get_dict()
                except Exception:  # noqa: BLE001
                    fresh = {}
                if fresh:
                    self._merge_cookies(fresh)
                return r.status_code, r.text
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("移动 curl_cffi 请求失败: %s", err)
                return None

        return await asyncio.to_thread(_do)

    # ------------------------------------------------------------------ fetch

    async def async_fetch(self) -> dict[str, Any]:
        data = empty_snapshot("mobile", self.phone)
        if self._mode == "cookie" and not self._cookie:
            raise CarrierAuthError("mobile_session_missing")

        # 网页登录（sms/password/cookie）的会话 Cookie 只对 wx.10086.cn 网厅有效；
        # 12.5 原生通道(clientaccess)需要 App 会话，用网页 Cookie 必报会话过期。
        # 因此默认走 wx 通道；原生仅在显式提供原生 Cookie 时启用。
        native_ok = False
        if self._data.get("use_native"):
            try:
                native_ok = await self._native_fetch(data)
            except CarrierAuthError:
                raise
            except CarrierError as err:
                _LOGGER.debug("移动原生通道失败，转网厅通道: %s", err)

        if not native_ok:
            await self._wx_fetch(data)

        if (
            data["plan_name"] is None
            and data["balance"] is None
            and data["flow_total"] is None
            and data["voice"] is None
        ):
            raise CarrierAuthError("mobile_session_expired")
        return finish(data)

    async def _wx_fetch(self, data: dict[str, Any]) -> None:
        plan = await self._wx_query(MOBILE_PLAN_URL)
        if plan:
            rd = (plan.get("object") or {}).get("resultData") or {}
            data["plan_name"] = rd.get("curPlanName") or None
            if rd.get("curPlanPrice"):
                data["plan_price"] = rd.get("curPlanPrice")
        fee = await self._wx_query(MOBILE_FEE_URL_V2)
        if fee:
            fee_rsp = (fee.get("data") or {}).get("realFeeQryRsp") or {}
            if data["balance"] is None:
                data["balance"] = to_float(
                    fee_rsp.get("curFeeTotal") or fee_rsp.get("curFee"))
            # 本月实时话费（8 元卡 = 8.00，实测）
            if data["month_cost"] is None:
                data["month_cost"] = to_float(fee_rsp.get("realFee"))
            extra = data["extra"]
            for src_key, dst in (("oweFee", "owed"), ("payType", "pay_type"),
                                 ("userSpecBalance", "user_spec_balance")):
                val = to_float(fee_rsp.get(src_key))
                if val is not None:
                    extra[dst] = val
                elif fee_rsp.get(src_key):
                    extra[dst] = fee_rsp.get(src_key)
        # uniApi 需三头：csrf-token（页面内联 window.user.csrf，每次加载轮换）
        # + e76ccba…（本函数生成的关联令牌，服务器仅回显校验）+ Lrsbhbg8（bundle 常量）
        uni_headers: dict[str, str] = {}
        try:
            page = await self._cffi_wx(
                "https://wx.10086.cn/website/spa/main/newHome")
            if page:
                m_csrf = re.search(r'"csrf":"([^"]+)"', page[1] or "")
                if m_csrf:
                    uni_headers["csrf-token"] = m_csrf.group(1)
            uni_headers[FP_HEADER_NAME] = _gen_fingerprint(
                self._query_headers().get("User-Agent", "Chrome/126"))
            uni_headers["Lrsbhbg8"] = LRSBHBG8_VALUE
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("移动 uniApi 头准备失败: %s", err)

        margin = await self._wx_query(
            MOBILE_MARGIN_URL_V2, method="POST",
            json_body={"operation": "getCustomerKeyMessage"},
            extra_headers=uni_headers)
        if margin:
            self._parse_margin(margin, data)
        # 积分（明文 JSON；必须走 curl_cffi 通道——wx.10086.cn 拦截 aiohttp TLS）
        try:
            score = await self._wx_query(
                MOBILE_SCORE_URL, top_keys={"qryInfoRsp"})
            pv = to_float((score.get("qryInfoRsp") or {}).get("pointValue"))
            if pv is not None:
                data["points"] = pv
        except CarrierAuthError:
            raise
        except CarrierError as err:
            _LOGGER.debug("移动积分查询失败（忽略）: %s", err)

        # 归属地 / 套餐code（getMarginQueryInfo）
        try:
            qry = await self._wx_query(
                MOBILE_MARGIN_QRY_URL_V2, method="POST",
                json_body={"operation": "getCustomerKeyMessage"},
                extra_headers=uni_headers)
            if not qry:
                raise CarrierError("归属地接口无响应")
            assign = ((qry.get("data") or {}).get("resultData") or {}).get("customerAssignment")
            if assign:
                data["location"] = assign
            tariff = ((qry.get("data") or {}).get("resultData") or {}).get("tariffPackages")
            if tariff and not data["plan_name"]:
                data["plan_name"] = tariff
        except CarrierError as err:
            _LOGGER.debug("移动归属地查询失败（忽略）: %s", err)

    @parse_safe
    def _parse_margin(self, payload: dict[str, Any], data: dict[str, Any]) -> None:
        node = payload.get("data")
        rd = node.get("resultData") if isinstance(node, dict) else None
        if not isinstance(rd, dict):
            return

        def _item(raw: dict) -> tuple[Any, Any, Any, str]:
            return (raw.get("usedNum"), raw.get("sumNum"), raw.get("remainNum"),
                    str(raw.get("unit") or "04"))

        def _scale(value: Any, unit: str) -> Any:
            num = to_float(value)
            if num is None:
                return None
            return round(num / 1024, 2) if unit == "03" else round(num, 2)

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
            used, total, remain, _unit = _item(plan)
            data["voice"] = usage_block(used, total, remain)

        sms = rd.get("planRemainMSGInfo") or rd.get("planRemianMSGInfo") or {}
        if sms:
            note = sms.get("notePlanRemain") or sms.get("notePlanRemian") or {}
            used, total, remain, _unit = _item(note)
            if used is None and total is None:
                note = sms.get("totalInfo") or {}
                used, total, remain, _unit = _item(note)
            data["sms"] = usage_block(used, total, remain)

    def export_session_cookies(self) -> str:
        """登录成功后导出会话 Cookie（SSO + JSESSIONID），供 coordinator 复用。"""
        merged = self._parse_cookie_header()
        return "; ".join(f"{k}={v}" for k, v in merged.items())

    def export_credentials(self) -> dict[str, Any]:
        exported = {"device_id": self._device_id}
        # native 通道 refreshSession 会滑动顺延 Cookie；把合并后的会话带回写
        merged = self._parse_cookie_header()
        if merged:
            exported["cookies"] = "; ".join(f"{k}={v}" for k, v in merged.items())
        return exported


def empty_snapshot(carrier: str, phone: str) -> dict[str, Any]:
    """Local import to avoid circular dependency with base."""
    from .base import empty_data

    return empty_data(carrier, phone)
