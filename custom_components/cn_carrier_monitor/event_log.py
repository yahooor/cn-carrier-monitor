"""结构化更新事件日志：失败四分类 + 落盘 JSONL。

每次 coordinator 更新结果追加一行 JSON 到 HA 配置目录下的
``cn_carrier_monitor_events.jsonl``；失败必须归入四类之一：

- auth_expired:   CarrierAuthError（会话/凭证失效）
- protocol_drift: ParseAnomalyError（HTTP 200 但响应结构全空/未知/解密失败）
- network:        连接/DNS/TLS/超时（aiohttp ClientError、TimeoutError、
                  客户端封装的"网络错误/请求超时"CarrierError）
- internal:       其它异常

文件超过 1MB 轮转为 ``.jsonl.1``（只保留一代）。追加模式、写失败只记
debug 日志，保证集成卸载/重启期间不会因日志反而破坏更新流程。
本模块不 import homeassistant，hass 仅按鸭子类型使用（便于无 HA 单测）。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

_LOGGER = logging.getLogger(__name__)

FILE_NAME = "cn_carrier_monitor_events.jsonl"
MAX_BYTES = 1_000_000

CATEGORY_AUTH = "auth_expired"
CATEGORY_DRIFT = "protocol_drift"
CATEGORY_NETWORK = "network"
CATEGORY_INTERNAL = "internal"

EVENT_SUCCESS = "update_success"
EVENT_FAILURE = "update_failure"
EVENT_CIRCUIT_OPEN = "circuit_open"

# CarrierClient._request 把 aiohttp 异常封装成这两种文案的 CarrierError
_NETWORK_MESSAGE_MARKS = ("网络错误", "请求超时")


def classify_exception(err: BaseException) -> str:
    """把更新异常归入失败四分类之一。"""
    from .api.base import CarrierAuthError, CarrierError, ParseAnomalyError

    if isinstance(err, CarrierAuthError):
        return CATEGORY_AUTH
    if isinstance(err, ParseAnomalyError):
        return CATEGORY_DRIFT
    if isinstance(err, CarrierError):
        if any(mark in str(err) for mark in _NETWORK_MESSAGE_MARKS):
            return CATEGORY_NETWORK
        return CATEGORY_INTERNAL
    # 逃逸出客户端封装的底层网络错误（DNS=gaierror、TLS=ssl.SSLError 均是 OSError）
    if isinstance(err, (TimeoutError, ConnectionError, OSError)):
        return CATEGORY_NETWORK
    if type(err).__module__.split(".")[0] in ("aiohttp", "socket", "_socket", "ssl"):
        return CATEGORY_NETWORK
    return CATEGORY_INTERNAL


def _append_line(path: str, line: str) -> None:
    """同步追加一行；超过 1MB 先轮转（.1 只保留一代）。在 executor 线程执行。"""
    if os.path.exists(path) and os.path.getsize(path) >= MAX_BYTES:
        rotated = path + ".1"
        if os.path.exists(rotated):
            os.remove(rotated)
        os.replace(path, rotated)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


async def emit(hass: Any, event_type: str, data: dict[str, Any]) -> None:
    """追加一条事件记录；任何失败都只降级为 debug 日志，绝不影响更新流程。"""
    try:
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": event_type,
            **data,
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        path = hass.config.path(FILE_NAME)
    except Exception:  # noqa: BLE001 - 埋点永不抛出
        _LOGGER.debug("事件日志构造失败", exc_info=True)
        return
    try:
        await hass.async_add_executor_job(_append_line, path, line)
    except Exception as err:  # noqa: BLE001 - 卸载/重启窗口期磁盘可能不可写
        _LOGGER.debug("事件日志写入失败: %s", err)
