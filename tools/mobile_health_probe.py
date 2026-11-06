#!/usr/bin/env python
"""移动网厅健康探针 —— 用于判断「维护中 / 端点已变更 / 会话失效 / 正常」。

用法:
    python mobile_health_probe.py            # 用当前 mobile_cookie.txt + 代理探测
    python mobile_health_probe.py --no-proxy # 直连探测

判别规则（基于 2026-11-07 实测）：
  - 响应体含「升级公告/系统升级/升级期间/系统优化升级/维护」 → 运营商维护中（等）
  - 全部端点 404 且非公告页 → 端点已下线/迁移（需改代码）
  - 401/403 或响应含「请登录/未登录/login.htm」 → 会话失效（需重粘 cookie）
  - 200 且可解密 → 正常
"""
import argparse
import html
import re
import sys

import curl_cffi.requests as cr

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_PROXY = "http://Clash:AkEGjd42@192.168.31.123:7890"

MAINTENANCE_MARKS = (
    "升级公告", "系统升级", "升级期间", "系统优化升级",
    "维护", "暂无法提供服务",
)
LOGIN_MARKS = ("请登录", "未登录", "login.htm", "notLogin")

ENDPOINTS = [
    ("GET", "https://wx.10086.cn/website/spa/main/newHome", None),
    ("GET", "https://wx.10086.cn/website/serviceMargin/getMainPlan", None),
    ("GET", "https://wx.10086.cn/website/fareBalance", None),
    ("POST", "https://wx.10086.cn/website/uniApi/getNewMarginInfo",
     {"operation": "getCustomerKeyMessage"}),
    ("GET", "https://wx.10086.cn/website/score/news", None),
    ("POST", "https://wx.10086.cn/website/uniApi/getMarginQueryInfo",
     {"operation": "getCustomerKeyMessage"}),
]


def cookie_dict(path="mobile_cookie.txt"):
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read().strip()
    except OSError:
        return out
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            if k:
                out[k] = v
    return out


def strip_html(text: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def classify(status: int, body: str) -> str:
    plain = strip_html(body)
    if any(m in plain or m in body for m in MAINTENANCE_MARKS):
        return "maintenance"
    if status in (401, 403) or any(m in plain for m in LOGIN_MARKS):
        return "auth_expired"
    if status == 200:
        return "ok"
    return "unknown"


def probe(use_proxy: bool, cookies: dict) -> dict:
    proxies = (
        {"http": DEFAULT_PROXY, "https": DEFAULT_PROXY} if use_proxy else None
    )
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://wx.10086.cn/website/spa/main/index.html",
    }
    results = []
    for method, url, body in ENDPOINTS:
        try:
            kw = {"headers": headers, "cookies": cookies, "impersonate": "chrome",
                  "timeout": 25, "proxies": proxies}
            if method == "GET":
                r = cr.get(url, **kw)
            else:
                r = cr.post(url, json=body, **kw)
            kind = classify(r.status_code, r.text)
            title = re.search(r"<title>(.*?)</title>", r.text, re.S)
            results.append({
                "method": method, "url": url, "status": r.status_code,
                "kind": kind,
                "title": (title.group(1).strip() if title else ""),
                "len": len(r.text),
            })
        except Exception as err:  # noqa: BLE001
            results.append({"method": method, "url": url, "status": 0,
                            "kind": "transport", "title": type(err).__name__,
                            "len": 0})
    return {"results": results}


def summarize(out: dict) -> str:
    rows = out["results"]
    kinds = [r["kind"] for r in rows]
    maint = kinds.count("maintenance")
    auth = kinds.count("auth_expired")
    ok = kinds.count("ok")
    print(f"{'方法':<5}{'状态':<7}{'判定':<14}{'标题':<22}端点")
    for r in rows:
        print(f"{r['method']:<6}{r['status']:<8}{r['kind']:<15}"
              f"{r['title'][:20]:<23}{r['url'].split('/website/')[-1]}")
    print()
    if maint == len(rows):
        verdict = ("运营商维护中：整站返回「升级公告」。凭据有效，"
                   "集成侧应识别为 maintenance 并退避，无需重登。")
    elif maint >= 1:
        verdict = (f"部分端点返回维护公告（{maint}/{len(rows)}）："
                   "运营商升级中，升级结束后需复核端点路径是否变更。")
    elif auth == len(rows):
        verdict = "会话失效：cookie 已过期，需重新登录（重粘 cookie）。"
    elif ok:
        verdict = f"正常：{ok}/{len(rows)} 个端点 200。"
    else:
        verdict = "异常：无端点正常，且未见明确特征，需人工排查。"
    return verdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--cookie", default="mobile_cookie.txt")
    args = ap.parse_args()

    cookies = cookie_dict(args.cookie)
    if not cookies:
        print("!! 未找到 cookie 文件，探测结果仅供参考（无 cookie 会被导向登录页）")
    out = probe(not args.no_proxy, cookies)
    verdict = summarize(out)
    print()
    print("结论:", verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
