"""共享网络工具：稳定 DNS / 运营商 legacy TLS 的连接器。"""

import aiohttp

_SSL_CTX_CACHE: dict[bool, object] = {}


def _ssl_context(legacy: bool):
    """移动 wx.10086.cn 要求旧套件（SECLEVEL=1）+ 允许重协商。"""
    if not legacy:
        return True
    if legacy in _SSL_CTX_CACHE:
        return _SSL_CTX_CACHE[legacy]
    import ssl

    # 不用 create_default_context()（内部 load_default_certs 是阻塞调用，
    # HA 会告警"Detected blocking call"）；改为手动构建最小上下文
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # 运营商端点使用自签/老证书，不做 MITM 防护
    ctx.options |= 0x4  # OP_LEGACY_SERVER_CONNECT
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    except Exception:
        pass
    _SSL_CTX_CACHE[legacy] = ctx
    return ctx


def build_connector(legacy_tls: bool = True) -> aiohttp.TCPConnector:
    """AF_INET + 公共 DNS（HAOS coredns 对运营商域名解析不稳）+ 可选 legacy TLS。"""
    import socket as _socket

    kwargs: dict = {"family": _socket.AF_INET, "ssl": _ssl_context(legacy_tls)}
    try:
        from aiohttp.resolver import AsyncResolver

        kwargs["resolver"] = AsyncResolver(nameservers=["223.5.5.5", "119.29.29.29"])
    except Exception:  # aiodns 缺失时回退系统解析
        pass
    return aiohttp.TCPConnector(**kwargs)
