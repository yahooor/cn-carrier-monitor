"""Base classes for carrier API clients."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import aiohttp

from ..const import REQUEST_TIMEOUT

_LOGGER = logging.getLogger(__name__)


def parse_safe(func):
    """解析器装饰器：容器/数值类型崩溃统一转 ParseAnomalyError（protocol_drift）。

    运营商字段类型随省份/套餐漂移（fuzz 实证），任何深层 .get 链崩溃都不该
    让整次更新以裸 traceback 失败——转为领域异常后由事件日志归类。
    """
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ParseAnomalyError:
            raise
        except (ValueError, TypeError, AttributeError, KeyError, IndexError,
                ArithmeticError) as err:
            # ArithmeticError 覆盖 OverflowError（超大数值→inf）/ZeroDivisionError
            raise ParseAnomalyError(
                f"{func.__name__}: 响应结构异常 {type(err).__name__}: {err}") from None

    return wrapper


class CarrierError(Exception):
    """A recoverable carrier API error (connection/protocol/remote)."""

    def __init__(self, message: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail


class CarrierAuthError(CarrierError):
    """The stored credentials/session are no longer accepted."""

    def __init__(self, message: str, detail: str | None = None) -> None:
        super().__init__(message, detail)


class ParseAnomalyError(CarrierError):
    """HTTP 200 but the response is structurally empty/unknown (protocol drift)."""


class CarrierMaintenanceError(CarrierError):
    """运营商侧维护/升级公告页（暂时性：凭据有效，重登无效，重试无用）。

    2026-11-05 线上实测：中国移动 wx.10086.cn 整站系统升级期间，所有端点
    （含 newHome 根路径）都返回同一张 HTML 公告页，HTTP 500 / 404 均可能。
    这类响应既不是"端点下线"（永久，需要人改代码），也不是"网络故障"
    （用户网络没问题），更不是"会话失效"（凭证完全有效）—— 单独归一类。
    """

    def __init__(self, message: str, detail: str | None = None) -> None:
        super().__init__(message, detail)


# 运营商维护/升级公告页的 HTML 特征（大小写不敏感）。
_MAINTENANCE_HTML_MARKERS = ("<html", "<!doctype html", "<title", "<body")
# 公告页正文/标题的中文特征词。实测有两种形态：
#   形态 A（业务端点）：<title>升级公告</title> + "由于系统升级，部分功能暂无法提供服务"
#   形态 B（站点根路径）："为给您提供更优质的服务，我们正在进行系统优化升级。
#                          升级期间将影响业务的查询和办理……敬请谅解"
#
# **强特征词**：单独出现即可判定为维护。这些词高度特异于公告页。
_MAINTENANCE_TEXT_MARKERS = (
    "升级公告",
    "系统升级",
    "系统优化升级",
    "升级期间",
    "维护升级",
    "暂无法提供服务",
    "正在玩命调试",
)
# **弱特征词**：通用礼貌语/错误页常用语，单独出现不足以判定为维护。
# "稍后再试" / "敬请谅解" 在真实故障页、限流页里也常见；"系统维护" 在用户
# 协议、帮助文档里也常见——若把它们当强特征，一次真实故障会被永久误判成
# "维护中"，比原缺陷更难排查（用户看到的只是"运营商维护中"，永远不会去查
# 真实原因）。
_MAINTENANCE_WEAK_TEXT_MARKERS = (
    "稍后再试",
    "敬请谅解",
    "请稍后",
    "系统维护",
)
# 弱特征词需要搭配的上下文：必须同时在解释"为什么不可用"。
# 刻意**不含**「维护」「升级」——因为「系统维护」本身就是弱特征词，若把
# 「维护」列为上下文，"本协议涉及设备系统维护说明"会自命中（QA 实测过）。
_MAINTENANCE_WEAK_CONTEXT_MARKERS = (
    "系统优化",
    "升级公告",
    "正在升级",
    "优化升级",
    "暂停",
    "不可用",
    "暂时无法",
    "稍后开放",
)


def is_maintenance_notice(body: str) -> bool:
    """判断响应体是否为运营商维护/升级公告页。

    判据是「HTML 页面 + 公告特征词」，**不看状态码**：同一个 404/500 既可能
    是公告页（暂时性，运营商升级完自己恢复），也可能是真的端点下线或网关
    故障（永久性/故障）。只看状态码会把维护误判成 protocol_drift（污染漂移率
    统计）或 network（让用户以为网络坏了）。

    特征词分强/弱两档：强特征词单独即可判定；弱特征词（"稍后再试"、
    "敬请谅解"）必须同时出现上下文词（"升级"/"维护"/"优化"…），否则会把
    真实故障页误判成维护。
    """
    if not body:
        return False
    lowered = body.lower()
    if not any(mark in lowered for mark in _MAINTENANCE_HTML_MARKERS):
        return False
    if any(mark in body for mark in _MAINTENANCE_TEXT_MARKERS):
        return True
    if any(mark in body for mark in _MAINTENANCE_WEAK_TEXT_MARKERS) and any(
        mark in body for mark in _MAINTENANCE_WEAK_CONTEXT_MARKERS
    ):
        return True
    return False


def empty_data(carrier: str, phone: str) -> dict[str, Any]:
    """Normalized snapshot returned by every carrier client."""
    return {
        "carrier": carrier,
        "phone": phone,
        "balance": None,      # 元, float
        "month_cost": None,   # 元, float
        "plan_name": None,
        "plan_price": None,   # 套餐月费（如 "8元/月"）
        "location": None,     # 归属地（如 湖南张家界）
        "last_update": None,  # 数据更新时间（datetime）
        "star_level": None,   # 星级
        "real_name": None,    # 实名状态
        "owe_fee": None,      # 欠费金额（元）
        "credit_limit": None, # 信用额度（元）
        "pay_type": None,     # 付费类型（预付费/后付费）
        "special_balance": None,  # 专用余额（元）
        "phone_region": None,     # 归属地完整（省+市）
        "account_status": None,   # 账户状态（正常在网 / 欠费 / 停机）
        "points": None,
        "flow_general": None,  # {"used": GB, "total": GB, "remain": GB}
        "flow_directed": None,
        "flow_total": None,
        "voice": None,         # {"used", "total", "remain"} 分钟
        "sms": None,           # {"used", "total", "remain"} 条
        "extra": {},
        "updated_at": None,
    }


def finish(data: dict[str, Any]) -> dict[str, Any]:
    # astimezone() 保证带本地时区偏移，跨时区日志/诊断不再有歧义
    data["updated_at"] = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return data


def to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> int | None:
    num = to_float(value)
    return int(num) if num is not None else None


def usage_block(used: Any, total: Any, remain: Any) -> dict[str, float | None] | None:
    """Build a usage dict when at least one of the values is numeric."""
    used_f, total_f, remain_f = to_float(used), to_float(total), to_float(remain)
    if used_f is None and total_f is None and remain_f is None:
        return None
    if used_f is None and total_f is not None and remain_f is not None:
        used_f = max(total_f - remain_f, 0)
    if remain_f is None and used_f is not None and total_f is not None:
        remain_f = total_f - used_f
    if total_f is None and used_f is not None and remain_f is not None:
        total_f = used_f + remain_f
    return {"used": used_f, "total": total_f, "remain": remain_f}


class CarrierClient:
    """Common plumbing: per-account session, JSON posts, error mapping."""

    carrier = "base"

    def __init__(self, session: aiohttp.ClientSession, data: dict[str, Any]) -> None:
        self._session = session
        self._data = data
        # 出站代理（可选）：d.sid 等 IP 绑定型凭据需要固定的签发/使用出口。
        # 形如 http://192.168.31.123:7890；为空 = 直连（默认行为不变）
        self._proxy = str(data.get("proxy_url") or "").strip() or None

    @property
    def phone(self) -> str:
        return str(self._data.get("phone") or "")

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict | None = None,
        form: dict | None = None,
        data: str | None = None,
        headers: dict | None = None,
        timeout: int = REQUEST_TIMEOUT,
        allow_error_status: bool = True,
        content_type_override: str | None = None,
    ) -> tuple[int, str]:
        merged = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36"}
        if headers:
            merged.update(headers)
        if content_type_override:
            merged["Content-Type"] = content_type_override
        try:
            async with self._session.request(
                method,
                url,
                json=json_body,
                data=data if data is not None else form,
                headers=merged,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=False,
                proxy=self._proxy,
            ) as resp:
                body = await resp.text()
                if resp.status >= 400 and not allow_error_status:
                    raise CarrierError(f"{url} HTTP {resp.status}")
                if resp.status >= 400:
                    _LOGGER.debug("%s HTTP %s: %s", url, resp.status, body[:200])
                # Set-Cookie 交给子类回收（如移动 wx 通道的 uuid→d.sid 重签）；
                # 空实现兜底，不影响不关心 Cookie 的运营商
                self.absorb_cookies(getattr(resp, "cookies", None))
                return resp.status, body
        except aiohttp.ClientSSLError as err:
            # 证书校验失败是配置/环境问题，不该被当成"运营商改协议"反复重试
            raise CarrierError(
                f"TLS 校验失败: {err}（若为运营商自签证书，可临时设置 "
                "CN_CARRIER_INSECURE_TLS=1，但会失去 MITM 防护）"
            ) from err
        except aiohttp.ClientError as err:
            raise CarrierError(f"网络错误: {err}") from err
        except TimeoutError as err:
            raise CarrierError(f"请求超时: {url}") from err

    def absorb_cookies(self, cookies: Any) -> None:
        """Hook for subclasses to merge Set-Cookie from a response (base: no-op)."""

    async def async_fetch(self) -> dict[str, Any]:
        """Fetch one normalized snapshot; raise CarrierAuthError on reauth."""
        raise NotImplementedError

    def export_credentials(self) -> dict[str, Any]:
        """Values that must be persisted after a successful login."""
        return {}
