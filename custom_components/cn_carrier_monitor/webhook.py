"""Webhook: 接收手机端转发器（smsForward / iOS 快捷指令）POST 来的验证码，
自动完成对应账号的短信重登。

POST /api/webhook/cn_carrier_monitor_sms
Content-Type: application/json
{"phone": "19974410126", "code": "123456"}
（只有一个短信模式账号时可省略 phone，只发 {"code": "123456"}）

即使条目当前处于 setup_error（coordinator 不在内存），也会直接用条目数据
构造临时 client 完成重登并 reload 条目。
"""

from __future__ import annotations

import logging

import aiohttp
from aiohttp.web import Request

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .api import TelecomClient, UnicomClient
from .const import CARRIER_TELECOM, CARRIER_UNICOM, CONF_AUTH_MODE, DOMAIN

_LOGGER = logging.getLogger(__name__)

WEBHOOK_ID = f"{DOMAIN}_sms"

_SCHEMA = vol.Schema(
    {
        vol.Required("code"): cv.string,
        vol.Optional("phone", default=""): cv.string,
    }
)


def _sms_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(CONF_AUTH_MODE) == "sms"
        and entry.data.get("carrier") in (CARRIER_TELECOM, CARRIER_UNICOM)
    ]


async def _relogin(hass: HomeAssistant, entry: ConfigEntry, code: str) -> dict:
    """Build a throw-away client from entry data, submit the code, persist, reload."""
    data = dict(entry.data)
    connector = aiohttp.TCPConnector(family=4, ssl=True)
    session = aiohttp.ClientSession(connector=connector)
    try:
        if data.get("carrier") == CARRIER_TELECOM:
            client = TelecomClient(session, data)
            creds = await client.async_login_sms_submit(code)
            updates = {
                "token": creds["token"],
                "province_code": creds["province_code"],
                "city_code": creds["city_code"],
            }
        else:
            client = UnicomClient(session, data)
            creds = await client.async_sms_login(code)
            updates = {"token": creds["token"]}
    finally:
        await session.close()
    hass.config_entries.async_update_entry(entry, data={**data, **updates})
    await hass.config_entries.async_reload(entry.entry_id)
    return updates


async def async_handle_webhook(
    hass: HomeAssistant, webhook_id: str, request: Request
):
    """Handle incoming SMS-code webhook."""
    try:
        payload = _SCHEMA(await request.json())
    except (ValueError, vol.Invalid) as err:
        _LOGGER.warning("短信 webhook 载荷无效: %s", err)
        return webhook.json_response({"ok": False, "error": "invalid_payload"}, status=400)

    code = str(payload["code"]).strip()
    phone = str(payload["phone"]).strip()
    candidates = _sms_entries(hass)
    if phone:
        candidates = [e for e in candidates if e.data.get("phone") == phone]
    if not candidates:
        _LOGGER.warning("短信 webhook 没有匹配的短信模式账号 (phone=%s)", phone or "*")
        return webhook.json_response({"ok": False, "error": "no_matching_account"}, status=404)
    if len(candidates) > 1 and not phone:
        return webhook.json_response(
            {"ok": False, "error": "multiple_accounts_need_phone"}, status=400
        )

    entry = candidates[0]
    try:
        updates = await _relogin(hass, entry, code)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("短信自动重登失败: %s", err)
        return webhook.json_response({"ok": False, "error": str(err)[:160]}, status=500)
    _LOGGER.info("短信 webhook 重登成功: %s", entry.title)
    return webhook.json_response({"ok": True, "account": entry.title,
                                  "updated": sorted(updates)})


def async_register(hass: HomeAssistant) -> None:
    webhook.async_register(
        hass,
        DOMAIN,
        "sms_code",
        WEBHOOK_ID,
        async_handle_webhook,
        allowed_methods=["POST"],
    )
