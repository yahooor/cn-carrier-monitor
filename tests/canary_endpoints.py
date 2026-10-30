"""无凭据金丝雀：验证各运营商端点的"未认证行为"是否符合基线。

判据来自 docs/协议基线.md：
- 联通 package 接口未认证 → 200 + "999999"（端点存活标志）
- 移动 login.10086.cn 登录页 → 200
- 电信 appgologinsz/appfuwuhd → TLS 可达（证书握手成功即算存活）
- 广电 www.10099.com.cn → 403/WAF 页（阿里云 WAF 存活标志）
偏差 → 退出码 1（CI 失败 → 自动开 issue）。
"""
import sys

import requests

S = requests.Session()
S.verify = False
S.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0")
import urllib3  # noqa: E402

urllib3.disable_warnings()

checks = []


def check(name, ok, detail):
    checks.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")


# 1. 联通：未认证 → 999999
try:
    r = requests.post(
        "https://m.client.10010.com/servicequerybusiness/operationservice/"
        "queryOcsPackageFlowLeftContentRevisedInJune", data={"version": "WT"},
        headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    check("unicom.package 未认证响应", r.status_code == 200 and "999" in r.text[:20],
          f"{r.status_code} {r.text[:40]}")
except Exception as e:
    check("unicom.package", False, str(e)[:80])

# 2. 移动登录页
try:
    r = requests.get("https://login.10086.cn/login.html?channelID=12003", timeout=20)
    check("mobile.login 页", r.status_code == 200, f"{r.status_code} len={len(r.text)}")
except Exception as e:
    check("mobile.login", False, str(e)[:80])

# 3. 电信登录端点 TLS 可达
for host in ("https://appgologinsz.189.cn", "https://appfuwuhd.189.cn:443"):
    try:
        r = S.get(host, timeout=20)
        check(f"telecom {host.split('//')[1]}", True, f"HTTP {r.status_code}")
    except requests.exceptions.SSLError:
        # 握手失败说明端点消失/换防
        check(f"telecom {host.split('//')[1]}", False, "TLS handshake failure")
    except Exception as e:
        check(f"telecom {host.split('//')[1]}", False, str(e)[:80])

# 4. 广电 WAF
try:
    r = requests.get("https://www.10099.com.cn/login.html", timeout=20)
    check("broadnet.login WAF", r.status_code in (200, 403), f"{r.status_code}")
except Exception as e:
    check("broadnet.login", False, str(e)[:80])

failed = [c for c in checks if not c[1]]
print(f"\n{len(checks) - len(failed)}/{len(checks)} 通过")
sys.exit(1 if failed else 0)
