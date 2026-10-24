"""Base classes for carrier API clients."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import aiohttp

from ..const import REQUEST_TIMEOUT

_LOGGER = logging.getLogger(__name__)


class CarrierError(Exception):
    """A recoverable carrier API error (connection/protocol/remote)."""


class CarrierAuthError(CarrierError):
    """The stored credentials/session are no longer accepted."""


def empty_data(carrier: str, phone: str) -> dict[str, Any]:
    """Normalized snapshot returned by every carrier client."""
    return {
        "carrier": carrier,
        "phone": phone,
        "balance": None,      # 元, float
        "month_cost": None,   # 元, float
        "plan_name": None,
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
    data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
        headers: dict | None = None,
        timeout: int = REQUEST_TIMEOUT,
        allow_error_status: bool = True,
    ) -> tuple[int, str]:
        merged = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36"}
        if headers:
            merged.update(headers)
        try:
            async with self._session.request(
                method,
                url,
                json=json_body,
                data=form,
                headers=merged,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=False,
            ) as resp:
                body = await resp.text()
                if resp.status >= 400 and not allow_error_status:
                    raise CarrierError(f"{url} HTTP {resp.status}")
                if resp.status >= 400:
                    _LOGGER.debug("%s HTTP %s: %s", url, resp.status, body[:200])
                return resp.status, body
        except aiohttp.ClientError as err:
            raise CarrierError(f"网络错误: {err}") from err
        except TimeoutError as err:
            raise CarrierError(f"请求超时: {url}") from err

    async def async_fetch(self) -> dict[str, Any]:
        """Fetch one normalized snapshot; raise CarrierAuthError on reauth."""
        raise NotImplementedError

    def export_credentials(self) -> dict[str, Any]:
        """Values that must be persisted after a successful login."""
        return {}
