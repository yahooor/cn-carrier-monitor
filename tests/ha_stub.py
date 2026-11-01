"""最小 Home Assistant 桩：让 sensor / coordinator / __init__ 的逻辑能在没有
安装 Home Assistant 的环境里跑单测（CI 与本地测试解释器都没有 HA）。

只实现被测代码真正用到的 API，但关键语义对齐真实 HA 2024.6+：
- ``DataUpdateCoordinator.async_config_entry_first_refresh``：
  ``ConfigEntryAuthFailed`` 原样上抛，``UpdateFailed`` 转 ``ConfigEntryNotReady``；
- ``entity_registry.async_entries_for_config_entry`` 按 config_entry_id 过滤；
- ``persistent_notification.async_create/async_dismiss`` 按 notification_id 记录。

**仅供测试使用**，不要在产品代码里 import。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
from typing import Any

from conftest import CCM_ROOT

_STUB_FLAG = "_ccm_ha_stub"
DOMAIN = "cn_carrier_monitor"


# ------------------------------------------------------------------ 异常


class HomeAssistantError(Exception):
    """HA 基础异常桩。"""


class ConfigEntryAuthFailed(HomeAssistantError):
    """凭证失效。"""


class ConfigEntryNotReady(HomeAssistantError):
    """暂时不可用（可重试）。"""


class UpdateFailed(Exception):
    """协调器更新失败。"""


# ------------------------------------------------------------------ 实体注册表


class RegistryEntry:
    """EntityRegistryEntry 桩（只有被测代码读的字段）。"""

    def __init__(
        self,
        entity_id: str,
        unique_id: str,
        *,
        domain: str = "sensor",
        platform: str = DOMAIN,
        config_entry_id: str = "",
    ) -> None:
        self.entity_id = entity_id
        self.unique_id = unique_id
        self.domain = domain
        self.platform = platform
        self.config_entry_id = config_entry_id


class EntityRegistry:
    def __init__(self) -> None:
        self.entities: dict[str, RegistryEntry] = {}

    def add(
        self,
        entity_id: str,
        unique_id: str,
        *,
        domain: str = "sensor",
        platform: str = DOMAIN,
        config_entry_id: str = "",
    ) -> RegistryEntry:
        entry = RegistryEntry(
            entity_id, unique_id, domain=domain, platform=platform,
            config_entry_id=config_entry_id,
        )
        self.entities[entity_id] = entry
        return entry

    def async_get_entity_id(self, domain: str, platform: str, unique_id: str) -> str | None:
        for entry in self.entities.values():
            if entry.domain == domain and entry.unique_id == unique_id:
                return entry.entity_id
        return None


def async_get(hass: Any) -> EntityRegistry:
    return hass.entity_registry


def async_entries_for_config_entry(
    registry: EntityRegistry, config_entry_id: str
) -> list[RegistryEntry]:
    return [e for e in registry.entities.values() if e.config_entry_id == config_entry_id]


# ------------------------------------------------------------------ 通知


class NotificationStore:
    def __init__(self) -> None:
        self.created: dict[str, dict[str, Any]] = {}
        self.dismissed: list[str | None] = []
        self.create_calls = 0

    def dismiss_calls(self, notification_id: str | None) -> int:
        return sum(1 for item in self.dismissed if item == notification_id)


def _notifications(hass: Any) -> NotificationStore:
    store = getattr(hass, "notification_store", None)
    if store is None:
        store = NotificationStore()
        hass.notification_store = store
    return store


def pn_async_create(
    hass: Any,
    message: str,
    title: str | None = None,
    notification_id: str | None = None,
) -> None:
    store = _notifications(hass)
    store.create_calls += 1
    store.created[notification_id or f"auto_{store.create_calls}"] = {
        "title": title,
        "message": message,
    }


def pn_async_dismiss(hass: Any, notification_id: str | None = None) -> None:
    store = _notifications(hass)
    store.dismissed.append(notification_id)
    store.created.pop(notification_id, None)


# ------------------------------------------------------------------ 协调器 / 实体


class _GenericBase:
    """支持 ``Cls[Param]`` 泛型下标（HA 的 CoordinatorEntity 是泛型类）。"""

    def __class_getitem__(cls, item: Any) -> type:
        return cls


class DataUpdateCoordinator(_GenericBase):
    """DataUpdateCoordinator 桩：只实现被测代码调用到的方法。"""

    def __init__(
        self,
        hass: Any,
        logger: Any,
        *,
        name: str | None = None,
        update_interval: Any = None,
        **kwargs: Any,
    ) -> None:
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.data: Any = None
        self.last_update_success = True
        self.shutdown_called = False
        self.refresh_count = 0
        self._listeners: list[Any] = []

    def async_add_listener(self, update_callback: Any, context: Any = None) -> Any:
        self._listeners.append(update_callback)

        def _remove() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return _remove

    def async_update_listeners(self) -> None:
        for listener in list(self._listeners):
            listener()

    async def _async_refresh(self) -> Any:
        try:
            self.data = await self._async_update_data()
        except ConfigEntryAuthFailed:
            self.last_update_success = False
            raise
        except UpdateFailed as err:
            self.last_update_success = False
            raise ConfigEntryNotReady(str(err)) from err
        self.last_update_success = True
        self.refresh_count += 1
        self.async_update_listeners()
        return self.data

    async def async_config_entry_first_refresh(self) -> Any:
        return await self._async_refresh()

    async def async_request_refresh(self) -> Any:
        return await self._async_refresh()

    async def async_shutdown(self) -> None:
        self.shutdown_called = True


class CoordinatorEntity(_GenericBase):
    def __init__(self, coordinator: Any) -> None:
        self.coordinator = coordinator


class SensorEntity:
    """SensorEntity 桩：属性名与 HA 保持一致（被 _attr_* 覆盖）。"""

    _attr_has_entity_name = False
    _attr_should_poll = True
    _attr_unique_id: str | None = None
    _attr_translation_key: str | None = None
    _attr_icon: str | None = None
    _attr_device_class: Any = None
    _attr_state_class: Any = None
    _attr_native_unit_of_measurement: str | None = None
    _attr_device_info: Any = None

    @property
    def unique_id(self) -> str | None:
        return self._attr_unique_id

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> Any:
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {}

    @property
    def device_info(self) -> Any:
        return self._attr_device_info


class SensorDeviceClass:
    MONETARY = "monetary"
    DATA_SIZE = "data_size"
    TIMESTAMP = "timestamp"


class SensorStateClass:
    TOTAL = "total"
    TOTAL_INCREASING = "total_increasing"
    MEASUREMENT = "measurement"


class DeviceEntryType:
    SERVICE = "service"


class UnitOfInformation:
    GIGABYTES = "GB"


def DeviceInfo(**kwargs: Any) -> dict[str, Any]:  # noqa: N802 - 对齐 HA 的构造式 API
    return kwargs


def callback(func: Any) -> Any:
    """@callback 装饰器桩：直接返回原函数。"""
    return func


def split_entity_id(entity_id: str) -> tuple[str, str]:
    """``sensor.foo`` → ``("sensor", "foo")``；非法 ID 抛 ValueError（对齐 HA）。"""
    domain, _, object_id = (entity_id or "").partition(".")
    if not domain or not object_id:
        raise ValueError(f"Invalid entity ID: {entity_id}")
    return domain, object_id


# ------------------------------------------------------------------ hass


class FakeConfigFlowManager:
    def __init__(self) -> None:
        self.init_calls: list[dict[str, Any]] = []

    async def async_init(self, domain: str, **kwargs: Any) -> dict[str, Any]:
        self.init_calls.append({"domain": domain, **kwargs})
        return {"type": "form"}


class FakeConfigEntries:
    def __init__(self, hass: FakeHass) -> None:
        self.hass = hass
        self.flow = FakeConfigFlowManager()
        self.forwarded: list[tuple[str, list[str]]] = []
        self.unloaded: list[tuple[str, list[str]]] = []
        self.updated: list[dict[str, Any]] = []
        self.reloaded: list[str] = []
        self._entries: list[Any] = []

    async def async_forward_entry_setups(self, entry: Any, platforms: Any) -> None:
        self.forwarded.append((entry.entry_id, list(platforms)))

    async def async_unload_platforms(self, entry: Any, platforms: Any) -> bool:
        self.unloaded.append((entry.entry_id, list(platforms)))
        return True

    def async_update_entry(self, entry: Any, data: Any = None, **kwargs: Any) -> None:
        if data is not None:
            entry.data = dict(data)
        self.updated.append({"entry_id": entry.entry_id, "data": dict(data or {})})

    async def async_reload(self, entry_id: str) -> None:
        self.reloaded.append(entry_id)

    def async_entries(self, domain: str | None = None) -> list[Any]:
        return [e for e in self._entries if domain is None or e.domain == domain]


class FakeConfig:
    def __init__(self) -> None:
        self._dir = tempfile.mkdtemp(prefix="ccm-ha-stub-")

    def path(self, *parts: str) -> str:
        return os.path.join(self._dir, *parts)


class FakeHass:
    """HomeAssistant 桩：记录副作用，便于断言。"""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.config = FakeConfig()
        self.entity_registry = EntityRegistry()
        self.config_entries = FakeConfigEntries(self)
        self.notification_store = NotificationStore()
        self.tasks: list[str] = []
        self.task_qualnames: list[str] = []
        self.executor_jobs: list[str] = []
        self.states: dict[str, Any] = {}

    def async_create_task(self, target: Any, name: str | None = None, **kwargs: Any) -> Any:
        """记录被调度的协程，但不执行它（部分是真实网络操作，如发码）。

        协程体不会跑，所以用 ``cr_code.co_name/co_qualname`` 记录"调度了什么"，
        并立即 close 掉，避免 "coroutine was never awaited" 警告。
        """
        code = getattr(target, "cr_code", None)
        label = getattr(code, "co_name", type(target).__name__)
        qualname = getattr(code, "co_qualname", label)
        self.tasks.append(label)
        self.task_qualnames.append(qualname)
        close = getattr(target, "close", None)
        if callable(close):
            close()
        return target

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        self.executor_jobs.append(getattr(func, "__name__", str(func)))
        return func(*args)


class ConfigEntry:
    """ConfigEntry 桩。"""

    def __init__(
        self,
        entry_id: str = "entry1",
        data: dict[str, Any] | None = None,
        *,
        title: str = "中国联通 138****0000",
        options: dict[str, Any] | None = None,
        domain: str = DOMAIN,
    ) -> None:
        self.entry_id = entry_id
        self.data = dict(data or {})
        self.options = dict(options or {})
        self.title = title
        self.domain = domain
        self.state = "loaded"
        self.reauth_started = 0
        self.update_listeners: list[Any] = []
        self._unload_callbacks: list[Any] = []

    def async_on_unload(self, func: Any) -> None:
        self._unload_callbacks.append(func)

    def add_update_listener(self, listener: Any) -> Any:
        self.update_listeners.append(listener)
        return lambda: None

    def async_start_reauth(self, hass: Any, context: Any = None) -> None:
        self.reauth_started += 1


# ------------------------------------------------------------------ 装配


def _module(name: str, **attrs: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    parent_name, _, child = name.rpartition(".")
    if parent_name:
        parent = sys.modules.get(parent_name)
        if parent is not None:
            setattr(parent, child, module)
    return module


def _install_voluptuous() -> None:
    """voluptuous 桩：集成 __init__ 只用 Schema/Optional/Required 声明服务 schema。

    真实 voluptuous 已安装时不动 sys.modules（只在缺依赖的测试环境里兜底）。
    """
    try:
        import voluptuous  # noqa: F401
    except ImportError:
        pass
    else:
        return

    vol = _module("voluptuous")

    class _Key:
        def __init__(self, schema: Any = None, default: Any = None, **kwargs: Any) -> None:
            self.schema = schema
            self.default = default
            self.kwargs = kwargs

        def __hash__(self) -> int:
            return hash(self.schema)

    class Schema:
        def __init__(self, schema: Any = None, **kwargs: Any) -> None:
            self.schema = schema
            self.kwargs = kwargs

        def __call__(self, data: Any) -> Any:
            return data

    vol.Schema = Schema
    vol.Optional = _Key
    vol.Required = _Key
    vol.In = lambda *args, **kwargs: None
    vol.All = lambda *args, **kwargs: None
    vol.Any = lambda *args, **kwargs: None
    vol.Coerce = lambda t, msg=None: t
    vol.Range = lambda min=None, max=None, msg=None: None
    # webhook/config_flow 的 except vol.Invalid 分支需要该名字存在
    class Invalid(ValueError):
        """Stub for voluptuous.Invalid (schema validation failure)."""

    vol.Invalid = Invalid


def install() -> None:
    """把 homeassistant 桩装进 sys.modules（幂等）。"""
    if getattr(sys.modules.get("homeassistant"), _STUB_FLAG, False):
        return

    _install_voluptuous()

    ha = _module("homeassistant")
    setattr(ha, _STUB_FLAG, True)
    ha.__path__ = []  # 标记为包，避免被当作命名空间包误加载真实模块

    _module("homeassistant.exceptions",
            HomeAssistantError=HomeAssistantError,
            ConfigEntryAuthFailed=ConfigEntryAuthFailed,
            ConfigEntryNotReady=ConfigEntryNotReady)
    _module("homeassistant.core",
            HomeAssistant=FakeHass, callback=callback, ServiceCall=object)
    _module("homeassistant.const",
            PERCENTAGE="%", UnitOfInformation=UnitOfInformation, __version__="stub")
    class _StubConfigFlow:
        """config_entries.ConfigFlow 桩：支持 domain 关键字与 async_show_form。"""

        # 真实 ConfigFlow 用 __init_subclass__ 接收 domain=...；桩里存起来即可
        def __init_subclass__(cls, **kwargs: Any) -> None:
            super().__init_subclass__()
            cls._flow_domain = kwargs.get("domain")

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.hass = None

        def async_show_form(self, *, step_id, data_schema=None, errors=None,
                            description_placeholders=None):
            return {"type": "form", "step_id": step_id, "errors": errors or {}}

        def async_create_entry(self, *, title=None, data=None):
            return {"type": "create_entry", "title": title, "data": data}

        def async_abort(self, *, reason=None):
            return {"type": "abort", "reason": reason}

        async def async_set_unique_id(self, uid):
            self._unique_id = uid

    _module("homeassistant.config_entries", ConfigEntry=ConfigEntry,
            ConfigFlow=_StubConfigFlow,
            OptionsFlow=type("OptionsFlow", (), {}),
            OptionsFlowWithConfigEntry=type("OptionsFlowWithConfigEntry", (), {}))
    _module("homeassistant.util")
    _module("homeassistant.util.dt", get_default_time_zone=lambda: None)

    _module("homeassistant.components")
    _module("homeassistant.components.sensor",
            SensorEntity=SensorEntity,
            SensorDeviceClass=SensorDeviceClass,
            SensorStateClass=SensorStateClass)
    _module("homeassistant.components.button", ButtonEntity=SensorEntity)
    _module("homeassistant.components.persistent_notification",
            async_create=pn_async_create, async_dismiss=pn_async_dismiss)

    def _webhook_register(hass, domain, name, webhook_id, handler, **kwargs):
        # 记录 handler 供 webhook 全链路测试直接调用
        sys.modules.setdefault(
            "__ha_stub_state", types.SimpleNamespace(webhook_handlers={})
        ).webhook_handlers[webhook_id] = handler

    _module("homeassistant.components.webhook", async_register=_webhook_register)

    _module("homeassistant.helpers")
    _module("homeassistant.helpers.aiohttp_client",
            async_get_clientsession=lambda hass: None)
    _module("homeassistant.helpers.config_validation",
            config_entry_only_config_schema=lambda domain: {},
            string=str)
    _module("homeassistant.helpers.device_registry",
            DeviceEntryType=DeviceEntryType, DeviceInfo=DeviceInfo)
    _module("homeassistant.helpers.entity", split_entity_id=split_entity_id)
    _module("homeassistant.helpers.entity_platform",
            AddConfigEntryEntitiesCallback=object)
    _module("homeassistant.helpers.entity_registry",
            async_get=async_get,
            async_entries_for_config_entry=async_entries_for_config_entry)
    _module("homeassistant.helpers.update_coordinator",
            DataUpdateCoordinator=DataUpdateCoordinator,
            CoordinatorEntity=CoordinatorEntity,
            UpdateFailed=UpdateFailed)


def load_integration(pkg_name: str = "ccm_ha") -> types.ModuleType:
    """装好 HA 桩后加载集成包（含 sensor / coordinator 子模块）。"""
    install()
    module = sys.modules.get(pkg_name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(
        pkg_name, CCM_ROOT / "__init__.py",
        submodule_search_locations=[str(CCM_ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = module
    spec.loader.exec_module(module)
    importlib.import_module(f"{pkg_name}.sensor")
    importlib.import_module(f"{pkg_name}.coordinator")
    # 暴露通知模块句柄：便于测试 monkeypatch async_create 验证"通知失败不影响更新"
    module.notification_module = sys.modules[
        "homeassistant.components.persistent_notification"
    ]
    return module
