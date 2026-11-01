"""中国运营商话费监控 — 电信/联通/移动/广电统一集成.

支持多账号（每个配置条目一个号码）、短信/服务密码登录、会话过期自动发起重新认证。
"""

from __future__ import annotations

import logging
import os

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, PLATFORMS, SERVICE_REFRESH
from .coordinator import CarrierDataUpdateCoordinator
from .net import warm_ssl_context

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

REFRESH_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
        vol.Optional("phone"): cv.string,
    }
)

INJECT_SCHEMA = vol.Schema(
    {
        vol.Required("phone"): cv.string,
        vol.Optional("token"): cv.string,
        vol.Optional("cookies"): cv.string,
        vol.Optional("province_code"): cv.string,
        vol.Optional("city_code"): cv.string,
    }
)


async def async_setup(hass: HomeAssistant, config) -> bool:
    """Register integration-level services."""

    # SSL 上下文预热：load_default_certs() 是阻塞调用，必须放到 executor
    await hass.async_add_executor_job(warm_ssl_context)

    async def async_refresh(call: ServiceCall) -> None:
        entry_id = call.data.get("entry_id")
        phone = call.data.get("phone")
        coordinators = _coordinators(hass)
        if entry_id:
            coordinator = coordinators.get(entry_id)
            if coordinator is None:
                raise HomeAssistantError(f"未找到 entry_id={entry_id} 的账号")
            await coordinator.async_request_refresh()
            return
        if phone:
            matched = [
                c for c in coordinators.values()
                if c.entry.data.get("phone") == phone
            ]
            if not matched:
                raise HomeAssistantError(f"未找到号码 {phone} 的账号")
            for coordinator in matched:
                await coordinator.async_request_refresh()
            return
        for coordinator in coordinators.values():
            await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH, async_refresh, schema=REFRESH_SCHEMA
    )

    async def async_inject_token(call: ServiceCall) -> None:
        """调试/运维：直接写入 token 或 cookies 并重载条目。

        用途：浏览器收割的会话直接灌入，免去删除重建条目。
        """
        phone = call.data["phone"]
        updates = {
            k: call.data[k]
            for k in ("token", "cookies", "province_code", "city_code")
            if call.data.get(k)
        }
        if not updates:
            raise HomeAssistantError("至少提供 token 或 cookies 之一")
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.data.get("phone") == phone:
                hass.config_entries.async_update_entry(entry, data={**entry.data, **updates})
                _LOGGER.info("已注入会话凭证: %s（字段 %s）", entry.title, sorted(updates))
                await hass.config_entries.async_reload(entry.entry_id)
                return
        raise HomeAssistantError(f"未找到号码 {phone} 的条目")

    hass.services.async_register(
        DOMAIN, "inject_token", async_inject_token, schema=INJECT_SCHEMA
    )

    from . import webhook as _webhook

    _webhook.async_register(hass)

    # 集成图标（brands PR 合并前的前端覆盖方案）
    # 用 try/except 保护——图标注册失败不应阻止集成加载
    try:
        await _async_register_frontend_assets(hass)
    except Exception as err:  # noqa: BLE001 - 纯展示层，失败不影响功能
        _LOGGER.warning("前端图标资源注册失败（不影响功能）: %s", err)

    return True


def _coordinators(hass: HomeAssistant) -> dict[str, CarrierDataUpdateCoordinator]:
    """只取真正的 coordinator，避免历史遗留的杂项键混进遍历。"""
    raw = hass.data.get(DOMAIN, {})
    return {
        key: value
        for key, value in raw.items()
        if isinstance(value, CarrierDataUpdateCoordinator)
    }


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one account from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    coordinator = CarrierDataUpdateCoordinator(hass, entry)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        # 首次拉取失败。分两种情形处理：
        # 1) 全新条目（注册表里没有任何实体）→ 交给 HA 标准机制
        #    （AuthFailed→reauth / NotReady→退避重试），但必须先把会话关掉，
        #    否则每次重试都会漏一个 ClientSession。
        # 2) 已有实体的条目 → 绝不能整条卸载：setup_error 会让实体从状态机
        #    消失（注册表还在），用户看到的是"实体突然没了"。改为降级加载
        #    （实体保留、显示 unavailable）；仅当失败是"凭据被拒"时才发起
        #    重新认证，网络类暂时故障等下一轮轮询自愈。
        if not _entry_has_entities(hass, entry):
            await coordinator.async_shutdown()
            raise
        _log_degraded_setup(entry, err)
        hass.data[DOMAIN][entry.entry_id] = coordinator
        entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
        try:
            await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        except Exception:
            # 平台加载失败必须回收会话，否则每次 setup 重试漏一个连接池
            hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
            await coordinator.async_shutdown()
            raise
        # 只有"凭据被拒"才需要人；网络/接口临时故障（ConfigEntryNotReady）
        # 等下一轮轮询即可 —— 给网络问题弹"请重新登录"是误导。
        if isinstance(err, ConfigEntryAuthFailed):
            _async_start_reauth(hass, entry)
        return True

    hass.data[DOMAIN][entry.entry_id] = coordinator
    # 选项（轮询间隔 / webhook 密钥）变更后重载；凭证滑动续期只改 data，
    # 不会触发重载，否则每次轮询都会重载条目形成死循环。
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        # 同上：forward 抛异常时 hass.data 里不能留旧 coordinator，
        # 否则 HA 重试新建的 coordinator 会覆盖它，旧 session 永久孤儿。
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        await coordinator.async_shutdown()
        raise
    return True


