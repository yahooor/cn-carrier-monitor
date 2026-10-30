"""event_log：失败四分类、JSONL 落盘与轮转；附 wx 解析漂移抛错回归。"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from conftest import CCM_ROOT


@pytest.fixture()
def event_log(ccm):
    spec = importlib.util.spec_from_file_location("ccm.event_log", CCM_ROOT / "event_log.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ccm.event_log"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_classify_four_categories(event_log, ccm):
    base = ccm.modules["base"]
    assert event_log.classify_exception(base.CarrierAuthError("session expired")) == "auth_expired"
    assert event_log.classify_exception(base.ParseAnomalyError("结构未知")) == "protocol_drift"
    # _request 封装的两种网络文案
    assert event_log.classify_exception(base.CarrierError("网络错误: boom")) == "network"
    assert event_log.classify_exception(base.CarrierError("请求超时: url")) == "network"
    # 其余 CarrierError / 底层异常
    assert event_log.classify_exception(base.CarrierError("移动登录失败")) == "internal"
    assert event_log.classify_exception(TimeoutError()) == "network"
    assert event_log.classify_exception(ConnectionRefusedError()) == "network"
    assert event_log.classify_exception(ValueError("x")) == "internal"
    # aiohttp 异常按宿主模块名识别（测试环境无需安装 aiohttp）
    aiohttp_err = type("ClientError", (Exception,), {"__module__": "aiohttp.client_exceptions"})()
    assert event_log.classify_exception(aiohttp_err) == "network"


class _FakeHass:
    def __init__(self, root: Path):
        self._root = root

        class _Config:
            def path(self, name: str) -> str:
                return str(root / name)

        self.config = _Config()

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def test_emit_appends_and_rotates(event_log, tmp_path):
    hass = _FakeHass(tmp_path)
    out = tmp_path / event_log.FILE_NAME
    asyncio.run(event_log.emit(hass, "update_success", {"carrier": "mobile", "fields": ["balance"]}))
    rec = json.loads(out.read_text(encoding="utf-8").strip())
    assert rec["event"] == "update_success"
    assert rec["carrier"] == "mobile"
    assert rec["fields"] == ["balance"]
    # 超过 1MB 触发轮转：旧内容进 .1，新文件重新从一行开始
    out.write_text("x" * event_log.MAX_BYTES, encoding="utf-8")
    asyncio.run(event_log.emit(hass, "update_failure", {"category": "network"}))
    rotated = tmp_path / (event_log.FILE_NAME + ".1")
    assert rotated.read_text(encoding="utf-8") == "x" * event_log.MAX_BYTES
    assert len(out.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_wx_query_raises_parse_anomaly_on_garbage(event_log, ccm):
    """回归：wx 端点返回非 JSON（SPA/下线页）→ protocol_drift，而不是静默跳过。"""
    mobile = ccm.modules["mobile"]
    base = ccm.modules["base"]
    client = mobile.MobileClient.__new__(mobile.MobileClient)
    client._data = {"phone": "17267445980"}
    client._cookie = ""

    async def _fake_request(*_args, **_kwargs):
        return 200, "<html><body>SPA page</body></html>"

    async def _no_cffi(*_args, **_kwargs):
        return None

    client._request = _fake_request
    client._cffi_wx = _no_cffi
    with pytest.raises(base.ParseAnomalyError):
        asyncio.run(client._wx_query("https://wx.10086.cn/website/fareBalance"))
    # 解密成功但全空 / 未知顶层结构同样算漂移
    client._request = lambda *_a, **_k: _empty(200, "{}")
    with pytest.raises(base.ParseAnomalyError):
        asyncio.run(client._wx_query("https://wx.10086.cn/website/fareBalance"))
    client._request = lambda *_a, **_k: _empty(200, '{"retCode":"999","msg":"busy"}')
    with pytest.raises(base.ParseAnomalyError):
        asyncio.run(client._wx_query("https://wx.10086.cn/website/fareBalance"))


def _empty(status, body):
    async def _inner(*_args, **_kwargs):
        return status, body
    return _inner()
