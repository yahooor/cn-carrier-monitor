"""Webhook: 接收手机端转发器（smsForward / iOS 快捷指令）POST 来的验证码，
自动完成对应账号的短信重登。

POST /api/webhook/cn_carrier_monitor_sms
Content-Type: application/json
{"phone": "19974410126", "code": "123456"}
（只有一个短信模式账号时可省略 phone，只发 {"code": "123456"}）

鉴权与限频（v1.5 起）：
- 条目 options/data 里配置 ``webhook_secret`` 后，请求必须带同值
  ``X-Secret`` 头或 body 的 ``secret`` 字段（常量时间比较）；
- 未配置密钥时只接受**内网/回环地址**的请求，避免 HA 暴露到公网后
  任何人都能拿 6 位验证码去撞号；
- 同一号码 10 分钟内最多 5 次提交，防暴力枚举。

即使条目当前处于 setup_error（coordinator 不在内存），也会直接用条目数据
构造临时 client 完成重登并 reload 条目。
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import time
from typing import Any

import aiohttp
import voluptuous as vol

# 注意：HA 的 homeassistant.components.webhook 模块**没有** json_response——
# 7 处错误响应曾误用 `webhook.json_response`，运行时全部 AttributeError，
# 被 HA 的兜底 except 吞成 200，转发器永远看到"成功"。
from aiohttp.web import Request, json_response
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .api import TelecomClient, UnicomClient
from .const import (
    CARRIER_TELECOM,
    CARRIER_UNICOM,
    CONF_AUTH_MODE,
    CONF_WEBHOOK_SECRET,
    DATA_SMS_STATE,
    DOMAIN,
)
from .net import build_connector

_LOGGER = logging.getLogger(__name__)

WEBHOOK_ID = f"{DOMAIN}_sms"

_SCHEMA = vol.Schema(
    {
        vol.Required("code"): cv.string,
        vol.Optional("phone", default=""): cv.string,
        # 早期 schema 没声明 secret，voluptuous 默认拒绝未知键，
        # 于是「body 里带密钥」这条路根本走不通，只有请求头有效
        vol.Optional("secret", default=""): cv.string,
    }
)

# 限频：同一账号 10 分钟内最多 5 次验证码提交
_RATE_WINDOW = 600.0
_RATE_MAX = 5
_ATTEMPTS: dict[str, list[float]] = {}


def _sms_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(CONF_AUTH_MODE) == "sms"
        and entry.data.get("carrier") in (CARRIER_TELECOM, CARRIER_UNICOM)
    ]


def _entry_secret(entry: ConfigEntry) -> str:
    return str(
        entry.options.get(CONF_WEBHOOK_SECRET)
        or entry.data.get(CONF_WEBHOOK_SECRET)
        or ""
    )


def _is_local(request: Request) -> bool:
    """请求是否来自内网/回环（未配置密钥时的最低门槛）。"""
    remote = request.remote
    if not remote:
        return False
    try:
        addr = ipaddress.ip_address(remote)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback


def _rate_limited(key: str) -> bool:
    now = time.monotonic()
    history = [t for t in _ATTEMPTS.get(key, []) if now - t < _RATE_WINDOW]
    if len(history) >= _RATE_MAX:
        _ATTEMPTS[key] = history
        return True
    history.append(now)
    _ATTEMPTS[key] = history
    return False


def _sms_state(hass: HomeAssistant, phone: str) -> dict[str, Any]:
    return hass.data.get(DATA_SMS_STATE, {}).get(phone) or {}


async def _relogin(hass: HomeAssistant, entry: ConfigEntry, code: str) -> dict:
    """Build a throw-away client from entry data, submit the code, persist, reload."""
    data = dict(entry.data)
    session = aiohttp.ClientSession(connector=build_connector(legacy_tls=True))
    try:
        if data.get("carrier") == CARRIER_TELECOM:
            client = TelecomClient(session, data)
            saved = _sms_state(hass, data.get("phone", ""))
            if saved:
                client.import_sms_state(saved)
            creds = await client.async_login_sms_submit(code)
            updates = {
                "token": creds["token"],
                "province_code": creds["province_code"],
                "city_code": creds["city_code"],
            }
        else:
            client = UnicomClient(session, data)
            # 联通 radomLogin 校验发码时的 deviceId，缺了会被判风控
            saved = _sms_state(hass, data.get("phone", ""))
            if saved:
                client.import_sms_state(saved)
            creds = await client.async_sms_login(code)
            updates = {"token": creds["token"], "app_id": creds.get("app_id", "")}
            updates = {k: v for k, v in updates.items() if v}
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
        return json_response({"ok": False, "error": "invalid_payload"}, status=400)

    code = str(payload["code"]).strip()
    phone = str(payload["phone"]).strip()
    provided_secret = str(
        request.headers.get("X-Secret") or payload.get("secret") or ""
    )

    candidates = _sms_entries(hass)
    if phone:
        candidates = [e for e in candidates if e.data.get("phone") == phone]
    if not candidates:
        _LOGGER.warning("短信 webhook 没有匹配的短信模式账号 (phone=%s)", phone or "*")
        return json_response({"ok": False, "error": "no_matching_account"}, status=404)
    if len(candidates) > 1 and not phone:
        return json_response(
            {"ok": False, "error": "multiple_accounts_need_phone"}, status=400
        )

    entry = candidates[0]
    expected = _entry_secret(entry)
    if expected:
        # compare_digest 的 str 版只接受 ASCII：用户配中文/emoji 密钥会
        # TypeError → 500。统一编码为字节后比较
        if not hmac.compare_digest(
            provided_secret.encode("utf-8"), expected.encode("utf-8")
        ):
            return json_response({"ok": False, "error": "bad_secret"}, status=403)
    elif not _is_local(request):
        # 未配密钥 + 来自公网：直接拒绝（验证码是账号登录凭据）
        _LOGGER.warning(
            "短信 webhook 拒绝公网请求（未配置 webhook_secret）: remote=%s",
            request.remote,
        )
        return json_response(
            {"ok": False, "error": "secret_required"}, status=403
        )

    if _rate_limited(phone or entry.entry_id):
        _LOGGER.warning("短信 webhook 触发限频: %s", entry.title)
        return json_response({"ok": False, "error": "rate_limited"}, status=429)

    try:
        updates = await _relogin(hass, entry, code)
    except Exception as err:  # noqa: BLE001 - 对外只给分类，细节进日志
        _LOGGER.warning("短信自动重登失败: %s", err)
        return json_response({"ok": False, "error": "relogin_failed"}, status=500)
    _LOGGER.info("短信 webhook 重登成功: %s", entry.title)
    # 重登成功 → reload 新建了 coordinator（_needs_human_notified=False），
    # 旧"需要人工"通知没人撤，必须在这里显式 dismiss，否则永久挂在界面上
    try:
        from homeassistant.components.persistent_notification import (
            async_dismiss as pn_dismiss,
        )

        pn_dismiss(hass, f"{DOMAIN}_reauth_{entry.entry_id}")
    except Exception as err:  # noqa: BLE001 - 清理失败不影响重登结果
        _LOGGER.debug("重登后清理掉线通知失败: %s", err)
    return json_response({"ok": True, "account": entry.title,
                          "updated": sorted(updates)})


def async_register(hass: HomeAssistant) -> None:
    webhook.async_register(
        hass,
        DOMAIN,
        "sms_code",
        WEBHOOK_ID,
        async_handle_webhook,
        allowed_methods=["POST"],
        # 不能设 local_only=True：短信转发器常从蜂窝网络（公网）回传。
        # 公网暴露由「密钥 or 内网来源」二选一兜底，见 async_handle_webhook。
        local_only=False,
    )
