"""共享网络工具：稳定 DNS 的连接器。"""

import aiohttp


def build_connector() -> aiohttp.TCPConnector:
    """AF_INET + 公共 DNS（HAOS 内置 coredns 对运营商域名解析不稳定）。"""
    import socket as _socket

    try:
        from aiohttp.resolver import AsyncResolver

        resolver = AsyncResolver(nameservers=["223.5.5.5", "119.29.29.29"])
        return aiohttp.TCPConnector(family=_socket.AF_INET, resolver=resolver, ssl=True)
    except Exception:  # aiodns 缺失时回退
        return aiohttp.TCPConnector(family=_socket.AF_INET, ssl=True)
