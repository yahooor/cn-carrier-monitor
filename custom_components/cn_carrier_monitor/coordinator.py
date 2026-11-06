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

from .api import (
    CarrierAuthError,
    CarrierError,
    CarrierMaintenanceError,
    create_client,
)
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

    # 连续失败断路器：连续 >=5 次失败后熔断（保护运营商风控额度）
    FAILURE_THRESHOLD = 5
    # 熔断冷却的**下限**（秒）。实际取值见 `_circuit_cooldown()`：必须 >= 当前
    # 轮询间隔，否则熔断分支永远进不去。
    #
    # 旧值 900s 是硬编码常数 < 轮询间隔 1800s，而 `_last_fail_at` 每轮失败
    # 都会刷新 → 条件 `time.time() - self._last_fail_at < CIRCUIT_COOLDOWN`
    # 恒为假 → 熔断（唯一会 return 而非 raise、从而能通知 listener 的分支）
    # 形同虚设。实测移动账号连续失败 75 次一次都没进过熔断。
    CIRCUIT_COOLDOWN = 3600.0
    # 运营商维护态的固定退避（秒）：维护通常持续数小时到数天，
    # 每 30 分钟打一次必然失败的请求只会徒增风控暴露面。
    MAINTENANCE_INTERVAL = 3 * 3600.0
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
        # 基准间隔：认证失败后按倍数自适应拉长，恢复后回到这个值
        self._base_interval = timedelta(minutes=minutes, seconds=jitter)
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{self.carrier}_{entry.data.get('phone', '')}",
            update_interval=self._base_interval,
        )

        self._fail_streak = 0
        self._last_fail_at = 0.0
        self._last_sms_sent_at = 0.0
        self._auth_fail_streak = 0
        # 运营商维护态标志：维护期间凭据有效，既不重登也不计入熔断
        self._maintenance = False
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
            # client 的 data 合并 options：proxy_url 等运行参数在选项里改，
            # client 每次重建（reload/凭证续期）都能拿到最新值
            client_data = {**dict(entry.options), **dict(entry.data)}
            self.client = create_client(self.carrier, self.session, client_data)
        except Exception:
            # 构造失败时不能留下未关闭的会话
            hass.async_create_task(self.session.close())
            raise

    # ------------------------------------------------------------------ update

    def _circuit_cooldown(self) -> float:
        """本次熔断的有效冷却秒数：至少覆盖一个轮询间隔。

        为什么不能是固定常数：轮询间隔 = 用户配置 + 0-59s 随机抖动，用户还
        可能把间隔改成 60 分钟甚至更长。固定 3600s 在间隔 3617s 时仍然不够
        —— 熔断分支要求 `now - _last_fail_at < cooldown`，而 `_last_fail_at`
        每轮失败都刷新，所以冷却必须**严格大于**轮询间隔才可能命中。
        """
        interval = self._base_interval.total_seconds()
        return max(self.CIRCUIT_COOLDOWN, interval * 2)

    async def _async_update_data(self) -> dict[str, Any]:
        if (
            self._fail_streak >= self.FAILURE_THRESHOLD
            and time.time() - self._last_fail_at < self._circuit_cooldown()
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
        except CarrierMaintenanceError as err:
            # 运营商系统升级/维护：凭据有效，重登、重签 d.sid、换 IP 都无效。
            # 因此这里刻意**不**做以下任何一件事（2026-11-07 修正）：
            #   - 不累加 _fail_streak（熔断针对可自愈故障，维护期熔断无意义）
            #   - 不推高 _auth_fail_streak（否则退避因子被认证语义污染）
            #   - 不触发 _notify_needs_human（弹「请重新登录」对维护是误导）
            #   - 不触发自动重登发码（白烧短信费 + 加倍风控暴露）
            #   - 不触发 ConfigEntryAuthFailed（不发 reauth 卡片）
            # 实测维护期曾被误判成 auth_expired 15 次、network 278 次。
            self.last_error = self._error_info(err)
            self._last_fail_at = time.time()
            self._maintenance = True
            await self._emit_event(
                EVENT_FAILURE,
                err=err,
                started=started,
                fail_streak=self._fail_streak,
            )
            _LOGGER.info(
                "%s 运营商维护中（凭据有效，等待恢复即可）: %s",
                self.entry.title, err,
            )
            # 维护期用固定的长间隔（3 小时），避免每 30 分钟打一次必然失败的请求
            self.update_interval = timedelta(seconds=self.MAINTENANCE_INTERVAL)
            # 走 return 路径：HA 的成功分支会 `self.data = data` 并调用
            # async_update_listeners()，实体属性（含 session_state=maintenance）
            # 会自动刷新，无需额外通知（额外通知会造成两次写入与状态抖动）。
            return self._degraded_snapshot(err, maintenance=True)
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
            # 认证失败后拉长轮询间隔（无人处理时不要每 30 分钟空转一次）
            self._apply_adaptive_interval()
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
            # 无历史数据 → 走 HA 标准 reauth 流程。HA 在这条路径上同样不会
            # 调用 async_update_listeners()，所以显式通知一次，让 session_state
            # 传感器能显示 needs_human 而不是停在旧的 healthy。
            self._notify_failure_state()
            raise ConfigEntryAuthFailed(str(err)) from err
        except CarrierError as err:
            self.last_error = self._error_info(err)
            await self._emit_event(EVENT_FAILURE, err=err, started=started)
            self._fail_streak += 1
            self._last_fail_at = time.time()
            # network 类失败同样要退避：原先只有认证失败才退避，network 一直
            # 按 30 分钟硬打（实测维护期 38 小时打了 380+ 次必然失败的请求）。
            self._apply_network_backoff()
            # 失败也要让实体刷新状态（HA 只在成功路径通知 listener）
            self._notify_failure_state()
            raise UpdateFailed(str(err)) from err
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            self.last_error = self._error_info(err)
            await self._emit_event(EVENT_FAILURE, err=err, started=started)
            self._fail_streak += 1
            self._last_fail_at = time.time()
            self._apply_network_backoff()
            self._notify_failure_state()
            _LOGGER.exception("%s 更新时出现未预期异常", self.entry.title)
            raise UpdateFailed(f"查询失败: {err}") from err

        self._fail_streak = 0
        self._auth_fail_streak = 0
        self._maintenance = False
        self.last_error = None
        # 恢复成功 → 回到基准轮询间隔
        self.update_interval = self._base_interval
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

    def _degraded_snapshot(
        self, err: BaseException, *, maintenance: bool = False
    ) -> dict[str, Any]:
        """失败时保留旧数据的降级快照。

        返回（而非抛异常）有两个作用：
        1. 实体保留上次数值而不是整条卸载（"降级保实体"设计）；
        2. **return 路径会触发 HA 的 listener 通知**——抛 UpdateFailed 不会。
           这正是 session_state 传感器此前停更的根因（见 `_notify_failure_state`）。
        """
        degraded = dict(self.data) if isinstance(self.data, dict) else {}
        extra: dict[str, Any] = dict(degraded.get("extra") or {})
        extra["fail_streak"] = self._fail_streak
        if maintenance:
            extra["maintenance"] = True
            extra["maintenance_error"] = str(err)[:120]
        degraded["extra"] = extra
        return degraded

    def _notify_failure_state(self) -> None:
        """失败后主动通知实体刷新状态。

        HA 2026.10.4 `helpers/update_coordinator.py` 的 `_async_refresh` 尾部：

        ```python
        if not self.last_update_success and not previous_update_success:
            return          # 连续失败（本次失败 + 上次也失败）→ 直接返回，不通知
        if (self.always_update or ...):
            self.async_update_listeners()
        ```

        即 HA 只在**状态跃迁**时通知 listener：首次失败（成功→失败）会通知一次，
        此后的**连续失败全部被早退吞掉**——`always_update=True` 也救不了，因为
        早退发生在 `always_update` 判断之前。

        实测后果（2026-11-07）：HA 于 11-05 10:53 重启后，移动账号
        session_state 传感器在首次失败那一刻写下 `fail_streak=1`（正是那次跃迁
        通知），之后连续失败 75 次，传感器属性**始终停在 fail_streak=1**——
        与协调器实际状态严重脱节，掉线告警自动化会拿到过期数据。

        这里在失败路径显式调用 `async_update_listeners()`，让
        `CarrierSessionStateSensor` 能重算 native_value 与 extra_state_attributes。
        顺序要求：先置 `last_update_success = False`，否则
        `CarrierSensor.available`（判据 `not last_update_success and not self._data`）
        会用旧值算出 available=True，把本该 unavailable 的实体写成 unknown。
        """
        self.last_update_success = False
        try:
            self.async_update_listeners()
        except Exception as err:  # noqa: BLE001 - 通知失败不影响更新流程
            _LOGGER.debug("失败状态通知 listener 异常（忽略）: %s", err)

    # ------------------------------------------------------------------ 会话状态

    @property
    def fail_streak(self) -> int:
        """连续失败次数（含网络/认证）。"""
        return self._fail_streak

    @property
    def auth_fail_streak(self) -> int:
        """连续认证失败次数。"""
        return self._auth_fail_streak

    @property
    def session_state(self) -> str:
        """会话健康度（供 session_state 实体与诊断包使用）。

        判定顺序即严重度：熔断 > 需人工 > 降级 > 维护中 > 正常。

        `maintenance`（2026-11-07 新增）：运营商整站升级维护。此时凭据有效、
        不需要人处理，区别于"需要重新登录"——自动化据此可以既不弹告警，
        也不把账号标记为异常。
        """
        if self._fail_streak >= self.FAILURE_THRESHOLD:
            return "circuit_open"
        if self._auth_fail_streak > 0:
            return "degraded" if self.data else "needs_human"
        if self._maintenance:
            return "maintenance"
        return "healthy"

    def _apply_adaptive_interval(self) -> None:
        """认证失败后按 1/2/4/8 倍拉长轮询间隔（封顶 24h）。

        Cookie 类账号失效后无人处理时，30 分钟一次的空转既刷日志又白白
        消耗运营商风控额度；熔断只管"要不要发请求"，间隔决定"多久醒一次"。
        """
        factor = min(2 ** max(self._auth_fail_streak - 1, 0), 8)
        target = self._base_interval * factor
        self.update_interval = min(target, timedelta(hours=24))
        _LOGGER.debug(
            "%s 认证失败 %d 次，轮询间隔调整为 %s",
            self.entry.title, self._auth_fail_streak, self.update_interval,
        )

    def _apply_network_backoff(self) -> None:
        """网络/传输类失败后拉长轮询间隔（2 倍起，封顶 6 小时）。

        实测（2026-11-07）：移动账号因运营商维护连续失败 38 小时 380+ 次，
        每次间隔仍是基准 30 分钟——因为退避原先只挂在认证失败分支上。
        对"必然失败"的端点继续按 30 分钟硬打，只会徒增风控暴露面。

        首次失败即 2 倍（而不是 1 倍）：网络类失败往往不是一次性的，
        立刻退避比"下次再说"更省风控额度。

        2 倍起步而不是 8 倍：网络故障通常是临时的，过度退避会让恢复变慢。
        """
        factor = min(2 ** max(self._fail_streak, 1), 4)
        target = self._base_interval * factor
        self.update_interval = min(target, timedelta(hours=6))
        _LOGGER.debug(
            "%s 网络类失败 %d 次，轮询间隔调整为 %s",
            self.entry.title, self._fail_streak, self.update_interval,
        )

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

    def _build_relogin_client(self):
        """为自动发码构造临时客户端（telecom/unicom 短信模式专用）。"""
        from .api import TelecomClient, UnicomClient

        if self.carrier == "telecom":
            return TelecomClient(self.session, dict(self.entry.data))
        if self.carrier == "unicom":
            return UnicomClient(self.session, dict(self.entry.data))
        return None

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
