"""响应结构签名金丝雀（drift canary v2）。

与 canary_endpoints.py 的"端点存活"互补：存活只证明端口通，
结构签名才能发现"响应换了形态/换了结构"这类真正的协议漂移。

对每个目标：
1. 断言基础行为（如联通 WT 未认证响应体含 999999、移动登录页 200）；
2. 计算结构指纹 —— 把响应归一化为"路径集合"后取 sha256：
   - JSON 响应 → 递归键路径（列表只展开前 3 个元素，叶子不带值）；
   - HTML 响应 → 标签名 + 表单控件 name/type + id 骨架（不带易变的
     文本/属性值/脚本哈希文件名，避免每次部署都误报）；
   - 纯文本   → 形态归一（6 位数字 → numeric6 等）。
3. 与 tests/baselines/<name>.json 对比；缺失或偏差 → 退出码 1
   （CI 失败 → 自动开 [drift] issue）。

重新采集基线：python tests/canary_signature.py --bless
（基线含 captured_from 备注，换网络出口采集后如大面积偏差需人工复核）
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings()

BASELINES = Path(__file__).resolve().parent / "baselines"

S = requests.Session()
S.verify = False
S.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0")

TARGETS = [
    {
        "name": "unicom_wt_package",
        "method": "POST",
        "url": ("https://m.client.10010.com/servicequerybusiness/operationservice/"
                "queryOcsPackageFlowLeftContentRevisedInJune"),
        "kwargs": {"data": {"version": "WT"},
                   "headers": {"User-Agent": "Mozilla/5.0"},
                   "timeout": 20},
        "assert_text": "999999",  # 未认证响应体（端点存活 + 结构双重断言）
    },
    {
        "name": "mobile_login_page",
        "method": "GET",
        "url": "https://login.10086.cn/login.html?channelID=12003",
        "kwargs": {"timeout": 20},
        "assert_status": 200,
    },
]


def json_paths(obj, prefix="$"):
    paths = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            paths |= json_paths(value, f"{prefix}.{key}")
    elif isinstance(obj, list):
        paths.add(prefix + "[]")
        for item in obj[:3]:
            paths |= json_paths(item, prefix + "[]")
    else:
        paths.add(prefix)
    return paths


_TAG_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9-]*)")
_CTRL_RE = re.compile(r"<(?:input|select|textarea|button)\b[^>]*?>", re.I | re.S)
_ATTR_RE = re.compile(r'\b(?:name|type|id)\s*=\s*["\']([^"\']+)["\']', re.I)


def html_skeleton(body: str) -> set[str]:
    skeleton = {"tag:" + tag.lower() for tag in _TAG_RE.findall(body)}
    for ctrl in _CTRL_RE.findall(body):
        skeleton |= {"ctrl:" + v.lower() for v in _ATTR_RE.findall(ctrl)}
    return skeleton or {"empty"}


def normalize_text(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return "empty"
    if re.fullmatch(r"\d{6}", stripped):
        return "numeric6"
    if re.fullmatch(r"\d+", stripped):
        return "numeric"
    return "text:" + re.sub(r"\s+", " ", stripped)[:64]


def fingerprint(body: str, content_type: str) -> dict:
    stripped = body.strip()
    if stripped[:1] in ("{", "["):
        try:
            return {"kind": "json",
                    "paths": sorted(json_paths(json.loads(stripped)))}
        except ValueError:
            pass
    if "<html" in body.lower() or "<!doctype html" in body.lower() \
            or "html" in (content_type or "").lower():
        return {"kind": "html", "paths": sorted(html_skeleton(body))}
    return {"kind": "text", "paths": [f"text:{normalize_text(stripped)}"]}


def signature(fp: dict) -> str:
    return hashlib.sha256(
        json.dumps(fp, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def fetch(target: dict) -> tuple[bool, str, dict | None]:
    try:
        resp = S.request(target["method"], target["url"], **target["kwargs"])
    except Exception as err:  # noqa: BLE001
        return False, f"请求失败: {str(err)[:120]}", None
    detail = f"HTTP {resp.status_code} len={len(resp.text)}"
    if "assert_status" in target and resp.status_code != target["assert_status"]:
        return False, f"状态码不符 {detail}", None
    if "assert_text" in target and target["assert_text"] not in resp.text[:200]:
        return False, f"响应体缺少 {target['assert_text']} {detail}", None
    return True, detail, fingerprint(resp.text, resp.headers.get("Content-Type", ""))


def main() -> int:
    bless = "--bless" in sys.argv
    BASELINES.mkdir(parents=True, exist_ok=True)
    failed = 0
    for target in TARGETS:
        name = target["name"]
        ok, detail, fp = fetch(target)
        if not ok:
            print(f"FAIL {name}: {detail}")
            failed += 1
            continue
        current_sig = signature(fp)
        baseline_file = BASELINES / f"{name}.json"
        if bless:
            baseline_file.write_text(json.dumps({
                "name": name,
                "url": target["url"],
                "fingerprint": fp,
                "signature": current_sig,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "captured_from": "local-CN",  # 换网络出口重采需人工复核
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"BLESS {name}: {current_sig[:16]}… kind={fp['kind']} paths={len(fp['paths'])}")
            continue
        if not baseline_file.exists():
            print(f"FAIL {name}: 基线缺失，请本地运行 canary_signature.py --bless 采集")
            failed += 1
            continue
        baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
        if baseline.get("signature") != current_sig:
            print(f"FAIL {name}: 结构签名与基线不一致 {detail}")
            print(f"  baseline kind={baseline['fingerprint']['kind']} sig={baseline['signature'][:16]}…")
            print(f"  current  kind={fp['kind']} sig={current_sig[:16]}…")
            removed = sorted(set(baseline["fingerprint"]["paths"]) - set(fp["paths"]))
            added = sorted(set(fp["paths"]) - set(baseline["fingerprint"]["paths"]))
            if removed:
                print(f"  消失的结构: {removed[:20]}")
            if added:
                print(f"  新增的结构: {added[:20]}")
            failed += 1
        else:
            print(f"PASS {name}: 签名一致 kind={fp['kind']} paths={len(fp['paths'])}")
    print(f"\n{'基线重新采集完成' if bless else f'{len(TARGETS) - failed}/{len(TARGETS)} 签名一致'}")
    return 0 if bless else (1 if failed else 0)


if __name__ == "__main__":
    sys.exit(main())
