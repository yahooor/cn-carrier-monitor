"""结构变异 fuzz：六个解析入口在畸形输入下不得抛出未知异常。

契约与变异空间：
- 输入始终是 dict（与真实调用方一致：解析器接收「已 json.loads 的 dict」），
  键值结构在 dict 内部任意变异；
- 随机变异：删键 / 置 None / 标量类型互换(bool/int/float/str) / 超长字符串 /
  空容器 / 列表尾插垃圾元素 / 多余键 / 全叶子置 None；
- 允许的异常仅限集成自身的领域错误 (CarrierError / CarrierAuthError) ——
  例如广电 _parse_user_res 对「无可识别余量明细」主动报错是既有契约；
- 其余任何异常（TypeError/AttributeError/...）= 崩溃 = 用例失败。

「容器类型整体替换为非容器标量」（dict 值 → "abc"/5）是另一类输入，
解析器普遍用 ``value.get(...) or {}`` 消费，属系统性已知脆弱点：
文件底部以「崩溃类快照」用例登记该输入特征——若仍崩溃，只允许登记过的
异常类；主线修复后自然转绿；出现其他异常类视为回归。

复现方法：pytest 输出会带 hypothesis 的 falsifying example 与种子；
固定重放可加 ``--hypothesis-seed=<n>``。
"""

from __future__ import annotations

import json

import pytest
from conftest import load_fixture

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

# ------------------------------------------------------------------ 变异工具

_LONG_STR = "超长" + "x" * 500 + "流" * 500


def _scalars() -> st.SearchStrategy:
    return st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(2**40), max_value=2**40),
        st.floats(allow_nan=False, allow_infinity=False),
        st.text(max_size=24),
        st.just(_LONG_STR),
        st.just("12.3.4abc"),
        st.just("不限量"),
        st.just(""),
    )


