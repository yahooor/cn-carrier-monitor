"""Local mock carrier HTTP server for full-chain replay tests.

提供绑定在 127.0.0.1 随机端口上的 aiohttp 测试服务器：

- ``register(path, responder, method=...)`` 按「运营商场景」注册路由 → 响应；
- responder 可以是静态值（str / dict / (status, body) / (status, body, headers)）、
  可调用对象 ``f(record) -> 上述任意``、或直接返回 ``web.Response``；
- dict/list 自动以 application/json 返回，str 原样返回（便于 hex 密文 / JSONP / "999999"）；
- 每个收到的请求（方法/路径/查询串/头/体）记录进 ``server.requests`` 供断言。

用例里把运营商客户端的模块级 URL 常量（或 request_url）指到
``server.base_url`` 即可在本地跑完整 登录→查询→解析 链路。
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

import aiohttp
import pytest
from aiohttp import web


class RecordedRequest:
    """One request the mock server received (headers lower-cased)."""

    def __init__(self, method: str, path: str, query: dict, headers: dict, body: str) -> None:
        self.method = method
        self.path = path
        self.query = query
        self.headers = {str(k).lower(): str(v) for k, v in headers.items()}
        self.body = body

    def json_dict(self) -> Any:
        """Parsed JSON body or None."""
        try:
            return json.loads(self.body)
        except ValueError:
            return None

    def form_dict(self) -> dict[str, list[str]]:
        """Form-encoded body as parse_qs mapping (blank values kept)."""
        return urllib.parse.parse_qs(self.body, keep_blank_values=True)


def _as_response(value: Any, rec: RecordedRequest) -> web.Response:
    if callable(value):
        value = value(rec)
    if isinstance(value, web.Response):
        return value
    status, headers = 200, {}
    if isinstance(value, tuple):
        if len(value) == 3:
            status, value, headers = value
        else:
            status, value = value
    if isinstance(value, (dict, list)):
        return web.json_response(value, status=status, headers=headers)
    if isinstance(value, bytes):
        return web.Response(body=value, status=status, headers=headers)
    if value is None:
        return web.Response(status=status)
    return web.Response(text=str(value), status=status, headers=headers)


class MockCarrierServer:
    """Catch-all aiohttp app with scenario routes + request journal."""

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self._routes: list[tuple[str, str, Any]] = []
        self.port = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def register(self, path: str, responder: Any = None, *, method: str = "*") -> None:
        """Register a scenario route; the first match serves the request."""
        self._routes.append((method.upper(), path, responder))

    def register_json(self, path: str, payload: Any, *, method: str = "*", status: int = 200) -> None:
        self.register(path, (status, payload), method=method)

    def reset_requests(self) -> None:
        self.requests.clear()

    def calls(self, path: str | None = None, method: str | None = None) -> list[RecordedRequest]:
        return [
            r
            for r in self.requests
            if (path is None or r.path == path) and (method is None or r.method == method)
        ]

    async def _handle(self, request: web.Request) -> web.Response:
        body = await request.text()
        rec = RecordedRequest(
            request.method, request.path, dict(request.query), dict(request.headers), body
        )
        self.requests.append(rec)
        for method, path, responder in self._routes:
            if method not in ("*", request.method.upper()):
                continue
            if path != request.path:
                continue
            return _as_response(responder, rec)
        return web.json_response(
            {"mock_error": f"no route for {request.method} {request.path}"}, status=404
        )


@pytest.fixture
async def mock_carrier():
    """Mock carrier server bound to 127.0.0.1 with a random port."""
    app = web.Application()
    server = MockCarrierServer()
    app.router.add_route("*", "/{tail:.*}", server._handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    server.port = runner.addresses[0][1]
    try:
        yield server
    finally:
        await runner.cleanup()


@pytest.fixture
async def client_session():
    """One aiohttp client session shared by the clients under test."""
    async with aiohttp.ClientSession() as session:
        yield session
