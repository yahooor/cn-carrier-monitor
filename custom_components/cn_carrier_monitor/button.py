"""Button platform: per-account manual refresh."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import CarrierDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

CARRIER_NAME = {
    "telecom": "中国电信",
    "unicom": "中国联通",
    "mobile": "中国移动",
    "broadnet": "中国广电",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: CarrierDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([CarrierRefreshButton(coordinator, entry)])


class CarrierRefreshButton(CoordinatorEntity[CarrierDataUpdateCoordinator], ButtonEntity):
    """立即刷新该账号的话费/套餐数据。"""

    _attr_has_entity_name = True
    _attr_translation_key = "refresh_now"
    _attr_icon = "mdi:refresh"

    def __init__(
        self, coordinator: CarrierDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_refresh_now"
        phone = entry.data.get("phone", "")
        masked = f"{phone[:3]}****{phone[7:]}" if len(phone) == 11 else phone
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"{CARRIER_NAME.get(coordinator.carrier, coordinator.carrier)} {masked}",
            manufacturer="China Carrier Monitor",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_press(self) -> None:
        await self.coordinator.async_request_refresh()
