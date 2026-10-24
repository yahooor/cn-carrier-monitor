"""中国运营商话费监控 — 电信/联通/移动/广电统一集成.

支持多账号（每个配置条目一个号码）、短信/服务密码登录、会话过期自动发起重新认证。
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
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

    async def async_inject_token(call: ServiceCall) -> None:
        """调试：直接写入 token 并重载条目（发码在本机完成时使用）。"""
        phone = call.data["phone"]
        updates = {k: call.data[k] for k in ("token", "province_code", "city_code") if k in call.data}
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.data.get("phone") == phone:
                hass.config_entries.async_update_entry(entry, data={**entry.data, **updates})
                await hass.config_entries.async_reload(entry.entry_id)
                return
        raise HomeAssistantError(f"未找到号码 {phone} 的条目")

    hass.services.async_register(
        DOMAIN, "inject_token", async_inject_token,
        schema=vol.Schema({
            vol.Required("phone"): str,
            vol.Required("token"): str,
            vol.Optional("province_code"): str,
            vol.Optional("city_code"): str,
        }),
    )
    from . import webhook as _webhook

    _webhook.async_register(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one account from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    coordinator = CarrierDataUpdateCoordinator(hass, entry)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:  # noqa: BLE001
        # 首拉失败仍创建实体（unavailable）：电信短信 token 短效等场景
        # 靠后续轮询/重认证恢复，而不是整条卸载（Pocket-Carrier 同策略）
        _LOGGER.warning("%s 首次拉取失败，实体将以不可用状态创建: %s", entry.title, err)
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
