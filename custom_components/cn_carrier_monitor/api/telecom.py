"""China Telecom (中国电信) client — 掌上营业厅 app protocol.

Login:  POST appgologin.189.cn:9031/login/client/userLoginNormal (服务密码)
Query:  POST appfuwu.189.cn:9021/query/qryImportantData

Protocol details ported from ChinaTelecomMonitor / ha_china_comm:
- phone & password are Caesar-shifted by +2
- loginAuthCipherAsymmertric = RSA-PKCS1v15("iPhone 14 15.4.0{dev12}{phone}{ts}{password}0$$$0.")
- token / provinceCode / cityCode come back in responseData.data.loginSuccessResult
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import aiohttp

from ..const import (
    TELECOM_CLIENT_TYPE,
    TELECOM_LOGIN_URL,
    TELECOM_LOGIN_V2_HOSTS,
    TELECOM_QUERY_URL,
    TELECOM_SHOP_ID,
    TELECOM_SOURCE,
    TELECOM_SOURCE_PASSWORD,
    TELECOM_UA,
)
from .base import (
    CarrierAuthError,
    CarrierClient,
    empty_data,
    CarrierError,
    finish,
    to_float,
    to_int,
    usage_block,
)
from .crypto import caesar, rsa_encrypt_b64

_LOGGER = logging.getLogger(__name__)

RSA_PUB_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDBkLT15ThVgz6/NOl6s8GNPofd"
    "WzWbCkWnkaAm7O2LjkM1H7dMvzkiqdxU02jamGRHLX/ZNMCXHnPcW/sDhiFCBN18"
    "qFvy8g6VYb9QtroI09e176s+ZCtiv7hbin2cCTj99iUpnEloZm19lwHyo69u5UMi"
    "PMpq0/XKBO8lYhN/gwIDAQAB"
)

# Pocket-Carrier 新一代接口（appgologinsz，同一把 1024 位公钥）
RSA_PUB_B64_V2 = RSA_PUB_B64

LOGIN_ERROR_KEYS = {
    "3005": "telecom_need_captcha",
    "3006": "telecom_device_untrusted",
    "3007": "telecom_too_frequent",
    "3008": "telecom_account_locked",
    "3009": "telecom_password_locked",
}

# 会话失效判定（Pocket-Carrier 验证：仅登录态类 code/关键词，避免业务提示误杀）
AUTH_EXPIRED_CODES = ("1001", "2001", "9999", "X201", "X110")
AUTH_EXPIRED_HINTS = ("token", "重新登录", "鉴权失败", "未登录", "登录已失效", "凭证", "会话")

KB = 1024.0


def _kb_to_gb(value: Any) -> float | None:
    num = to_float(value)
    return round(num / KB, 2) if num is not None else None


def _parse_sms_count(value: Any) -> int:
    if value is None:
        return 0
    match = __import__("re").search(r"[\d.]+", str(value))
    return int(float(match.group(0))) if match else 0


class TelecomClient(CarrierClient):
    """服务密码登录的电信客户端。"""

    carrier = "telecom"

    def __init__(self, session: aiohttp.ClientSession, data: dict[str, Any]) -> None:
        super().__init__(session, data)
        self._password = str(data.get("password") or "")
        self._device_id = str(data.get("device_id") or "")
        self._token = str(data.get("token") or "")
        self._province_code = str(data.get("province_code") or "")
        self._city_code = str(data.get("city_code") or "")
        # 短信登录（v2 接口族）运行态：uid 按号码确定性生成，避免跨步骤不一致触发 121
        import hashlib as _hashlib

        self._uid = _hashlib.md5(f"CT_IOS_11_{self.phone}".encode()).hexdigest()
        self._key = ""
        self._isct = "0"
        self._headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=UTF-8",
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
            "user-agent": TELECOM_UA,
        }

    # ------------------------------------------------------------------ login

    def _login_body(self, phone: str, password: str, ts: str) -> dict[str, Any]:
        device_uid = f"3{phone}"
        trusted = self._device_id[:12] if self._device_id else phone
        enc_str = (
            f"iPhone 14 15.4.0{trusted}{phone}{ts}{password}0$$$0."
        )
        return {
            "content": {
                "fieldData": {
                    "accountType": "",
                    "authentication": caesar(password),
                    "deviceUid": device_uid,
                    "isChinatelecom": "0",
                    "loginAuthCipherAsymmertric": rsa_encrypt_b64(RSA_PUB_B64, enc_str),
                    "loginType": "4",
                    "phoneNum": caesar(phone),
                    "systemVersion": "15.4.0",
                    "androidId": caesar(self._device_id) if self._device_id else "",
                },
                "attach": "iPhone",
            },
            "headerInfos": {
                "code": "userLoginNormal",
                "clientType": TELECOM_CLIENT_TYPE,
                "timestamp": ts,
                "shopId": TELECOM_SHOP_ID,
                "source": TELECOM_SOURCE,
                "sourcePassword": TELECOM_SOURCE_PASSWORD,
                "userLoginName": caesar(phone),
            },
        }

    async def async_login_sms_send(self) -> bool:
        """电信短信验证码下发（自动过滑块，Pocket-Carrier 方案的纯 Python 移植）。

        滑块缺口定位改用 PIL+朴素灰度对比，不依赖 numpy。
        """
        # step1: 预取 key
        p1 = {
            "deviceUid": self._uid, "imsi": "", "key": self._key,
            "payType": "", "phoneNum": caesar(self.phone), "salesProdId": "",
            "scene": "55", "signSignatureString": "", "validationCode": "",
        }
        _hdr, data1 = await self._login_v2("getLoginRandomCode", p1)
        if data1.get("key"):
            self._key = data1["key"]
        if data1.get("isChinatelecom") is not None:
            self._isct = str(data1["isChinatelecom"])

        import base64 as _b64
        import io as _io

        for _attempt in range(5):
            p2 = {
                "account": caesar(self.phone), "clientType": "1",
                "deviceUid": caesar(self._uid), "scene": "1",
            }
            _hdr, d = await self._login_v2("getSliderVerificationPicture", p2)
            bg = d.get("backgroundImg") or d.get("bigImg")
            slider = d.get("sliderImg") or d.get("smallImg")
            if not bg or not slider or not d.get("key") or not d.get("extra"):
                _LOGGER.debug("滑块图不完整: keys=%s", sorted(d.keys()))
                continue
            try:
                hx, pw, bw = _locate_notch(bg, slider)
            except Exception as err:  # noqa: BLE001 - image decode failure
                _LOGGER.debug("滑块图解析失败: %s", err)
                continue
            dist = hx / (bw - pw)
            kb = _b64.b64decode(d["extra"])
            st = int(__import__("time").time() * 1000)
            n = 15
            track = "%".join(f"{(hx * i / n) / (bw - pw):.3f}#0.000#{st + i * 40}"
                             for i in range(1, n + 1))
            from .crypto import aes_ecb_encrypt_b64
            vp = {
                "account": caesar(self.phone), "clientType": "1",
                "deviceUid": caesar(self._uid),
                "distance": aes_ecb_encrypt_b64("%.4f" % dist, kb),
                "endTime": aes_ecb_encrypt_b64("%d" % (st + 600), kb),
                "startTime": aes_ecb_encrypt_b64("%d" % st, kb),
                "key": d["key"],
                "scene": "1", "shopId": "20002",
                "slidingTrack": aes_ecb_encrypt_b64(track, kb),
            }
            _hdr, j = await self._login_v2("verificationSliderPicture", vp)
            sign = j.get("signSignatureString")
            if sign:
                p3 = {
                    "deviceUid": self._uid, "imsi": "", "key": self._key,
                    "payType": "", "phoneNum": caesar(self.phone), "salesProdId": "",
                    "scene": "55", "signSignatureString": sign, "validationCode": "",
                }
                _hdr3, data3 = await self._login_v2("getLoginRandomCode", p3)
                if data3.get("key"):
                    self._key = data3["key"]
                if data3.get("isChinatelecom") is not None:
                    self._isct = str(data3["isChinatelecom"])
                code = str(_hdr3.get("resultCode") or "")
                desc = str(_hdr3.get("resultDesc") or "")
                if code in ("0", "0000") or "成功" in desc or "60s" in desc or "重复获取" in desc:
                    return True
            await __import__("asyncio").sleep(0.5)
        raise CarrierError(
            "telecom_sms_send_failed",
            f"自动过滑块或下发短信失败(尝试{_attempt + 1}轮)，可改用服务密码登录",
        )

    async def async_login_sms_submit(self, code: str) -> dict[str, Any]:
        """电信短信验证码登录（loginType=2）。"""
        import base64 as _b64
        import time as _time

        ts = _time.strftime("%Y%m%d%H%M%S")
        pad = lambda s, n: (s or "")[:n].ljust(n, "$")  # noqa: E731
        model_prefix = "iPhone 11"[:10].ljust(10, " ")
        plain = (model_prefix + "16.1." + self._uid[:12]
                 + self.phone[:11] + ts[:14] + pad(code, 6)
                 + pad("0", 4) + pad("0.000000", 2))
        cipher = _b64.b64encode(
            __import__("cryptography.hazmat.primitives.serialization", fromlist=["load_der_public_key"])
            .load_der_public_key(__import__("base64").b64decode(RSA_PUB_B64_V2))
            .encrypt(plain.encode(), __import__("cryptography.hazmat.primitives.asymmetric.padding", fromlist=["PKCS1v15"]).PKCS1v15())
        ).decode()
        fd = {
            "accountType": "",
            "authentication": _b64.b64encode(caesar(code).encode()).decode(),
            "clientType": "1", "deviceUid": self._uid,
            "isChinatelecom": self._isct or "1",
            "loginAuthCipherAsymmertric": cipher, "loginType": "2",
            "phoneNum": caesar(self.phone), "signSignatureString": "",
            "systemVersion": "16.1.1",
        }
        hdr, data = await self._login_v2(
            "userLoginNormal", fd,
            hdr_extra={"userLoginName": caesar(self.phone), "timestamp": ts},
        )
        login_result = (data or {}).get("loginSuccessResult") or {}
        token = login_result.get("token")
        if not token:
            desc = str((data or {}).get("loginFailResult") or {}).get("reason") or hdr.get("resultDesc") or "登录未成功"
            raise CarrierAuthError("telecom_sms_login_failed", str(desc))
        return {
            "phone": self.phone,
            "password": "",
            "token": token,
            "province_code": str(login_result.get("provinceCode") or ""),
            "city_code": str(login_result.get("cityCode") or ""),
        }

    async def _login_v2(self, code: str, field_data: dict, hdr_extra: dict | None = None):
        """新一代登录族接口（appgologinsz.189.cn，source=120002）。"""
        import time as _time

        hdr = {
            "broadAccount": "", "broadToken": "", "fixedLineAccount": "",
            "fixedLineToken": "", "provinceCode": "", "code": code,
            "source": "120002", "sourcePassword": "TiqmIZ",
            "userLoginName": "", "clientType": "#13.4.0#channel50#iPhone 11 Pro Max#",
            "token": "", "timestamp": _time.strftime("%Y%m%d%H%M%S"), "shopId": "20004",
        }
        if hdr_extra:
            hdr.update(hdr_extra)
        body = {"headerInfos": hdr, "content": {"attach": "iPhone", "fieldData": field_data}}
        import json as _json

        last_err: Exception | None = None
        for host in TELECOM_LOGIN_V2_HOSTS:
            try:
                status, resp = await self._request(
                    "POST", f"{host}/login/client/{code}", json_body=body,
                    headers=self._headers, timeout=25,
                )
            except CarrierError as err:
                _LOGGER.debug("v2 host %s 不可达，尝试下一个: %s", host, err)
                last_err = err
                continue
            try:
                result = _json.loads(resp)
            except ValueError as err:
                raise CarrierError(f"登录接口响应异常: {resp[:120]}") from err
            rd = result.get("responseData") or {}
            return rd, rd.get("data") or {}
        raise CarrierError(f"登录接口不可达: {last_err}")

    async def async_login(self, phone: str, password: str) -> dict[str, Any]:
        """Login with the service password; returns credentials to persist."""
        if not phone or not password:
            raise CarrierError("请填写手机号和服务密码")
        ts = datetime.now().strftime("%Y%m%d%H%M00")
        status, body = await self._request(
            "POST",
            TELECOM_LOGIN_URL,
            json_body=self._login_body(phone, password, ts),
            headers=self._headers,
        )
        if status != 200:
            raise CarrierError(f"登录接口 HTTP {status}")
        import json

        try:
            result = json.loads(body)
        except ValueError as err:
            raise CarrierError(f"登录响应异常: {body[:120]}") from err
        resp_data = result.get("responseData") or {}
        if resp_data.get("resultCode") != "0000":
            code = str(resp_data.get("resultCode") or "")
            if code in LOGIN_ERROR_KEYS:
                raise CarrierAuthError(LOGIN_ERROR_KEYS[code])
            fail = (resp_data.get("data") or {}).get("loginFailResult") or {}
            reason = str(fail.get("reason") or resp_data.get("resultDesc") or code)
            # 服务端明确拒绝（含"手机号或密码有误"）→ 归类为凭据错误
            raise CarrierAuthError("telecom_invalid_credentials", reason)
        login_result = (resp_data.get("data") or {}).get("loginSuccessResult") or {}
        token = login_result.get("token")
        if not token:
            raise CarrierError("登录成功但未返回 token")
        return {
            "phone": phone,
            "password": password,
            "token": token,
            "province_code": str(login_result.get("provinceCode") or ""),
            "city_code": str(login_result.get("cityCode") or ""),
        }

    # ------------------------------------------------------------------ query

    def _query_body(self, code: str, extra_field: dict[str, Any]) -> dict[str, Any]:
        ts = datetime.now().strftime("%Y%m%d%H%M00")
        return {
            "content": {"fieldData": extra_field},
            "headerInfos": {
                "code": code,
                "clientType": TELECOM_CLIENT_TYPE,
                "timestamp": ts,
                "shopId": TELECOM_SHOP_ID,
                "source": TELECOM_SOURCE,
                "sourcePassword": TELECOM_SOURCE_PASSWORD,
                "userLoginName": caesar(self.phone),
                "token": self._token,
            },
        }

    async def _qry_important_data(self) -> dict[str, Any]:
        field = {
            "provinceCode": self._province_code or "600101",
            "cityCode": self._city_code or "8441900",
            "shopId": TELECOM_SHOP_ID,
            "isChinatelecom": "0",
            "account": caesar(self.phone),
        }
        status, body = await self._request(
            "POST",
            TELECOM_QUERY_URL,
            json_body=self._query_body("qryImportantData", field),
            headers=self._headers,
        )
        import json

        try:
            result = json.loads(body)
        except ValueError as err:
            raise CarrierError(f"套餐查询响应异常: {body[:120]}") from err
        # 精确判定会话失效（headerInfos.code / reason 关键词，Pocket-Carrier 验证），
        # 业务性提示（如详单授权）不会被误判
        hdr = result.get("headerInfos") or {}
        hdr_code = str(hdr.get("code") or "")
        hdr_reason = str(hdr.get("reason") or "")
        if hdr_code in AUTH_EXPIRED_CODES or any(
            kw in hdr_reason.lower() for kw in AUTH_EXPIRED_HINTS
        ):
            raise CarrierAuthError("telecom_session_expired", f"{hdr_code}: {hdr_reason}")
        return result

    async def async_fetch(self) -> dict[str, Any]:
        data = empty_data("telecom", self.phone)
        if not self._password and not self._token:
            raise CarrierAuthError("telecom_credentials_missing")
        if not self._token:
            creds = await self.async_login(self.phone, self._password)
            self._token = creds["token"]
            self._province_code = creds["province_code"]
            self._city_code = creds["city_code"]

        result = await self._qry_important_data()
        resp_data = result.get("responseData") or {}
        if resp_data.get("resultCode") != "0000" or not resp_data.get("data"):
            # token may have expired — re-login once and retry (password mode only)
            if not self._password:
                raise CarrierAuthError("telecom_session_expired", "短信登录会话已失效，请重新验证")
            _LOGGER.debug("电信查询失败，尝试重新登录: %s", str(result)[:200])
            creds = await self.async_login(self.phone, self._password)
            self._token = creds["token"]
            self._province_code = creds["province_code"]
            self._city_code = creds["city_code"]
            result = await self._qry_important_data()
            resp_data = result.get("responseData") or {}
            if resp_data.get("resultCode") != "0000" or not resp_data.get("data"):
                raise CarrierAuthError("telecom_session_expired", "查询失败，请重新登录")

        self._parse_summary(resp_data["data"], data)
        return finish(data)

    # ------------------------------------------------------------------ parse

    def _parse_summary(self, payload: dict[str, Any], data: dict[str, Any]) -> None:
        flow_info = payload.get("flowInfo") or {}
        voice_info = payload.get("voiceInfo") or {}
        sms_info = payload.get("smsInfo") or {}
        integral_info = payload.get("integralInfo") or {}
        balance_info = payload.get("balanceInfo") or {}

        total_amount = flow_info.get("totalAmount") or {}
        used_num = to_int(total_amount.get("used")) or 0
        bal_num = to_int(total_amount.get("balance")) or 0
        data["flow_total"] = usage_block(
            _kb_to_gb(used_num), _kb_to_gb(used_num + bal_num), _kb_to_gb(bal_num)
        )

        common = flow_info.get("commonFlow") or {}
        c_used = to_int(common.get("used")) or 0
        c_bal = to_int(common.get("balance")) or 0
        data["flow_general"] = usage_block(_kb_to_gb(c_used), _kb_to_gb(c_used + c_bal), _kb_to_gb(c_bal))

        special = flow_info.get("specialAmount") or {}
        s_used = to_int(special.get("used")) or 0
        s_bal = to_int(special.get("balance")) or 0
        data["flow_directed"] = usage_block(_kb_to_gb(s_used), _kb_to_gb(s_used + s_bal), _kb_to_gb(s_bal))

        voice = voice_info.get("voiceDataInfo") or {}
        data["voice"] = usage_block(voice.get("used"), voice.get("total"), voice.get("balance"))

        sms_info_data = sms_info.get("smsDataInfo") or sms_info.get("dataInfo") or {}
        sms_used = sms_balance = sms_total = None
        if sms_info_data:
            sms_used = to_int(sms_info_data.get("used") or sms_info_data.get("usedAmount"))
            sms_balance = to_int(
                sms_info_data.get("balance") or sms_info_data.get("remainAmount")
            )
            sms_total = to_int(
                sms_info_data.get("total") or sms_info_data.get("totalAmount")
            )
        if sms_used is None and sms_balance is None:
            # fall back to the flowList item titled 短信
            for item in flow_info.get("flowList") or []:
                if not isinstance(item, dict) or "短信" not in str(item.get("title", "")):
                    continue
                if "已用" in str(item.get("leftTitle", "")) and "剩余" in str(
                    item.get("rightTitle", "")
                ):
                    sms_used = _parse_sms_count(item.get("leftTitleHh"))
                    sms_balance = _parse_sms_count(item.get("rightTitleHh"))
                break
        if sms_used is not None or sms_balance is not None or sms_total is not None:
            data["sms"] = usage_block(sms_used, sms_total, sms_balance)

        index_balance = balance_info.get("indexBalanceDataInfo") or {}
        balance_val = to_float(index_balance.get("balance")) or 0.0
        arrear_val = to_float(index_balance.get("arrear")) or 0.0
        if balance_val == 0 and arrear_val > 0:
            data["balance"] = -arrear_val
        else:
            data["balance"] = balance_val

        bill_region = balance_info.get("phoneBillRegion") or {}
        if isinstance(bill_region, dict):
            data["month_cost"] = to_float(
                str(bill_region.get("subTitleHh") or "").replace("元", "")
            )

        data["points"] = to_int(integral_info.get("integral"))

        flow_items: list[dict[str, Any]] = []
        for item in flow_info.get("flowList") or []:
            if not isinstance(item, dict) or "流量" not in str(item.get("title", "")):
                continue
            used = remain = total = None
            left, right = str(item.get("leftTitle", "")), str(item.get("rightTitle", ""))
            if "已用" in left and "剩余" in right:
                used = _kb_to_gb(item.get("leftTitleHh"))
                remain = _kb_to_gb(item.get("rightTitleHh"))
                total = round((used or 0) + (remain or 0), 2)
            elif "超出" in left and "/" in str(item.get("rightTitleEnd", "")):
                remain = -(_kb_to_gb(item.get("leftTitleHh")) or 0)
                used = _kb_to_gb(str(item.get("rightTitleEnd", "0/0")).split("/")[1])
                total = round((used or 0) + (remain or 0), 2)
            elif "已用" in left and "降速" in right:
                import re

                match = re.search(r"(\d+[KMGT]B)", right)
                total = _flow_str_to_kb_gb(match.group(1)) if match else None
                used = _kb_to_gb(item.get("leftTitleHh"))
                remain = round((total or 0) - (used or 0), 2)
            else:
                continue
            flow_items.append(
                {"name": item.get("title"), "used": used, "remain": remain, "total": total}
            )
        if flow_items:
            data["extra"]["flow_items"] = flow_items


def _flow_str_to_kb_gb(size_str: str) -> float:
    """Convert a string like '20GB' to GB."""
    units = {"KB": 1 / 1024, "MB": 1, "GB": 1, "TB": 1024}
    m = __import__("re").match(r"([\d.]+)([KMGT]B)", size_str.strip())
    if not m:
        return 0.0
    return round(float(m.group(1)) * units[m.group(2)], 2)


def _locate_notch(bg_b64: str, piece_b64: str) -> tuple[int, int, int]:
    """滑块缺口定位（Pocket-Carrier locate_notch 的纯 PIL 实现）。

    缺口在背景图上是暗色凹槽：取滑块 mask 覆盖区域平均亮度最低的 x。
    y 在原图 y0 附近 ±8px 搜索。返回 (x, piece_width, bg_width)。
    """
    import base64 as b64
    import io as _io

    from PIL import Image

    bg = Image.open(_io.BytesIO(b64.b64decode(bg_b64))).convert("L")
    pc = Image.open(_io.BytesIO(b64.b64decode(piece_b64))).convert("RGBA")
    bg_w, bg_h = bg.size
    mask = pc.split()[3].point(lambda a: 255 if a > 128 else 0)
    bbox = mask.getbbox()
    if not bbox:
        raise ValueError("slider mask empty")
    x0, y0, _x1, _y1 = bbox
    m = mask.crop(bbox)
    pw, ph = m.size
    mask_px = [(xx, yy) for yy in range(0, ph, 2) for xx in range(0, pw, 2)
               if m.getpixel((xx, yy))]
    if not mask_px:
        raise ValueError("slider mask has no opaque pixels")
    bg_px = bg.load()
    best_x, best_score = -1, float("-inf")
    for y in range(max(0, y0 - 8), min(bg_h - ph, y0 + 8) + 1):
        for x in range(0, bg_w - pw + 1, 2):
            total = 0
            for xx, yy in mask_px:
                total += bg_px[x + xx, y + yy]
            score = -total / len(mask_px)  # darker wins
            if score > best_score:
                best_x, best_score = x, score
    if best_x < 0:
        raise ValueError("notch not found")
    return best_x, pw, bg_w
