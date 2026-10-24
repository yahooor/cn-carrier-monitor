"""中国运营商话费监控 — 电信/联通/移动/广电统一集成.

支持多账号（每个配置条目一个号码）、短信/服务密码登录、会话过期自动发起重新认证。
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .const import DOMAIN, PLATFORMS, SERVICE_REFRESH
from .coordinator import CarrierDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

REFRESH_SCHEMA = vol.Schema({vol.Optional("entry_id"): str})


async def async_setup(hass: HomeAssistant, config) -> bool:
    """Register integration-level services."""

    async def async_refresh(call: ServiceCall) -> None:
        entry_id = call.data.get("entry_id")
        coordinators = hass.data.get(DOMAIN, {})
        for unique_entry_id, coordinator in coordinators.items():
            if entry_id and unique_entry_id != entry_id:
                continue
            hass.async_create_task(coordinator.async_request_refresh())

    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH, async_refresh, schema=REFRESH_SCHEMA
    )
    from . import webhook as _webhook

    _webhook.async_register(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one account from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    coordinator = CarrierDataUpdateCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data[DOMAIN][entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    coordinator = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if coordinator:
        await coordinator.async_shutdown()
    return unload_ok
