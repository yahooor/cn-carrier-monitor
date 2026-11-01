"""共享网络工具：稳定 DNS / 运营商 legacy TLS 的连接器。

安全约定（v1.5 起）：
- **默认校验证书**（CERT_REQUIRED + check_hostname）。早期版本用 CERT_NONE
  全局关闭校验来绕过运营商网关的旧 TLS 套件，这会让话费、服务密码、会话
  token 在链路上可被中间人读取——旧套件问题用 ``SECLEVEL=1`` +
  ``OP_LEGACY_SERVER_CONNECT`` 就能解决，不需要放弃校验。
- 只有确实遇到自签/过期证书的极端场景，才用环境变量
  ``CN_CARRIER_INSECURE_TLS=1`` 显式降级（会在日志里持续告警）。
"""

from __future__ import annotations

import logging
import os
import socket
import ssl

import aiohttp

_LOGGER = logging.getLogger(__name__)

# 运营商网关仍在用 1024 位 DH / SHA1 等旧套件，默认 SECLEVEL=2 会握手失败
_LEGACY_CIPHERS = "DEFAULT:@SECLEVEL=1"
# 公共 DNS：HAOS 内置 coredns 对运营商域名解析偶发失败
_DNS_SERVERS = ["223.5.5.5", "119.29.29.29"]

_INSECURE_ENV = "CN_CARRIER_INSECURE_TLS"


def _insecure_allowed() -> bool:
    """是否允许关闭证书校验（默认否，需显式设置环境变量）。"""
    return os.environ.get(_INSECURE_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


_SSL_CTX_CACHE: dict[bool, ssl.SSLContext] = {}


def _build_ssl_context(insecure: bool) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = not insecure
    ctx.verify_mode = ssl.CERT_NONE if insecure else ssl.CERT_REQUIRED
    if not insecure:
        # load_default_certs() 读 CA 目录属阻塞调用；由 warm_ssl_context()
        # 在 executor 里预热，避免 HA 的 blocking-call 检测告警。
        ctx.load_default_certs()
    ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
    try:
        ctx.set_ciphers(_LEGACY_CIPHERS)
    except ssl.SSLError:
        _LOGGER.debug("当前 OpenSSL 不支持 %s，沿用默认套件", _LEGACY_CIPHERS)
    return ctx


def _ssl_context(insecure: bool) -> ssl.SSLContext:
    cached = _SSL_CTX_CACHE.get(insecure)
    if cached is not None:
        return cached
    ctx = _build_ssl_context(insecure)
    _SSL_CTX_CACHE[insecure] = ctx
    return ctx


def warm_ssl_context() -> None:
    """在 executor 中预热 SSL 上下文（集成 async_setup 调用一次）。

    不做预热的话，首次建连会在事件循环里执行 ``load_default_certs()``，
    HA 会报 "Detected blocking call inside the event loop"。
    """
    insecure = _insecure_allowed()
    if insecure:
        _LOGGER.warning(
            "已通过 %s 关闭运营商端点证书校验：会话可被中间人读取，仅限排障临时使用",
            _INSECURE_ENV,
        )
    try:
        _ssl_context(insecure)
    except Exception as err:  # noqa: BLE001 - 预热失败时退回 aiohttp 默认上下文
        _LOGGER.warning("SSL 上下文预热失败，回退 aiohttp 默认配置: %s", err)


def _resolver():
    """优先用公共 DNS 解析；aiodns 缺失时返回 None（用系统解析）。

    早期版本 ``except Exception: pass`` 把失败吞掉，导致「换公共 DNS」这个
    修复在多数 HAOS 环境里其实没生效。现在至少留一条 debug 线索。
    """
    try:
        import aiodns  # noqa: F401 - 仅探测是否可用
    except ImportError:
        _LOGGER.debug("未安装 aiodns，DNS 走系统解析（不影响功能）")
        return None
    try:
        from aiohttp.resolver import DefaultResolver

        return DefaultResolver(nameservers=list(_DNS_SERVERS))
    except Exception as err:  # noqa: BLE001 - 解析器属可选项
        _LOGGER.debug("自定义 DNS 解析器不可用，回退系统解析: %s", err)
        return None


def build_connector(legacy_tls: bool = True) -> aiohttp.TCPConnector:
    """AF_INET + 公共 DNS + 可选 legacy TLS 套件（证书校验保持开启）。"""
    kwargs: dict = {"family": socket.AF_INET}
    if legacy_tls:
        kwargs["ssl"] = _ssl_context(_insecure_allowed())
    resolver = _resolver()
    if resolver is not None:
        kwargs["resolver"] = resolver
    return aiohttp.TCPConnector(**kwargs)
