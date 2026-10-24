"""Diagnostics support for 中国运营商话费监控.

Redacted config-entry dump: 下载诊断信息时自动抹去密码 / token / cookie /
openid / 手机号等敏感字段，只保留结构与配置形态，便于 issue 排查。
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_APP_ID,
    CONF_COOKIES,
    CONF_OPENID,
    CONF_PASSWORD,
    CONF_PHONE,
    CONF_REQUEST_BODY,
    CONF_REQUEST_URL,
    CONF_TOKEN,
    DOMAIN,
)

SENSITIVE_KEYS = {
    CONF_PASSWORD,
    CONF_TOKEN,
    CONF_COOKIES,
    CONF_OPENID,
    CONF_APP_ID,
    CONF_REQUEST_BODY,
    CONF_REQUEST_URL,
    "device_id",
    "phone_detected",
}


def _redact(value: Any, keep_structure: bool = True) -> Any:
    if isinstance(value, dict):
        return {
            k: ("**REDACTED**" if k in SENSITIVE_KEYS else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _mask_phone(phone: str) -> str:
    return f"{phone[:3]}****{phone[7:]}" if len(phone) == 11 else phone


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    data = _redact(dict(entry.data))
    phone = str(entry.data.get(CONF_PHONE, ""))
    if phone:
        data[CONF_PHONE] = _mask_phone(phone)

    diagnostics: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "carrier": entry.data.get("carrier"),
            "auth_mode": entry.data.get("auth_mode"),
            "interval": entry.options.get("interval", entry.data.get("interval")),
            "disabled_by": entry.disabled_by,
        },
        "config_data": data,
    }
    if coordinator is not None:
        snapshot = coordinator.data or {}
        diagnostics["last_update"] = {
            "success": snapshot.get("updated_at"),
            "fields_present": sorted(
                k for k, v in snapshot.items()
                if v not in (None, {}, []) and k != "extra"
            ),
            "extra_keys": sorted((snapshot.get("extra") or {}).keys()),
        }
        diagnostics["coordinator"] = {
            "last_update_success": coordinator.last_update_success,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
        }
    return diagnostics
