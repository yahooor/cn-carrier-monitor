"""DataUpdateCoordinator for China Carrier Monitor."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CarrierAuthError, CarrierError, create_client
from .const import CARRIER_LABELS, DEFAULT_INTERVAL, DOMAIN
from .event_log import (
    EVENT_CIRCUIT_OPEN,
    EVENT_FAILURE,
    EVENT_SUCCESS,
    classify_exception,
    emit,
)
from .net import build_connector

_LOGGER = logging.getLogger(__name__)

# 凭证滑动续期的最小落盘间隔（秒）：每个轮询都写 config entry 会磨损 SD 卡，
# 也会让 .storage 频繁重写。
CREDENTIAL_PERSIST_INTERVAL = 600.0


class CarrierDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Per-account coordinator: one config entry = one phone account."""

    # 连续失败断路器：连续 >=5 次失败后熔断 15 分钟（保护运营商风控额度）
    FAILURE_THRESHOLD = 5
    CIRCUIT_COOLDOWN = 900.0
    # 自动重登发码退避：首次 30 分钟，之后每次翻倍，上限 6 小时。
    # 早期固定 5 分钟冷却 = 一小时最多 12 条短信，既烧钱又必然触发运营商风控。
    SMS_COOLDOWN_BASE = 1800.0
    SMS_COOLDOWN_MAX = 6 * 3600.0

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.carrier = entry.data["carrier"]
        minutes = entry.options.get("interval", entry.data.get("interval", DEFAULT_INTERVAL))
        try:
            minutes = max(int(minutes), 5)
        except (TypeError, ValueError):
            minutes = DEFAULT_INTERVAL

        # 多账号错峰：全部条目在同一分钟建连容易触发运营商风控
        jitter = random.randint(0, 59)
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{self.carrier}_{entry.data.get('phone', '')}",
            update_interval=timedelta(minutes=minutes, seconds=jitter),
        )

        self._fail_streak = 0
        self._last_fail_at = 0.0
        self._last_sms_sent_at = 0.0
        self._auth_fail_streak = 0
        self._last_persist_at = 0.0
        self._relogin_lock = asyncio.Lock()
        # 掉线一次性通知是否已发（恢复成功后清零，下一轮掉线可再提示）
        self._needs_human_notified = False
        # 诊断用：最近一次失败的分类与摘要（不含凭证）
        self.last_error: dict[str, Any] | None = None
        # 选项快照：用于区分「用户改设置」与「凭证滑动续期」两种 entry 更新
        self.options_snapshot: dict[str, Any] = dict(entry.options)

        # 每个账号独立会话，避免 Cookie 串号；强制 IPv4 + 公共 DNS
        self.session = aiohttp.ClientSession(connector=build_connector(legacy_tls=True))
        try:
            self.client = create_client(self.carrier, self.session, dict(entry.data))
        except Exception:
            # 构造失败时不能留下未关闭的会话
            hass.async_create_task(self.session.close())
            raise

    # ------------------------------------------------------------------ update

    async def _async_update_data(self) -> dict[str, Any]:
        if (
            self._fail_streak >= self.FAILURE_THRESHOLD
            and time.time() - self._last_fail_at < self.CIRCUIT_COOLDOWN
        ):
            degraded = dict(self.data) if isinstance(self.data, dict) else {}
            extra = {
                **(degraded.get("extra") or {}),
                "circuit_open": True,
                "fail_streak": self._fail_streak,
            }
            # 熔断期间保留"掉线"语义：实体属性要能区分"会话失效"和"只是没数据"
            if self._auth_fail_streak > 0:
                extra.setdefault("login_expired", True)
                message = (self.last_error or {}).get("message")
                if message:
                    extra.setdefault("login_error", str(message)[:120])
            degraded["extra"] = extra
            await self._emit_event(EVENT_CIRCUIT_OPEN, fail_streak=self._fail_streak)
            # 熔断打开且是认证问题 → 一次性提示人工处理（重复调用由标记位吞掉）
            if self._auth_fail_streak > 0:
                self._notify_needs_human("会话/凭证失效，自动重试已熔断")
            return degraded

        started = time.perf_counter()
        try:
            data = await self.client.async_fetch()
        except CarrierAuthError as err:
            self.last_error = self._error_info(err)
            self._auth_fail_streak += 1
            # 认证失败同样计入熔断计数：否则 cookie 类账号会按轮询间隔无限
            # 无效重试（线上 22 小时 48 次 auth_expired），熔断器形同虚设。
            self._fail_streak += 1
            self._last_fail_at = time.time()
            circuit_open = self._fail_streak >= self.FAILURE_THRESHOLD
            await self._emit_event(
                EVENT_FAILURE,
                err=err,
                started=started,
                fail_streak=self._fail_streak,
                circuit_open=circuit_open,
            )
            _LOGGER.info(
                "%s 认证失败(%s)，连续失败 %s 次", self.entry.title, err, self._fail_streak
            )
            if self.supports_sms_relogin:
                if circuit_open:
                    # 熔断期内不再空转发码任务（每次 auth 失败都调度只会烧短信费）
                    _LOGGER.debug("%s 已熔断，跳过自动发码", self.entry.title)
                else:
                    self.hass.async_create_task(self._async_safe_send_relogin_sms())
            # 降级保实体（Pocket-Carrier 同策略）：有历史数据时返回旧值并标记
            # 登录失效，实体保持可用而不是整条卸载；首次（无数据）才走官方重认证
            has_data = isinstance(self.data, dict) and bool(self.data)
            if not has_data or circuit_open:
                self._notify_needs_human(f"认证失败：{err}")
            if has_data:
                degraded = dict(self.data)
                degraded["extra"] = {
                    **(degraded.get("extra") or {}),
                    "login_expired": True,
                    "login_error": str(err)[:120],
                    "fail_streak": self._fail_streak,
                    **({"circuit_open": True} if circuit_open else {}),
                }
                return degraded
            raise ConfigEntryAuthFailed(str(err)) from err
        except CarrierError as err:
            self.last_error = self._error_info(err)
            await self._emit_event(EVENT_FAILURE, err=err, started=started)
            self._fail_streak += 1
            self._last_fail_at = time.time()
            raise UpdateFailed(str(err)) from err
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            self.last_error = self._error_info(err)
            await self._emit_event(EVENT_FAILURE, err=err, started=started)
            self._fail_streak += 1
            self._last_fail_at = time.time()
            _LOGGER.exception("%s 更新时出现未预期异常", self.entry.title)
            raise UpdateFailed(f"查询失败: {err}") from err

        self._fail_streak = 0
        self._auth_fail_streak = 0
        self.last_error = None
        # 恢复成功 → 撤掉"需要人工"通知，避免陈旧告警长期挂在界面上
        self._dismiss_needs_human()
        await self._emit_event(EVENT_SUCCESS, data=data, started=started)
        # 会话滑动续期：把客户端刷新后的凭证（联通 token_online、移动 Cookie 等）
        # 写回配置条目；服务端会话顺延后重启 HA 不丢登录态
        try:
            self._async_persist_credentials()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("凭证回写失败（忽略）: %s", err)
        return data

    @staticmethod
    def _error_info(err: BaseException) -> dict[str, Any]:
        return {
            "category": classify_exception(err),
            "type": type(err).__name__,
            "message": str(err)[:200],
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ------------------------------------------------------------------ 掉线通知

    @property
    def needs_human_notification_id(self) -> str:
        """同一 entry 固定通知 ID：重复发送是原地更新，不会堆一串通知。"""
        return f"{DOMAIN}_reauth_{self.entry.entry_id}"

    def _needs_human_hint(self) -> str:
        """按认证模式给出用户可执行的处理动作（cookie 模式与短信模式差别很大）。"""
        mode = str(self.entry.data.get("auth_mode") or "cookie")
        if mode == "sms":
            return (
                "短信验证码模式：若已自动下发验证码，请把收到的验证码 POST 到 "
                "`/api/webhook/cn_carrier_monitor_sms`（JSON: "
                '{"phone": "号码", "code": "验证码"}），或在集成的「重新认证」里手动输入。'
            )
        if mode == "password":
            return "服务密码模式：请确认密码未被修改，并在集成的「重新认证」里重新提交。"
        return (
            "会话/Cookie 模式：会话已失效且无法自动续期。请在浏览器重新登录后，"
            "用 `cn_carrier_monitor.inject_token` 服务注入新的 cookies，"
            "或在集成的「重新认证」里重新导入会话。"
        )

    def _notify_needs_human(self, reason: str) -> None:
        """账号掉线且无法自愈时发一条 persistent_notification（每个 entry 一次）。"""
        if self._needs_human_notified:
            return
        try:
            from homeassistant.components.persistent_notification import (
                async_create as pn_create,
            )

            carrier = CARRIER_LABELS.get(self.carrier, self.carrier)
            pn_create(
                self.hass,
                title="中国运营商话费监控：账号需要人工处理",
                message=(
                    f"{carrier} 账号 {self.entry.title} 已掉线，自动重试无法恢复。\n\n"
                    f"原因：{reason}\n\n"
                    f"{self._needs_human_hint()}\n\n"
                    "实体已保留（显示为 unavailable），重新登录成功后本通知会自动消失。"
                ),
                notification_id=self.needs_human_notification_id,
            )
            self._needs_human_notified = True
        except Exception as err:  # noqa: BLE001 - 通知失败绝不能影响更新流程
            _LOGGER.debug("掉线通知发送失败（忽略）: %s", err)

    def _dismiss_needs_human(self) -> None:
        """恢复成功后撤掉掉线通知，并允许下一次掉线重新提示。"""
        if not self._needs_human_notified:
            return
        try:
            from homeassistant.components.persistent_notification import (
                async_dismiss as pn_dismiss,
            )

            pn_dismiss(self.hass, self.needs_human_notification_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("掉线通知清理失败（忽略）: %s", err)
            return
        self._needs_human_notified = False

    # ------------------------------------------------------------------ 事件埋点

    _EVENT_PAYLOAD_FIELDS = ("carrier", "phone")

    async def _emit_event(
        self,
        event_type: str,
        *,
        err: BaseException | None = None,
        started: float | None = None,
        data: dict[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        """把一次更新结果写入结构化事件日志（四分类）；埋点本身绝不抛出。"""
        try:
            payload: dict[str, Any] = {
                key: self.entry.data.get(key, "") for key in self._EVENT_PAYLOAD_FIELDS
            }
            if started is not None:
                payload["duration_s"] = round(time.perf_counter() - started, 2)
            if err is not None:
                payload["category"] = classify_exception(err)
                payload["err_type"] = type(err).__name__
                payload["err"] = str(err)[:200]
            if data is not None:
                payload["fields"] = sorted(
                    k for k, v in data.items()
                    if k not in ("carrier", "phone", "extra") and v is not None
                )
                payload["extra_keys"] = sorted((data.get("extra") or {}).keys())
            payload.update(extra)
            await emit(self.hass, event_type, payload)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("事件埋点失败（忽略）", exc_info=True)

    # ------------------------------------------------------------------ 凭证回写

    @staticmethod
    def _looks_like_regression(old: Any, new: Any) -> bool:
        """新凭证是否明显比旧凭证更差（空值 / Cookie 名骤减）。

        历史教训：无条件的"有变化就写回"会把一次异常响应里的空 Cookie
        覆盖掉有效会话，导致后续所有请求都 401。
        """
        if new is None or new == "":
            return True
        if old in (None, ""):
            return False
        old_s, new_s = str(old), str(new)
        if ";" not in old_s or "=" not in old_s:
            return False
        old_names = {p.split("=", 1)[0].strip() for p in old_s.split(";") if "=" in p}
        new_names = {p.split("=", 1)[0].strip() for p in new_s.split(";") if "=" in p}
        if not new_names:
            return True
        return len(new_names) < max(1, len(old_names) // 2)

    def _async_persist_credentials(self) -> None:
        now = time.time()
        if now - self._last_persist_at < CREDENTIAL_PERSIST_INTERVAL:
            return
        exported = self.client.export_credentials() or {}
        updates = {}
        for key, value in exported.items():
            if value in (None, ""):
                continue
            old = self.entry.data.get(key)
            if old == value:
                continue
            if self._looks_like_regression(old, value):
                _LOGGER.debug("跳过疑似退化的凭证回写: %s", key)
                continue
            updates[key] = value
        if not updates:
            return
        self._last_persist_at = now
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

    def _sms_cooldown(self) -> float:
        """连续认证失败的指数退避（30min → 60 → 120 → 240 → 360 封顶）。"""
        step = min(max(self._auth_fail_streak - 1, 0), 4)
        return min(self.SMS_COOLDOWN_BASE * (2 ** step), self.SMS_COOLDOWN_MAX)

    async def async_send_relogin_sms(self) -> str:
        """token 失效后自动下发登录短信（指数退避 + 单飞防并发发码）。"""
        async with self._relogin_lock:
            now = time.time()
            if now - self._last_sms_sent_at < self._sms_cooldown():
                return "cooldown"
            client = self._build_relogin_client()
            if client is None:
                return "unsupported"
            if self.carrier == "telecom":
                ok = await client.async_login_sms_send()
            else:
                hint = await client.async_sms_trigger()
                ok = "已下发" in hint or "成功" in hint
            if not ok:
                return "failed"
            self._last_sms_sent_at = now
            # 发码会话状态（uid/key/isct、device_id）必须留存，否则验证码提交
            # 会因会话不一致被运营商拒绝
            state = getattr(client, "export_sms_state", None)
            if callable(state):
                self._store_sms_state(state())
            self._notify_sms_sent()
            return "sent"

    def _store_sms_state(self, state: dict[str, Any]) -> None:
        from .const import DATA_SMS_STATE

        if not state:
            return
        store = self.hass.data.setdefault(DATA_SMS_STATE, {})
        store[self.entry.data.get("phone", "")] = state

    def _notify_sms_sent(self) -> None:
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
            # 验证码绑定发码会话（uid/key/isct）——必须恢复同状态提交
            self._import_saved_sms_state(client)
            creds = await client.async_login_sms_submit(code)
            updates = {
                "token": creds["token"],
                "province_code": creds["province_code"],
                "city_code": creds["city_code"],
            }
        else:
            self._import_saved_sms_state(client)
            creds = await client.async_sms_login(code)
            updates = {"token": creds["token"], "app_id": creds.get("app_id", "")}
            updates = {k: v for k, v in updates.items() if v}
        new_data = {**self.entry.data, **updates}
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        # 重建 client 以载入新 token
        self.client = create_client(self.carrier, self.session, dict(new_data))
        # 人工重登成功后清零退避计数，否则下一次失效仍按最长冷却等待
        self._fail_streak = 0
        self._auth_fail_streak = 0
        self._last_sms_sent_at = 0.0
        await self.async_request_refresh()
        from homeassistant.components.persistent_notification import (
            async_dismiss as pn_dismiss,
        )

        pn_dismiss(self.hass, f"{DOMAIN}_sms_{self.entry.entry_id}")
        return True

    def _import_saved_sms_state(self, client) -> None:
        from .const import DATA_SMS_STATE

        saved = self.hass.data.get(DATA_SMS_STATE, {}).get(
            self.entry.data.get("phone", "")
        )
        importer = getattr(client, "import_sms_state", None)
        if saved and callable(importer):
            importer(saved)

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        if not self.session.closed:
            await self.session.close()
