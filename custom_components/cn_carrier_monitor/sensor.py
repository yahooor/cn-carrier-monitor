"""Sensor platform for 中国运营商话费监控.

每个账号一台设备，传感器集合（缺数据的运营商自动跳过）：
余额 / 本月消费 / 套餐名称 / 积分 / 通用·定向·总流量(剩余+已用) / 语音 / 短信
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTR_CARRIER, ATTR_PHONE, CARRIER_LABELS, DOMAIN
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
    carrier = entry.data["carrier"]
    entities: list[SensorEntity] = [
        CarrierBalanceSensor(coordinator, entry),
        CarrierMonthCostSensor(coordinator, entry),
        CarrierPlanNameSensor(coordinator, entry),
        CarrierUsageSensor(coordinator, entry, "flow_general_remain", "flow_general", "remain"),
        CarrierUsageSensor(coordinator, entry, "flow_general_used", "flow_general", "used"),
        CarrierUsageSensor(coordinator, entry, "flow_directed_remain", "flow_directed", "remain"),
        CarrierUsageSensor(coordinator, entry, "flow_total_remain", "flow_total", "remain"),
        CarrierUsageSensor(coordinator, entry, "voice_remain", "voice", "remain", unit_minutes=True),
        CarrierUsageSensor(coordinator, entry, "sms_remain", "sms", "remain", unit_sms=True),
        CarrierPointsSensor(coordinator, entry),
    ]
    async_add_entities(entities)


def _phone_masked(coordinator: CarrierDataUpdateCoordinator) -> str:
    phone = coordinator.entry.data.get("phone", "")
    return f"{phone[:3]}****{phone[7:]}" if len(phone) == 11 else phone


def _base_device_info(coordinator: CarrierDataUpdateCoordinator) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.entry.entry_id)},
        name=f"{CARRIER_NAME.get(coordinator.carrier, coordinator.carrier)} {_phone_masked(coordinator)}",
        manufacturer="China Carrier Monitor",
        model=f"{CARRIER_LABELS.get(coordinator.carrier, coordinator.carrier)}账号",
        entry_type=DeviceEntryType.SERVICE,
    )


class CarrierSensor(CoordinatorEntity[CarrierDataUpdateCoordinator], SensorEntity):
    """Base sensor bound to the account coordinator."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, coordinator: CarrierDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{self._sensor_key}"
        self._attr_device_info = _base_device_info(coordinator)

    @property
    def _data(self) -> dict[str, Any]:
        return self.coordinator.data or {}

    @property
    def available(self) -> bool:
        return super().available and bool(self._data)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            ATTR_PHONE: self.coordinator.entry.data.get("phone", ""),
            ATTR_CARRIER: self.coordinator.carrier,
            "last_update": self._data.get("updated_at"),
            "extra": self._data.get("extra") or {},
        }


class CarrierBalanceSensor(CarrierSensor):
    _sensor_key = "balance"
    _attr_translation_key = "balance"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "CNY"
    _attr_icon = "mdi:cash"

    @property
    def native_value(self):
        return self._data.get("balance")


class CarrierMonthCostSensor(CarrierSensor):
    _sensor_key = "month_cost"
    _attr_translation_key = "month_cost"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "CNY"
    _attr_icon = "mdi:receipt-text"

    @property
    def native_value(self):
        return self._data.get("month_cost")


class CarrierPlanNameSensor(CarrierSensor):
    _sensor_key = "plan_name"
    _attr_translation_key = "plan_name"
    _attr_icon = "mdi:sim"

    @property
    def native_value(self):
        return self._data.get("plan_name")

    @property
    def available(self) -> bool:
        return super().available and self._data.get("plan_name") is not None


class CarrierPointsSensor(CarrierSensor):
    _sensor_key = "points"
    _attr_translation_key = "points"
    _attr_icon = "mdi:star-circle-outline"
    _attr_state_class = SensorStateClass.TOTAL

    @property
    def native_value(self):
        return self._data.get("points")

    @property
    def available(self) -> bool:
        return super().available and self._data.get("points") is not None


class CarrierUsageSensor(CarrierSensor):
    """Generic usage sensor: reads data[key][field]."""

    def __init__(
        self,
        coordinator: CarrierDataUpdateCoordinator,
        entry: ConfigEntry,
        sensor_key: str,
        data_key: str,
        field: str,
        *,
        unit_minutes: bool = False,
        unit_sms: bool = False,
    ) -> None:
        self._sensor_key = sensor_key
        self._data_key = data_key
        self._field = field
        self._unit_minutes = unit_minutes
        self._unit_sms = unit_sms
        super().__init__(coordinator, entry)
        if unit_minutes:
            self._attr_native_unit_of_measurement = "min"
        elif unit_sms:
            self._attr_native_unit_of_measurement = "条"
        else:
            self._attr_native_unit_of_measurement = UnitOfInformation.GIGABYTES
            self._attr_device_class = SensorDeviceClass.DATA_SIZE
            self._attr_state_class = SensorStateClass.TOTAL

    @property
    def translation_key(self):
        return self._sensor_key

    @property
    def native_value(self):
        block = self._data.get(self._data_key) or {}
        value = block.get(self._field)
        return round(value, 2) if isinstance(value, (int, float)) else None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        block = self._data.get(self._data_key) or {}
        used, total, remain = block.get("used"), block.get("total"), block.get("remain")
        if used is not None or total is not None:
            attrs["用量明细"] = {
                "已用": used,
                "总量": total,
                "剩余": remain,
                "使用率": (
                    f"{round(used / total * 100, 1)}{PERCENTAGE}"
                    if used is not None and total
                    else None
                ),
            }
        return attrs
