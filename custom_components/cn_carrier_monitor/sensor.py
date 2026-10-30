"""Sensor platform for 中国运营商话费监控.

实体按账号实际返回的数据**动态创建**：运营商不提供的字段（如 8 元保号卡没有
流量、联通网页接口不含短信）不再以"不可用"状态出现。协调器后续刷新带来新
字段时，通过 listener 增补实体。
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
from homeassistant.core import HomeAssistant, callback
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
    created: set[str] = set()

    @callback
    def _sync() -> None:
        desired = _desired_keys(coordinator.data or {})
        new = [k for k in desired if k not in created]
        if not new:
            return
        created.update(new)
        async_add_entities([ENTITY_FACTORY[k](coordinator, entry) for k in new])

    _sync()
    coordinator.async_add_listener(_sync)


def _desired_keys(data: dict[str, Any]) -> list[str]:
    """根据最新数据决定应有实体（顺序即实体排序）。"""
    keys: list[str] = []
    if data.get("balance") is not None:
        keys.append("balance")
    if data.get("month_cost") is not None:
        keys.append("month_cost")
    if data.get("plan_name"):
        keys.append("plan_name")
    if data.get("plan_price"):
        keys.append("plan_price")
    if data.get("location"):
        keys.append("location")
    for block_key in ("flow_general", "flow_directed", "flow_total"):
        block = data.get(block_key) or {}
        if block.get("used") is not None:
            keys.append(f"{block_key}_used")
        if block.get("remain") is not None:
            keys.append(f"{block_key}_remain")
    voice = data.get("voice") or {}
    if voice.get("remain") is not None:
        keys.append("voice_remain")
    if voice.get("used") is not None:
        keys.append("voice_used")
    sms = data.get("sms") or {}
    if sms.get("remain") is not None:
        keys.append("sms_remain")
    if data.get("points") is not None:
        keys.append("points")
    return keys


ENTITY_FACTORY: dict[str, Any] = {}


def _register(key: str):
    def deco(cls):
        ENTITY_FACTORY[key] = cls
        return cls
    return deco


def _device_info(coordinator: CarrierDataUpdateCoordinator) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.entry.entry_id)},
        name=f"{CARRIER_NAME.get(coordinator.carrier, coordinator.carrier)} "
             f"{_phone_masked(coordinator)}",
        manufacturer="China Carrier Monitor",
        model=f"{CARRIER_LABELS.get(coordinator.carrier, coordinator.carrier)}账号",
        entry_type=DeviceEntryType.SERVICE,
    )


def _phone_masked(coordinator: CarrierDataUpdateCoordinator) -> str:
    phone = coordinator.entry.data.get("phone", "")
    return f"{phone[:3]}****{phone[7:]}" if len(phone) == 11 else phone


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
        self._attr_device_info = _device_info(coordinator)

    @property
    def _data(self) -> dict[str, Any]:
        return self.coordinator.data or {}

    @property
    def available(self) -> bool:
        # 实体在数据出现后才创建；登录过期降级时保持显示上次值
        if not self.coordinator.last_update_success and not self._data:
            return False
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            ATTR_PHONE: self.coordinator.entry.data.get("phone", ""),
            ATTR_CARRIER: self.coordinator.carrier,
            "last_update": self._data.get("updated_at"),
            "extra": self._data.get("extra") or {},
        }


@_register("balance")
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


@_register("month_cost")
class CarrierMonthCostSensor(CarrierSensor):
    _sensor_key = "month_cost"
    _attr_translation_key = "month_cost"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "CNY"
    _attr_icon = "mdi:receipt-text"

    @property
    def native_value(self):
        return self._data.get("month_cost")


@_register("plan_name")
class CarrierPlanNameSensor(CarrierSensor):
    _sensor_key = "plan_name"
    _attr_translation_key = "plan_name"
    _attr_icon = "mdi:sim"

    @property
    def native_value(self):
        return self._data.get("plan_name")


@_register("points")
class CarrierPointsSensor(CarrierSensor):
    _sensor_key = "points"
    _attr_translation_key = "points"
    _attr_icon = "mdi:star-circle-outline"
    _attr_state_class = SensorStateClass.TOTAL

    @property
    def native_value(self):
        return self._data.get("points")


class CarrierUsageSensor(CarrierSensor):
    """Generic usage sensor: reads data[data_key][field]."""

    def __init__(
        self,
        coordinator: CarrierDataUpdateCoordinator,
        entry: ConfigEntry,
        sensor_key: str,
        data_key: str,
        field: str,
        *,
        unit: str | None = None,
        device_class: SensorDeviceClass | None = None,
        state_class: SensorStateClass | None = SensorStateClass.TOTAL,
    ) -> None:
        self._data_key = data_key
        self._field = field
        self._sensor_key = sensor_key
        self._attr_unique_id = f"{entry.entry_id}_{sensor_key}"
        self._attr_translation_key = sensor_key
        if unit:
            self._attr_native_unit_of_measurement = unit
        if device_class:
            self._attr_device_class = device_class
        if state_class:
            self._attr_state_class = state_class
        super().__init__(coordinator, entry)

    @property
    def native_value(self):
        block = self._data.get(self._data_key) or {}
        value = block.get(self._field)
        return round(value, 2) if isinstance(value, (int, float)) else None

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


def _usage_factory(sensor_key: str, data_key: str, field: str, **kw):
    def make(coordinator, entry):
        return CarrierUsageSensor(coordinator, entry, sensor_key, data_key, field, **kw)
    return make


ENTITY_FACTORY["flow_general_used"] = _usage_factory(
    "flow_general_used", "flow_general", "used",
    unit=UnitOfInformation.GIGABYTES, device_class=SensorDeviceClass.DATA_SIZE)
ENTITY_FACTORY["flow_general_remain"] = _usage_factory(
    "flow_general_remain", "flow_general", "remain",
    unit=UnitOfInformation.GIGABYTES, device_class=SensorDeviceClass.DATA_SIZE)
ENTITY_FACTORY["flow_directed_used"] = _usage_factory(
    "flow_directed_used", "flow_directed", "used",
    unit=UnitOfInformation.GIGABYTES, device_class=SensorDeviceClass.DATA_SIZE)
ENTITY_FACTORY["flow_directed_remain"] = _usage_factory(
    "flow_directed_remain", "flow_directed", "remain",
    unit=UnitOfInformation.GIGABYTES, device_class=SensorDeviceClass.DATA_SIZE)
ENTITY_FACTORY["flow_total_used"] = _usage_factory(
    "flow_total_used", "flow_total", "used",
    unit=UnitOfInformation.GIGABYTES, device_class=SensorDeviceClass.DATA_SIZE)
ENTITY_FACTORY["flow_total_remain"] = _usage_factory(
    "flow_total_remain", "flow_total", "remain",
    unit=UnitOfInformation.GIGABYTES, device_class=SensorDeviceClass.DATA_SIZE)
ENTITY_FACTORY["voice_used"] = _usage_factory("voice_used", "voice", "used", unit="min")
ENTITY_FACTORY["voice_remain"] = _usage_factory("voice_remain", "voice", "remain", unit="min")
ENTITY_FACTORY["sms_used"] = _usage_factory("sms_used", "sms", "used", unit="条")
ENTITY_FACTORY["sms_remain"] = _usage_factory("sms_remain", "sms", "remain", unit="条")


@_register("plan_price")
class CarrierPlanPriceSensor(CarrierSensor):
    _sensor_key = "plan_price"
    _attr_translation_key = "plan_price"
    _attr_icon = "mdi:tag"

    @property
    def native_value(self):
        return self._data.get("plan_price")


@_register("location")
class CarrierLocationSensor(CarrierSensor):
    _sensor_key = "location"
    _attr_translation_key = "location"
    _attr_icon = "mdi:map-marker"

    @property
    def native_value(self):
        return self._data.get("location")
