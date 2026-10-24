"""DataUpdateCoordinator for China Carrier Monitor."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CarrierAuthError, CarrierError, create_client
from .const import DEFAULT_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class CarrierDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Per-account coordinator: one config entry = one phone account."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.carrier = entry.data["carrier"]
        minutes = entry.options.get("interval", entry.data.get("interval", DEFAULT_INTERVAL))
        try:
            minutes = max(int(minutes), 5)
        except (TypeError, ValueError):
            minutes = DEFAULT_INTERVAL
        # 每个账号独立会话，避免 Cookie 串号；强制 IPv4 + 公共 DNS
        from .net import build_connector

        self.session = aiohttp.ClientSession(connector=build_connector())
        self.client = create_client(self.carrier, self.session, dict(entry.data))
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{self.carrier}_{entry.data.get('phone', '')}",
            update_interval=timedelta(minutes=minutes),
        )

    # 连续失败断路器：连续 >=5 次失败后熔断 15 分钟（保护运营商风控额度）
    FAILURE_THRESHOLD = 5
    CIRCUIT_COOLDOWN = 900.0

    async def _async_update_data(self) -> dict[str, Any]:
        import time as _time

        if (
            getattr(self, "_fail_streak", 0) >= self.FAILURE_THRESHOLD
            and _time.time() - getattr(self, "_last_fail_at", 0.0) < self.CIRCUIT_COOLDOWN
        ):
            degraded = dict(self.data) if isinstance(self.data, dict) else {}
            degraded["extra"] = {
                **(degraded.get("extra") or {}),
                "circuit_open": True,
                "fail_streak": self._fail_streak,
            }
            return degraded
        try:
            data = await self.client.async_fetch()
        except CarrierAuthError as err:
            _LOGGER.info("%s 认证失败(%s)", self.entry.title, err)
            if self.supports_sms_relogin:
                self.hass.async_create_task(self._async_safe_send_relogin_sms())
            # 降级保实体（Pocket-Carrier 同策略）：有历史数据时返回旧值并标记
            # 登录失效，实体保持可用而不是整条卸载；首次（无数据）才走官方重认证
            if isinstance(self.data, dict) and self.data:
                degraded = dict(self.data)
                degraded["extra"] = {
                    **(degraded.get("extra") or {}),
                    "login_expired": True,
                    "login_error": str(err)[:120],
                }
                return degraded
            raise ConfigEntryAuthFailed(str(err)) from err
        except CarrierError as err:
            self._fail_streak = getattr(self, "_fail_streak", 0) + 1
            self._last_fail_at = _time.time()
            raise UpdateFailed(str(err)) from err
        except Exception as err:  # noqa: BLE001
            self._fail_streak = getattr(self, "_fail_streak", 0) + 1
            self._last_fail_at = _time.time()
            raise UpdateFailed(f"查询失败: {err}") from err
        self._fail_streak = 0
        # 会话滑动续期：把客户端刷新后的凭证（联通 token_online、移动 Cookie 等）
        # 写回配置条目；服务端会话顺延后重启 HA 不丢登录态
        try:
            self._async_persist_credentials()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("凭证回写失败（忽略）: %s", err)
        return data

    def _async_persist_credentials(self) -> None:
        exported = self.client.export_credentials() or {}
        updates = {}
        for key, value in exported.items():
            if not value:
                continue
            if self.entry.data.get(key) != value:
                updates[key] = value
        if not updates:
            return
        _LOGGER.debug("更新账号凭证（滑动续期）: %s", sorted(updates.keys()))
        new_data = {**self.entry.data, **updates}
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)


    # ------------------------------------------------------------------ SMS 自动重登

    SMS_AUTH_MODES = {"telecom": "sms", "unicom": "sms"}

    @property
    def supports_sms_relogin(self) -> bool:
        return (
            self.carrier in self.SMS_AUTH_MODES
            and self.entry.data.get("auth_mode") == self.SMS_AUTH_MODES[self.carrier]
        )

    async def _async_safe_send_relogin_sms(self) -> None:
        try:
            result = await self.async_send_relogin_sms()
            _LOGGER.info("%s 自动重登发码结果: %s", self.entry.title, result)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("%s 自动重登发码失败: %s", self.entry.title, err)

    async def async_send_relogin_sms(self) -> str:
        """token 失效后自动下发登录短信（限频 5 分钟）。"""
        import time as _time

        now = _time.time()
        if now - getattr(self, "_last_sms_sent_at", 0.0) < 300:
            return "cooldown"
        client = self._build_relogin_client()
        if client is None:
            return "unsupported"
        self._last_sms_sent_at = now
        if self.carrier == "telecom":
            ok = await client.async_login_sms_send()
        else:
            hint = await client.async_sms_trigger()
            ok = "已下发" in hint or "成功" in hint
        if ok:
            from homeassistant.components.persistent_notification import (
                async_create as pn_create,
            )

            pn_create(
                self.hass,
                title="中国运营商话费监控",
                message=(
                    f"{self.entry.title} 登录已过期，验证码短信已自动下发到手机。\n\n"
                    "转发器（smsForward / iOS 快捷指令）POST 到 "
                    "`/api/webhook/cn_carrier_monitor_sms`，"
                    'JSON: {"phone": "号码", "code": "验证码"} 即可自动完成重登；'
                    "也可在集成的重新认证里手动输入。"
                ),
                notification_id=f"{DOMAIN}_sms_{self.entry.entry_id}",
            )
        return "sent" if ok else "failed"

    def _build_relogin_client(self):
        from .api import TelecomClient, UnicomClient

        if self.carrier == "telecom":
            return TelecomClient(self.session, dict(self.entry.data))
        if self.carrier == "unicom":
            return UnicomClient(self.session, dict(self.entry.data))
        return None

    async def async_complete_sms_relogin(self, code: str) -> bool:
        """用新验证码完成重登并写回条目。"""
        client = self._build_relogin_client()
        if client is None:
            return False
        if self.carrier == "telecom":
            creds = await client.async_login_sms_submit(code)
            updates = {
                "token": creds["token"],
                "province_code": creds["province_code"],
                "city_code": creds["city_code"],
            }
        else:
            creds = await client.async_sms_login(code)
            updates = {"token": creds["token"]}
        new_data = {**self.entry.data, **updates}
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        # 重建 client 以载入新 token
        self.client = create_client(self.carrier, self.session, dict(new_data))
        await self.async_request_refresh()
        from homeassistant.components.persistent_notification import (
            async_dismiss as pn_dismiss,
        )

        pn_dismiss(
            self.hass, f"{DOMAIN}_sms_{self.entry.entry_id}"
        )
        return True

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self.session.close()
