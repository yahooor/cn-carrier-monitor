"""Diagnostics support for 中国运营商话费监控.

Redacted config-entry dump: 下载诊断信息时自动抹去密码 / token / cookie /
openid / 手机号等敏感字段，只保留结构与配置形态，便于 issue 排查。
"""

from __future__ import annotations

import collections
import json
import logging
from pathlib import Path
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
from .event_log import FILE_NAME, MAX_RECENT_EVENTS

_LOGGER = logging.getLogger(__name__)

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


def _summarize_events(path: str) -> dict[str, Any]:
    """读事件日志尾部，输出失败分类计数与最近几条摘要。

    只统计 event/category 计数，不输出 phone 等原始字段（诊断包会随
    issue 外发，手机号与时序画像按威胁模型 R2 不落盘）。
    """
    file = Path(path)
    if not file.exists():
        return {}
    try:
        lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    tail = lines[-200:]
    counts: dict[str, dict[str, int]] = collections.defaultdict(
        lambda: collections.defaultdict(int)
    )
    recent: list[dict[str, str]] = []
    for line in tail:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        event = str(record.get("event") or "unknown")
        category = str(record.get("category") or "")
        counts[event][category or "-"] += 1
        if len(recent) < MAX_RECENT_EVENTS:
            recent.append(
                {
                    "ts": str(record.get("ts") or ""),
                    "event": event,
                    "category": category,
                }
            )
    return {
        "counts": {k: dict(v) for k, v in counts.items()},
        "recent": recent,
    }


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
            # 会话健康度 + 失败计数：排障时一眼区分"掉线/熔断/正常"
            "session_state": getattr(coordinator, "session_state", None),
            "fail_streak": getattr(coordinator, "_fail_streak", 0),
            "auth_fail_streak": getattr(coordinator, "_auth_fail_streak", 0),
            # 最近一次失败的分类/摘要（不含凭证），排障时比翻日志快
            "last_error": getattr(coordinator, "last_error", None),
        }
        # 事件日志摘要放 executor 读（diagnostics 是 async 上下文）
        try:
            diagnostics["recent_events"] = await hass.async_add_executor_job(
                _summarize_events, hass.config.path(FILE_NAME)
            )
        except Exception as err:  # noqa: BLE001 - 诊断信息缺失不应让下载失败
            _LOGGER.warning("读取事件日志摘要失败（忽略）: %s", err)
    return diagnostics