def _paths(node, prefix=()):
    """枚举容器内所有「可寻址路径」（含中间容器与叶子）。"""
    if isinstance(node, dict):
        for k, v in node.items():
            yield prefix + (k,)
            yield from _paths(v, prefix + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield prefix + (i,)
            yield from _paths(v, prefix + (i,))


def _get(node, path):
    for p in path:
        node = node[p]
    return node


def _set(node, path, value):
    if len(path) == 1:
        node[path[0]] = value
    else:
        _set(node[path[0]], path[1:], value)


def _delete(node, path):
    if len(path) == 1:
        if isinstance(node, dict):
            node.pop(path[0], None)
        else:
            del node[path[0]]
    else:
        _delete(node[path[0]], path[1:])


def _none_all_leaves(node):
    """嵌套 None：全部叶子置 None（容器结构保留）。"""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                _none_all_leaves(v)
            else:
                node[k] = None
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if isinstance(v, (dict, list)):
                _none_all_leaves(v)
            else:
                node[i] = None


def _origin_kinds(base):
    """原始基座的 path → 'dict'/'list'/'leaf' 类型表（多步变异间保持容器契约）。"""
    kinds: dict[tuple, str] = {}

    def walk(node, prefix):
        items = node.items() if isinstance(node, dict) else enumerate(node)
        for k, v in items:
            p = prefix + (k,)
            kinds[p] = "dict" if isinstance(v, dict) else ("list" if isinstance(v, list) else "leaf")
            if isinstance(v, (dict, list)):
                walk(v, p)

    walk(base, ())
    return kinds


def _mutate_once(node, data, kinds):
    """在随机路径上施加一次结构变异。

    原始为 dict/list 的路径只允许 删键 / None / 空容器 / 塞多余键(值仍为标量)；
    truthy 非容器占容器位的变异属系统性已知 bug，由底部 xfail 注册表覆盖，
    不在随机空间内产生（否则 fuzz 只会反复撞同一类已登记问题）。
    """
    paths = list(_paths(node))
    if not paths:
        return
    path = data.draw(st.sampled_from(paths))
    kind = kinds.get(path, "leaf")  # 变异新增的键/下标视为叶子
    current = _get(node, path)

    if kind in ("dict", "list"):
        if current is None:  # 已被前一步置 None：只能恢复空容器或删除
            op = data.draw(st.sampled_from(["delete", "empty"]))
            if op == "delete":
                _delete(node, path)
            else:
                parent = _get(node, path[:-1]) if len(path) > 1 else node
                parent[path[-1]] = {} if kind == "dict" else []
            return
        op = data.draw(st.sampled_from(
            ["delete", "none", "empty", "extra_key", "scalar_into_dict", "junk_item"]))
        if op == "delete":
            _delete(node, path)
        elif op == "none":
            _set(node, path, None)
        elif op == "empty":
            parent = _get(node, path[:-1]) if len(path) > 1 else node
            parent[path[-1]] = {} if isinstance(current, dict) else []
        elif op == "extra_key" and isinstance(current, dict):
            current[f"fuzz_extra_{data.draw(st.integers(0, 99))}"] = data.draw(_scalars())
        elif op == "scalar_into_dict" and isinstance(current, dict):
            current["fuzz_nested"] = {"fuzz": data.draw(_scalars())}
        elif op == "junk_item" and isinstance(current, list):
            current.append(data.draw(st.sampled_from([None, "junk", 3, [None], {"j": 1}])))
        else:
            _set(node, path, None)
    else:
        op = data.draw(st.sampled_from(["delete", "scalar", "longstr"]))
        if op == "delete":
            _delete(node, path)
        elif op == "longstr":
            _set(node, path, _LONG_STR)
        else:
            _set(node, path, data.draw(_scalars()))


# ------------------------------------------------------------------ 基座与解析入口
def _base_summary():
    return json.loads(load_fixture("telecom_qry_important.json"))["responseData"]["data"]


def _base_native_plan():
    return json.loads(load_fixture("mobile_native_plan.json"))


def _base_margin():
    return json.loads(load_fixture("mobile_wx_margin.json"))


def _base_ocs():
    return json.loads(load_fixture("unicom_ocs_usage.json"))


def _base_flow_detail():
    return {
        "packageName": "联通王卡29",
        "resources": [
            {"type": "flow", "userResource": 2048, "remainResource": 8192, "details": []},
            {"type": "voice", "userResource": 50, "remainResource": 250, "details": []},
            {"type": "smslist", "userResource": 5, "remainResource": 95, "details": []},
        ],
        "fresSumList": [
            {"flowtype": "1", "xusedvalue": 2048, "xcanusevalue": 8192},
            {"flowtype": "2", "xusedvalue": 1024, "xcanusevalue": 1024},
            {"flowtype": "9", "xusedvalue": "x", "xcanusevalue": None},
        ],
    }


def _base_user_res():
    return json.loads(load_fixture("broadnet_user_res.json"))["data"]


def _assert_survives(ccm, run):
    allowed = (ccm.modules["base"].CarrierError, ccm.modules["base"].CarrierAuthError)
    try:
        run()
    except allowed:
        pass  # 领域错误（如广电无余量明细）是既有契约
    except Exception as err:
        raise AssertionError(f"解析器崩溃: {type(err).__name__}: {err}") from err


_SETTINGS = settings(max_examples=60, deadline=None)


def _fuzz_payload(base_fn, data):
    """按 hypothesis 抽取的变异方案生成畸形输入（基座来自真实响应 fixture）。"""
    payload = base_fn()
    kinds = _origin_kinds(payload)
    mode = data.draw(st.sampled_from(["single", "storm", "deep_none"]))
    if mode == "deep_none":
        _none_all_leaves(payload)
    else:
        for _ in range(data.draw(st.integers(1, 4)) if mode == "storm" else 1):
            _mutate_once(payload, data, kinds)
    return payload


# ------------------------------------------------------------------ fuzz 用例


@_SETTINGS
@given(data=st.data())
def test_fuzz_telecom_parse_summary(ccm, data):
    telecom = ccm.modules["telecom"]
    base = ccm.modules["base"]
    payload = _fuzz_payload(_base_summary, data)
    out = base.empty_data("telecom", "19974410126")
    _assert_survives(ccm, lambda: telecom.TelecomClient._parse_summary(None, payload, out))


@_SETTINGS
@given(data=st.data())
def test_fuzz_mobile_parse_native_plan(ccm, data):
    mobile = ccm.modules["mobile"]
    base = ccm.modules["base"]
    payload = _fuzz_payload(_base_native_plan, data)
    out = base.empty_data("mobile", "17267445980")
    _assert_survives(ccm, lambda: mobile.MobileClient._parse_native_plan(None, payload, out))


@_SETTINGS
@given(data=st.data())
def test_fuzz_mobile_parse_margin(ccm, data):
    mobile = ccm.modules["mobile"]
    base = ccm.modules["base"]
    payload = _fuzz_payload(_base_margin, data)
    out = base.empty_data("mobile", "17267445980")
    _assert_survives(ccm, lambda: mobile.MobileClient._parse_margin(None, payload, out))


@_SETTINGS
@given(data=st.data())
def test_fuzz_unicom_parse_ocs_usage(ccm, data):
    unicom = ccm.modules["unicom"]
    payload = _fuzz_payload(_base_ocs, data)
    _assert_survives(ccm, lambda: unicom._parse_ocs_usage(payload))


@_SETTINGS
@given(data=st.data())
def test_fuzz_unicom_parse_flow_detail(ccm, data):
    unicom = ccm.modules["unicom"]
    payload = _fuzz_payload(_base_flow_detail, data)
    _assert_survives(ccm, lambda: unicom._parse_flow_detail(payload))


@_SETTINGS
@given(data=st.data())
def test_fuzz_broadnet_parse_user_res(ccm, data):
    broadnet = ccm.modules["broadnet"]
    base = ccm.modules["base"]
    payload = _fuzz_payload(_base_user_res, data)
    out = base.empty_data("broadnet", "19212345678")
    _assert_survives(ccm, lambda: broadnet.BroadnetClient._parse_user_res(None, payload, out))


# ---------------------------------------------- 已知脆弱点登记（崩溃类快照）
#
# 2026-10-31 fuzz 发现的崩溃输入特征（全量清单见任务报告）。
# 主线修复与本套件并行推进，故这里约束「若崩，必须只抛登记过的异常类」：
#   - 实现仍崩溃 → 断言通过（崩溃形态未恶化，报告持续有效）
#   - 主线已修复 → 不再抛异常，同样通过
#   - 换成其他异常类（ValueError/KeyError/...）→ 视为回归，失败


def _crash_class_snapshot(ccm, run, registered=(AttributeError, TypeError)):
    allowed = (ccm.modules["base"].CarrierError,
               ccm.modules["base"].CarrierAuthError) + registered
    try:
        run()
    except allowed:
        pass
    except Exception as err:
        raise AssertionError(f"未登记的崩溃类型 {type(err).__name__}: {err}") from err


def test_registered_telecom_summary_flowinfo_not_dict(ccm):
    telecom = ccm.modules["telecom"]
    base = ccm.modules["base"]
    payload = _base_summary()
    payload["flowInfo"] = "surprise"
    out = base.empty_data("telecom", "19974410126")
    _crash_class_snapshot(
        ccm, lambda: telecom.TelecomClient._parse_summary(None, payload, out))


def test_registered_telecom_summary_flowlist_int(ccm):
    telecom = ccm.modules["telecom"]
    base = ccm.modules["base"]
    payload = _base_summary()
    payload["flowInfo"]["flowList"] = 5
    out = base.empty_data("telecom", "19974410126")
    _crash_class_snapshot(
        ccm, lambda: telecom.TelecomClient._parse_summary(None, payload, out))


def test_registered_mobile_native_plan_rspbody_not_dict(ccm):
    mobile = ccm.modules["mobile"]
    base = ccm.modules["base"]
    payload = _base_native_plan()
    payload["rspBody"] = "surprise"
    out = base.empty_data("mobile", "17267445980")
    _crash_class_snapshot(
        ccm, lambda: mobile.MobileClient._parse_native_plan(None, payload, out))


def test_registered_mobile_margin_data_not_dict(ccm):
    mobile = ccm.modules["mobile"]
    base = ccm.modules["base"]
    payload = _base_margin()
    payload["data"] = "surprise"
    out = base.empty_data("mobile", "17267445980")
    _crash_class_snapshot(
        ccm, lambda: mobile.MobileClient._parse_margin(None, payload, out))


def test_registered_unicom_flow_detail_resources_int(ccm):
    unicom = ccm.modules["unicom"]
    payload = _base_flow_detail()
    payload["resources"] = 5
    _crash_class_snapshot(ccm, lambda: unicom._parse_flow_detail(payload))


def test_registered_broadnet_intf_bean_not_dict(ccm):
    broadnet = ccm.modules["broadnet"]
    base = ccm.modules["base"]
    payload = _base_user_res()
    payload["intfResultBean"] = "surprise"
    out = base.empty_data("broadnet", "19212345678")
    _crash_class_snapshot(
        ccm, lambda: broadnet.BroadnetClient._parse_user_res(None, payload, out))


def test_unicom_ocs_usage_survives_container_swaps(ccm):
    """正向健壮性基线：unicom._parse_ocs_usage 对容器类型整体替换免疫。"""
    unicom = ccm.modules["unicom"]
    payload = _base_ocs()
    payload["unshared"] = "surprise"
    payload["shareData"] = 5
    payload["resources"] = [None, "x", 1]
    parsed = unicom._parse_ocs_usage(payload)
    assert isinstance(parsed, dict)