def _entry_has_entities(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """该条目是否已在实体注册表里留下实体（决定能否降级加载而不是卸载）。"""
    try:
        registry = er.async_get(hass)
        return bool(er.async_entries_for_config_entry(registry, entry.entry_id))
    except Exception as err:  # noqa: BLE001 - 注册表不可用时按全新条目处理（安全侧）
        _LOGGER.debug("读取实体注册表失败，按全新条目处理: %s", err)
        return False


def _log_degraded_setup(entry: ConfigEntry, err: BaseException) -> None:
    """降级加载的告警日志：认证失败与暂时不可用分开记，便于线上区分。"""
    if isinstance(err, ConfigEntryAuthFailed):
        _LOGGER.warning(
            "%s 首次刷新认证失败（%s），保留已有实体并转入重新认证", entry.title, err
        )
    elif isinstance(err, ConfigEntryNotReady):
        _LOGGER.warning(
            "%s 首次刷新暂时失败（%s），保留已有实体等待下次轮询", entry.title, err
        )
    else:
        _LOGGER.warning(
            "%s 首次刷新异常（%s: %s），保留已有实体继续加载",
            entry.title, type(err).__name__, err,
        )


def _async_start_reauth(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """主动发起重新认证；旧版 HA 没有 async_start_reauth 时直接起 reauth flow。"""
    starter = getattr(entry, "async_start_reauth", None)
    if callable(starter):
        for args in ((hass,), ()):
            try:
                starter(*args)
                return
            except TypeError:
                continue  # 新版/旧版签名不同（hass 参数有/无），换一种再试
            except Exception as err:  # noqa: BLE001 - 退回 flow.async_init
                _LOGGER.debug("entry.async_start_reauth 不可用，退回 flow.async_init: %s", err)
                break
    try:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "reauth", "entry_id": entry.entry_id},
                data=entry.data,
            )
        )
    except Exception as err:  # noqa: BLE001 - 起 flow 失败不该影响集成加载
        _LOGGER.warning("%s 发起重新认证失败: %s", entry.title, err)


async def _async_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    coordinator = _coordinators(hass).get(entry.entry_id)
    if coordinator is None:
        return
    if dict(entry.options) == coordinator.options_snapshot:
        return
    coordinator.options_snapshot = dict(entry.options)
    _LOGGER.debug("%s 选项已变更，重载条目", entry.title)
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    coordinator = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if isinstance(coordinator, CarrierDataUpdateCoordinator):
        await coordinator.async_shutdown()
    return unload_ok


async def _async_register_frontend_assets(hass: HomeAssistant) -> None:
    """注册集成前端资源（图标覆盖 JS + 静态图片）。"""
    integration_path = os.path.dirname(__file__)
    js_path = os.path.join(integration_path, "js", "icon_override.js")
    img_dir = os.path.join(integration_path, "images")

    paths: list[tuple[str, str, bool]] = []
    js_url = f"/api/custom_components/{DOMAIN}/js/icon_override.js"
    if os.path.exists(js_path):
        paths.append((js_url, js_path, False))
    if os.path.isdir(img_dir):
        paths.append((f"/api/custom_components/{DOMAIN}/images", img_dir, True))
    if not paths:
        return

    try:
        # HA 2024.7+ 的新 API；旧版本回退到 register_static_path
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(url, path, cache) for url, path, cache in paths]
        )
    except ImportError:
        for url, path, cache in paths:
            hass.http.register_static_path(url, path, cache_headers=cache)

    # 注入前端模块（图标覆盖）。add_extra_js_url 需要 frontend 已加载，
    # 未启用前端时静默跳过。
    if any(url == js_url for url, _path, _cache in paths):
        try:
            from homeassistant.components.frontend import add_extra_js_url

            add_extra_js_url(hass, js_url)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("前端模块注入跳过: %s", err)
