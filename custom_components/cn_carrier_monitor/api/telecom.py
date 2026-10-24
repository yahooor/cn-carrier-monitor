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
    TELECOM_QUERY_URL,
    TELECOM_SHOP_ID,
    TELECOM_SOURCE,
    TELECOM_SOURCE_PASSWORD,
    TELECOM_UA,
)
from .base import (
    CarrierAuthError,
    CarrierClient,
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

LOGIN_ERROR_KEYS = {
    "3005": "telecom_need_captcha",
    "3006": "telecom_device_untrusted",
    "3007": "telecom_too_frequent",
    "3008": "telecom_account_locked",
    "3009": "telecom_password_locked",
}

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
            reason = fail.get("reason") or resp_data.get("resultDesc") or code
            raise CarrierError(f"电信登录失败: {reason}")
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
            return json.loads(body)
        except ValueError as err:
            raise CarrierError(f"套餐查询响应异常: {body[:120]}") from err

    async def async_fetch(self) -> dict[str, Any]:
        data = empty_data("telecom", self.phone)
        if not self._password:
            raise CarrierAuthError("缺少服务密码")
        if not self._token:
            creds = await self.async_login(self.phone, self._password)
            self._token = creds["token"]
            self._province_code = creds["province_code"]
            self._city_code = creds["city_code"]

        result = await self._qry_important_data()
        resp_data = result.get("responseData") or {}
        if resp_data.get("resultCode") != "0000" or not resp_data.get("data"):
            # token may have expired — re-login once and retry
            _LOGGER.debug("电信查询失败，尝试重新登录: %s", str(result)[:200])
            creds = await self.async_login(self.phone, self._password)
            self._token = creds["token"]
            self._province_code = creds["province_code"]
            self._city_code = creds["city_code"]
            result = await self._qry_important_data()
            resp_data = result.get("responseData") or {}
            if resp_data.get("resultCode") != "0000" or not resp_data.get("data"):
                raise CarrierAuthError("电信查询失败，请重新登录")

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
