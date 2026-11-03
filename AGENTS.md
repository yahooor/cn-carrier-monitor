# AGENTS.md — cn-carrier-monitor

> 给 AI 会话与新协作者的项目约定。状态类信息看 README，本文件只记「怎么干活」。

## 一句话

Home Assistant 自定义集成，聚合四大运营商（电信 / 联通 / 移动 / 广电）的话费、流量、账单。
当前 v1.10.1。

## 跑测试

```bash
pip install -r requirements-dev.txt   # pytest / pytest-asyncio / hypothesis / ruff / aiohttp / cryptography / pillow
pytest -q                             # 期望 136 passed, 2 skipped
ruff check .                          # 必须全绿
```

## 部署（**不在 HACS**，走手动部署）

```bash
haops deploy custom_components/cn_carrier_monitor --name cn_carrier_monitor --restart
```

部署前会自动 tar 备份远端目录；`--no-backup` 可跳过。装完读日志确认无 ERROR。

## 架构要点

```
custom_components/cn_carrier_monitor/
├── api/
│   ├── mobile.py     (≈1025 行) 移动：uuid 72h 自愈 + 新端点 + TLS 指纹
│   ├── unicom.py     (≈905 行)  联通：version=WT + Cookie，账号级强制短信
│   ├── telecom.py    (≈814 行)  电信：v1/v2 接口族不通用
│   └── broadnet.py              广电
├── coordinator.py    统一轮询与熔断
└── webhook.py        短信转发收码（每账号独立 webhook_secret）
```

**关键认知**：三个运营商模块函数名重叠仅 5–16%，**不是重复代码**，是三套不同协议。
不要为了「复用」而强行抽公共基类——只能抽出约 30 行，却让 800–1000 行的协议实现多一层间接。

## 找测试的技巧

`tests/` 有 19 个文件，其中 5 个按**迭代批次**命名（`test_v15_fixes` / `v16_stability` /
`v17_qa_fixes` / `v18_round2` / `v19_audit_fixes`），每个都是多主题混装。要找某功能的测试：

```bash
grep -rn "def test_.*webhook" tests/     # 按功能名 grep，不要按文件名猜
```

已知分布：`sensor` 全在 v16；`webhook` 在 v16+v19；`session` 散在 v17/v18/v19。

## 红线

- **凭据**：cookie / token / webhook_secret 一律不进代码，测试里只用假值（`duanlianjieabc=1` 这类）
- 不要改 `tests/test_*.py` 里的假值去「让测试通过」——那些值本身在测边界
- 运营商协议随时可能变（历史上移动 uuid 72h 就变过），改解析前先用 `tests/mock_carrier.py` 对齐真实结构
