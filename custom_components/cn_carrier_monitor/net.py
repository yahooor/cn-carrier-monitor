"""共享网络工具：稳定 DNS / 运营商 legacy TLS 的连接器。"""

import aiohttp


def _ssl_context(legacy: bool):
    """移动 wx.10086.cn 要求旧套件（SECLEVEL=1）+ 允许重协商。"""
    if not legacy:
        return True
    import ssl

    ctx = ssl.create_default_context()
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        ctx.options |= 0x4  # OP_LEGACY_SERVER_CONNECT
    except Exception:
        pass
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
