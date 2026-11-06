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
from homeassistant.helpers import entity_registry as er
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
        # 数据驱动部分：运营商这次真正返回了哪些字段（决定首次安装建哪些实体）
        desired = _desired_keys(coordinator.data or {})
        # 实体集合一旦建立就必须稳定：数据缺失（首次刷新失败 / 单端点漂移 /
        # 降级为空）时若让集合跟着塌缩，实体就会从状态机消失（注册表还在，
        # 用户视角是"17 个实体突然没了"）。因此始终并入注册表里已存在的 key，
        # 由实体自身显示 unavailable。首次安装时注册表为空 → 仍是纯数据驱动，
        # 不会凭空造出运营商不提供的字段。
        desired = list(dict.fromkeys([*desired, *_registry_keys(hass, entry)]))
        new = [k for k in desired if k not in created]
        if not new:
            return
        created.update(new)
        async_add_entities([ENTITY_FACTORY[k](coordinator, entry) for k in new])

    _sync()
    coordinator.async_add_listener(_sync)


def _registry_keys(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    """从实体注册表反推该条目已存在的 sensor key（实体集合稳定化）。

    只认 ``unique_id`` 形如 ``{entry_id}_{sensor_key}`` 且后缀在 ENTITY_FACTORY
    里的条目：历史脏数据（早期版本换过 unique_id 格式）不能让 setup 抛 KeyError。
    注册表不可用时返回空列表，退回纯数据驱动（首次安装的原有行为）。
    """
    try:
        registry = er.async_get(hass)
        reg_entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    except Exception as err:  # noqa: BLE001 - 注册表读取失败不该阻断平台加载
        _LOGGER.debug("读取实体注册表失败，退回数据驱动建实体: %s", err)
        return []

    prefix = f"{entry.entry_id}_"
    keys: list[str] = []
    for reg_entry in reg_entries:
        # 只处理本平台的 sensor 实体（button 等其它平台不在这里重建）
        entity_domain = getattr(reg_entry, "domain", None)
        if entity_domain is None:
            # 极老版本无 domain 字段：从 entity_id 推导。不能用
            # RegistryEntry.platform —— 它是**集成域名**（cn_carrier_monitor），
            # 拿它比 "sensor" 会把所有条目 skip 掉，E1 保护静默失效。
            # 也不用 import split_entity_id：它在不同 HA 版本里分别位于
            # helpers.entity / core，防御性分支不该再引入可能 ImportError 的依赖。
            entity_id = getattr(reg_entry, "entity_id", "") or ""
            entity_domain = entity_id.split(".", 1)[0]
        if entity_domain != "sensor":
            continue
        unique_id = getattr(reg_entry, "unique_id", "") or ""
        if not unique_id.startswith(prefix):
            continue
        key = unique_id[len(prefix):]
        if key not in ENTITY_FACTORY:
            _LOGGER.debug("跳过未知实体键（注册表脏数据）: %s", unique_id)
            continue
        keys.append(key)
    # 去重 + 排序：注册表顺序不保证稳定，排序后实体创建顺序可复现
    return sorted(dict.fromkeys(keys))


def _desired_keys(data: dict[str, Any]) -> list[str]:
    """根据最新数据决定应有实体（顺序即实体排序，键必须唯一）。"""
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
    if sms.get("used") is not None:
        keys.append("sms_used")
    if data.get("points") is not None:
        keys.append("points")
    # Round-3 新增
    if data.get("updated_at"):
        keys.append("last_update")
    if data.get("star_level"):
        keys.append("star_level")
    if data.get("real_name"):
        keys.append("real_name")
    if data.get("owe_fee") is not None:
        keys.append("owe_fee")
    if data.get("credit_limit") is not None:
        keys.append("credit_limit")
    if data.get("pay_type"):
        keys.append("pay_type")
    if data.get("special_balance") is not None:
        keys.append("special_balance")
    if data.get("phone_region"):
        keys.append("phone_region")
    if data.get("account_status"):
        keys.append("account_status")
    # 会话健康度与运营商返回什么字段无关：始终存在，便于自动化里做掉线告警
    keys.append("session_state")
    # 同一键只出现一次：重复项会生成两个同 unique_id 的实体
    return list(dict.fromkeys(keys))


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

# ------------------------------------------------------------------ Round-3 新增

@_register("last_update")
class CarrierLastUpdateSensor(CarrierSensor):
    """数据更新时间（用户可直接看到数据新鲜度）。"""
    _sensor_key = "last_update"
    _attr_translation_key = "last_update"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-check"

    @property
    def native_value(self):
        from datetime import datetime

        from homeassistant.util import dt as dt_util

        raw = self._data.get("updated_at")
        if not raw:
            return None
        try:
            dt = datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return None
        # 采集端存的是 HA 主机本地时间，按 HA 配置时区解释（原先硬编码 +8，
        # 非东八区部署会整体偏移 8 小时）
        return dt.replace(tzinfo=dt_util.get_default_time_zone())


@_register("star_level")
class CarrierStarLevelSensor(CarrierSensor):
    _sensor_key = "star_level"
    _attr_translation_key = "star_level"
    _attr_icon = "mdi:star-four-points"

    @property
    def native_value(self):
        return self._data.get("star_level")


@_register("real_name")
class CarrierRealNameSensor(CarrierSensor):
    _sensor_key = "real_name"
    _attr_translation_key = "real_name"
    _attr_icon = "mdi:account-check"

    @property
    def native_value(self):
        return self._data.get("real_name")


@_register("owe_fee")
class CarrierOweFeeSensor(CarrierSensor):
    _sensor_key = "owe_fee"
    _attr_translation_key = "owe_fee"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "CNY"
    _attr_icon = "mdi:cash-minus"

    @property
    def native_value(self):
        v = self._data.get("owe_fee")
        return round(v, 2) if isinstance(v, (int, float)) else None


@_register("credit_limit")
class CarrierCreditLimitSensor(CarrierSensor):
    _sensor_key = "credit_limit"
    _attr_translation_key = "credit_limit"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "CNY"
    _attr_icon = "mdi:credit-card-outline"

    @property
    def native_value(self):
        v = self._data.get("credit_limit")
        return round(v, 2) if isinstance(v, (int, float)) else None


@_register("pay_type")
class CarrierPayTypeSensor(CarrierSensor):
    _sensor_key = "pay_type"
    _attr_translation_key = "pay_type"
    _attr_icon = "mdi:swap-horizontal"

    @property
    def native_value(self):
        return self._data.get("pay_type")


@_register("special_balance")
class CarrierSpecialBalanceSensor(CarrierSensor):
    _sensor_key = "special_balance"
    _attr_translation_key = "special_balance"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "CNY"
    _attr_icon = "mdi:cash-lock"

    @property
    def native_value(self):
        v = self._data.get("special_balance")
        return round(v, 2) if isinstance(v, (int, float)) else None


@_register("phone_region")
class CarrierPhoneRegionSensor(CarrierSensor):
    _sensor_key = "phone_region"
    _attr_translation_key = "phone_region"
    _attr_icon = "mdi:map-marker-radius"

    @property
    def native_value(self):
        return self._data.get("phone_region")

@_register("account_status")
class CarrierAccountStatusSensor(CarrierSensor):
    _sensor_key = "account_status"
    _attr_translation_key = "account_status"
    _attr_icon = "mdi:shield-check"

    @property
    def native_value(self):
        return self._data.get("account_status")


@_register("session_state")
class CarrierSessionStateSensor(CarrierSensor):
    """会话健康度：healthy / degraded / needs_human / circuit_open / maintenance。

    与其它实体不同，它**始终存在**（不受运营商返回字段影响），因此掉线时
    自动化可以直接用它告警，而不用去猜哪个数值实体变成了 unavailable。

    `maintenance`（2026-11-07 新增）：运营商整站系统升级维护。此时凭据有效、
    **不需要人处理**——自动化据此可以既不弹"重新登录"告警，也不把账号标记
    为异常，只是暂时没有新数据。
    """

    _sensor_key = "session_state"
    _attr_translation_key = "session_state"
    _attr_icon = "mdi:shield-account"

    @property
    def native_value(self):
        return getattr(self.coordinator, "session_state", None)

    @property
    def available(self) -> bool:
        # 会话状态本身永远可读（这正是掉线时最需要的信息）
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        coordinator = self.coordinator
        attrs["fail_streak"] = getattr(coordinator, "fail_streak", None)
        attrs["auth_fail_streak"] = getattr(coordinator, "auth_fail_streak", None)
        attrs["last_error"] = getattr(coordinator, "last_error", None)
        anomalies = (self._data.get("extra") or {}).get("endpoint_anomalies")
        if anomalies:
            attrs["endpoint_anomalies"] = anomalies
        return attrs
