"""China Broadnet (中国广电) client — 会话导入模式.

广电官网 (www.10099.com.cn) 位于阿里云 WAF 之后，短信登录无法纯 HTTP 复现。
与 liuliang-buddy 的做法一致：用户在浏览器里完成一次短信登录后，把查询接口
(qryUserRes) 的请求「Copy as cURL」粘贴进来；本客户端重放该请求并解析响应。

响应结构（parseBroadnetH5 已验证）：
  {status: "000000", data: {respCode: "000000", intfResultBean: {userResList: [...]}}}
  每项: busiType("5"=流量KB / "1"=语音 / 其余按名称识别短信)、discntName、
        balance(剩余)、highFee(总量)，支持「不限量」字样。
部分响应可能被 AES-CBC(hex) 加密，密钥与移动一致（1234123412ABCDEF）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import aiohttp

from ..const import REQUEST_TIMEOUT
from .base import (
    CarrierAuthError,
    CarrierClient,
    CarrierError,
    empty_data,
    finish,
    to_float,
    usage_block,
)
from .crypto import aes_cbc_decrypt, parse_curl, parse_maybe_json

_LOGGER = logging.getLogger(__name__)

AES_KEY = b"1234123412ABCDEF"
AES_IV = b"ABCDEF1234123412"

UNLIMITED_RE = re.compile(
    r"^(不限量|无限量|无限|不限|不限制|unlimited|no\s*limit)\s*(GB|MB|KB|B)?$",
    re.IGNORECASE,
)


def _decode_body(text: str) -> dict[str, Any] | None:
    value = text.strip()
    if not value:
        return None
    # 1) plain / JSONP
    parsed = parse_maybe_json(value)
    if parsed is not None:
        return parsed
    # 2) hex AES
    stripped = re.sub(r"\s+", "", value)
    if len(stripped) >= 32 and all(c in "0123456789abcdefABCDEF" for c in stripped):
        try:
            import binascii

            plain = aes_cbc_decrypt(AES_KEY, AES_IV, binascii.unhexlify(stripped))
            return parse_maybe_json(plain.decode("utf-8", errors="replace"))
        except (ValueError, binascii.Error):
            return None
    return None


def _is_auth_failure(node: dict[str, Any]) -> bool:
    text = json.dumps(node, ensure_ascii=False)[:3000].lower()
    return any(
        mark in text
        for mark in ("请登录", "未登录", "重新登录", "登录失效", "notlogin", "nologin")
    )


def _kb_amount(value: Any) -> float | None:
    """busiType=5 的 balance/highFee 单位为 KB → GB。"""
    text = str(value or "").strip()
    if not text or UNLIMITED_RE.match(text):
        return None
    num = to_float(text)
    return round(num / 1024, 2) if num is not None else None


class BroadnetClient(CarrierClient):
    """广电客户端：重放用户从浏览器捕获的 qryUserRes 请求。"""

    carrier = "broadnet"

    def __init__(self, session: aiohttp.ClientSession, data: dict[str, Any]) -> None:
        super().__init__(session, data)
        self._url = str(data.get("request_url") or "")
        self._method = str(data.get("request_method") or "GET").upper()
        self._body = str(data.get("request_body") or "")
        self._cookie = str(data.get("cookies") or "")

    @staticmethod
    def parse_import(source: str, cookie: str = "") -> dict[str, str]:
        """Normalize user input: full cURL, or URL(+cookie) pair."""
        parsed = parse_curl(source)
        url = parsed["url"]
        method = parsed["method"]
        body = parsed["body"]
        merged_cookie = parsed["cookie"] or cookie
        if not url:
            raise CarrierError("未识别到请求 URL，请粘贴完整的 cURL 或 URL")
        if not merged_cookie:
            raise CarrierError("缺少 Cookie，请在浏览器中一并复制请求的 Cookie 头")
        return {
            "request_url": url,
            "request_method": method or "GET",
            "request_body": body,
            "cookies": merged_cookie,
        }

    async def async_fetch(self) -> dict[str, Any]:
        data = empty_data("broadnet", self.phone)
        if not self._url or not self._cookie:
            raise CarrierAuthError("broadnet_session_missing")

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest",
            "Cookie": self._cookie,
            "Referer": "https://www.10099.com.cn/",
        }
        url = self._url
        body = self._body
        method = self._method if self._method in ("GET", "POST") else "GET"
        if method == "GET" and body:
            # captured body on a GET is usually form params folded in the URL
            parts = urlsplit(url)
            query = parse_qs(parts.query)
            query.update(parse_qs(body))
            url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
            body = ""

        status, text = await self._request(
            method,
            url,
            form=body if method == "POST" else None,
            headers=headers,
            timeout=REQUEST_TIMEOUT + 10,
        )
        if status in (401, 403):
            raise CarrierAuthError("broadnet_session_expired")
        if status >= 400:
            raise CarrierError(f"广电查询 HTTP {status}")
        payload = _decode_body(text)
        if payload is None:
            raise CarrierError(f"广电响应无法解析: {text[:120]}")

        outer_status = str(payload.get("status") or "")
        business = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if outer_status == "701" or _is_auth_failure(payload) or (
            isinstance(business, dict) and _is_auth_failure(business)
        ):
            raise CarrierAuthError("broadnet_session_expired")
        if isinstance(business, dict) and business.get("respCode") not in (None, "000000"):
            raise CarrierError(f"广电业务查询失败: {business.get('respCode')}")
        if outer_status not in ("", "000000", "0"):
            raise CarrierError(f"广电接口返回异常: {outer_status}")

        self._parse_user_res(business or {}, data)
        return finish(data)

    def _parse_user_res(self, business: dict[str, Any], data: dict[str, Any]) -> None:
        if not isinstance(business, dict):
            raise CarrierError("广电响应结构未知")
        bean = business.get("intfResultBean")
        items = (bean.get("userResList") if isinstance(bean, dict) else None) or []
        if not isinstance(items, list):
            raise CarrierError("广电余量明细结构异常")
        if not isinstance(items, list) or not items:
            raise CarrierError("广电未返回可识别的余量明细")

        flow_items: list[dict[str, Any]] = []
        general_used = general_remain = 0.0
        directed_used = directed_remain = 0.0
        has_general = has_directed = False
        voice_used = voice_total = 0.0
        has_voice = False
        sms_used = sms_total = 0.0
        has_sms = False

        for item in items:
            if not isinstance(item, dict):
                continue
            busi_type = str(item.get("busiType") or "")
            name = str(item.get("discntName") or item.get("itemName") or "").strip()
            if not name:
                continue
            if busi_type == "5":
                remain = _kb_amount(item.get("balance"))
                total = _kb_amount(item.get("highFee"))
                unlimited = bool(UNLIMITED_RE.match(str(item.get("balance") or "")))
                used = (
                    round(total - remain, 2)
                    if total is not None and remain is not None
                    else None
                )
                flow_items.append(
                    {"name": name, "used": used, "remain": remain, "total": total,
                     "unlimited": unlimited}
                )
                if remain is not None or used is not None:
                    if "定向" in name or "专属" in name:
                        has_directed = True
                        directed_used += used or 0
                        directed_remain += remain or 0
                    elif "通用" in name or "流量" in name:
                        has_general = True
                        general_used += used or 0
                        general_remain += remain or 0
            elif busi_type == "1" or "分钟" in str(item.get("unit") or ""):
                remain = to_float(item.get("balance"))
                total = to_float(item.get("highFee"))
                if remain is None and total is None:
                    continue
                has_voice = True
                voice_used += to_float(item.get("usedNum")) or max(
                    (total or 0) - (remain or 0), 0
                )
                voice_total += total or 0
            elif re.search(r"短信|短、彩信|短彩信", name):
                remain = to_float(item.get("balance"))
                total = to_float(item.get("highFee"))
                if remain is None and total is None:
                    continue
                has_sms = True
                sms_used += to_float(item.get("usedNum")) or max(
                    (total or 0) - (remain or 0), 0
                )
                sms_total += total or 0

        if has_general:
            data["flow_general"] = usage_block(
                round(general_used, 2),
                round(general_used + general_remain, 2),
                round(general_remain, 2),
            )
        if has_directed:
            data["flow_directed"] = usage_block(
                round(directed_used, 2),
                round(directed_used + directed_remain, 2),
                round(directed_remain, 2),
            )
        if flow_items:
            numeric = [i for i in flow_items if i["used"] is not None or i["remain"] is not None]
            if numeric:
                all_used = sum(i["used"] or 0 for i in numeric)
                all_remain = sum(i["remain"] or 0 for i in numeric)
                data["flow_total"] = usage_block(
                    round(all_used, 2), round(all_used + all_remain, 2), round(all_remain, 2)
                )
            data["extra"]["flow_items"] = flow_items
            unlimited_names = [i["name"] for i in flow_items if i.get("unlimited")]
            if unlimited_names:
                data["extra"]["flow_unlimited"] = unlimited_names
        if has_voice:
            data["voice"] = usage_block(
                round(voice_used), round(voice_total), round(voice_total - voice_used)
            )
        if has_sms:
            data["sms"] = usage_block(
                round(sms_used), round(sms_total), round(sms_total - sms_used)
            )

        if not (flow_items or data["voice"] or data["sms"]):
            raise CarrierError("广电响应缺少可确认的余量数据")

        # 话费余额：部分响应会附带 fee 字段
        fee = to_float(business.get("totalBalance") or business.get("balanceFee"))
        if fee is not None:
            data["balance"] = fee
