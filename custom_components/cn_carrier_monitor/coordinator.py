"""DataUpdateCoordinator for China Carrier Monitor."""

from __future__ import annotations

import logging

import aiohttp
from datetime import timedelta
from typing import Any

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
        # 每个账号独立会话，避免 Cookie 串号；强制 IPv4（运营商域名常返回
        # IPv6 优先且内网无 v6 出口，DNS 也会偶发超时）
        import socket as _socket

        connector = aiohttp.TCPConnector(family=_socket.AF_INET, ssl=True)
        self.session = aiohttp.ClientSession(connector=connector)
        self.client = create_client(self.carrier, self.session, dict(entry.data))
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{self.carrier}_{entry.data.get('phone', '')}",
            update_interval=timedelta(minutes=minutes),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await self.client.async_fetch()
        except CarrierAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except CarrierError as err:
            raise UpdateFailed(str(err)) from err
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"查询失败: {err}") from err
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

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self.session.close()
