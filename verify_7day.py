#!/usr/bin/env python3
"""7 天连续运行验收：出值率 ≥95% 且最长断档 ≤2h。

读取 cn_carrier_monitor_events.jsonl（集成 event_log.py 落盘的 JSONL，
文件超 1MB 轮转出的 .jsonl.1 也会一并读取），按 (carrier, phone) 分组评估：

- 出值率   = update_success 数 / (update_success + update_failure) 数，
             circuit_open 不计入分母；
- 最长断档 = 相邻两次 update_* 事件的最大时间差（含窗口边界：
             窗口起点→首个事件、末个事件→窗口终点）。

全部账号 出值率 ≥ --min-rate 且 最长断档 ≤ --max-gap-hours → exit 0（PASS），
否则 exit 1 并打印 FAIL 明细。实际数据跨度不足 --window-days 时按实际跨度
评估并在报告注明（notes / "window.effective"）。--json 输出机器可读结果。

用法：
  python verify_7day.py [PATH] [--window-days 7] [--min-rate 0.95]
                        [--max-gap-hours 2] [--json]
  python verify_7day.py --ssh user@host [/config/cn_carrier_monitor_events.jsonl]

SSH 凭据一律从环境读取（SSH_PASSWORD / SSH_PASS / CN_CM_SSH_PASSWORD；
未设密码时回退本机 ssh-agent 密钥），绝不写入本文件或命令行。
PATH 缺省：本地模式为当前目录下 cn_carrier_monitor_events.jsonl；
SSH 模式为远端 /config/cn_carrier_monitor_events.jsonl（HA 配置目录）。
本脚本不依赖 homeassistant。
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

FILE_NAME = "cn_carrier_monitor_events.jsonl"
UPDATE_EVENTS = ("update_success", "update_failure")
EVENT_CIRCUIT_OPEN = "circuit_open"
CARRIER_NAMES = {"telecom": "电信", "unicom": "联通", "mobile": "移动",
                 "broadcast": "广电"}

SCHEMA = "cn-carrier-monitor/verify_7day@1"


def _parse_ts(raw: str) -> datetime:
    """解析 ISO 时间戳；带时区的统一换算为 UTC 后去掉 tzinfo 再比较。"""
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _read_text(path: str, ssh_target: str | None) -> str:
    """本地或经 SSH cat 读取文件全文；远端权限不足时回退免密 sudo 重试。"""
    if not ssh_target:
        with open(path, encoding="utf-8") as f:
            return f.read()
    try:
        import paramiko
    except ImportError as err:
        raise SystemExit("--ssh 需要 paramiko（pip install paramiko）") from err
    user, _, host = ssh_target.rpartition("@")
    password = next(
        (os.environ[k] for k in ("SSH_PASSWORD", "SSH_PASS", "CN_CM_SSH_PASSWORD")
         if os.environ.get(k)), None)
    if not host:
        host, user = user, os.environ.get("CN_CM_SSH_USER", "")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=int(os.environ.get("CN_CM_SSH_PORT", "22")),
                   username=user or None, password=password,
                   allow_agent=password is None, look_for_keys=password is None,
                   timeout=20)
    try:
        def _cat(p: str) -> tuple[int, str]:
            cmd = f"cat -- {shlex.quote(p)}"
            _, out, err = client.exec_command(cmd, timeout=60)
            code = out.channel.recv_exit_status()
            return code, out.read().decode("utf-8", "replace")

        code, text = _cat(path)
        if code != 0:
            code, text = _cat(f"/dev/null; sudo -n cat -- {shlex.quote(path)}")
        if code != 0:
            raise SystemExit(f"远端读取失败: {path}")
        return text
    finally:
        client.close()


def _load_text(path: str, ssh_target: str | None) -> str:
    """主文件 + 轮转文件 .jsonl.1（更早事件，若有）拼合，新事件在后。"""
    text = _read_text(path, ssh_target)
    try:
        older = _read_text(path + ".1", ssh_target)
    except (FileNotFoundError, SystemExit):
        older = None  # 无轮转文件属常态：本地 ENOENT / 远端 cat 失败
    return older + "\n" + text if older is not None else text


def _load_events(text: str) -> tuple[list[dict[str, Any]], int]:
    """解析 JSONL 文本，返回 (按 ts 升序的事件列表, 跳过的坏行数)。"""
    events: list[dict[str, Any]] = []
    skipped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            record["ts"] = _parse_ts(record["ts"])
        except (ValueError, KeyError, TypeError):
            skipped += 1
            continue
        events.append(record)
    events.sort(key=lambda e: e["ts"])
    return events, skipped


def _fmt_h(hours: float) -> str:
    return f"{hours:.2f}h" if hours < 100 else f"{hours:.0f}h"


def _analyze_account(
    key: tuple[str, str], events: list[dict[str, Any]], window_start: datetime,
    window_end: datetime, min_rate: float, max_gap_hours: float,
) -> dict[str, Any]:
    """评估单账号；events 为该账号窗口内全部事件（任意类型），按 ts 升序。"""
    updates = [e for e in events if e.get("event") in UPDATE_EVENTS]
    success = sum(1 for e in updates if e["event"] == "update_success")
    circuit = sum(1 for e in events if e.get("event") == EVENT_CIRCUIT_OPEN)

    rate: float | None = None
    reasons: list[str] = []
    if updates:
        rate = success / len(updates)
        if rate < min_rate:
            reasons.append(f"出值率 {rate * 100:.1f}% < {min_rate * 100:.1f}%")
    else:
        reasons.append("窗口内无 update 事件")

    # 断档：相邻 update_* 事件的时间差 + 窗口边界两段（起点→首事件、末事件→终点）
    bounds = [window_start] + [e["ts"] for e in updates] + [window_end]
    max_gap, gap_over, longest = 0.0, 0, None
    for prev, cur in zip(bounds, bounds[1:]):
        hours = (cur - prev).total_seconds() / 3600
        if hours > max_gap:
            max_gap, longest = hours, (prev, cur)
        if hours > max_gap_hours:
            gap_over += 1
    if max_gap > max_gap_hours:
        reasons.append(
            f"最长断档 {_fmt_h(max_gap)} > {_fmt_h(max_gap_hours)}"
            f"（{longest[0]:%m-%d %H:%M} → {longest[1]:%m-%d %H:%M}）")

    return {
        "carrier": key[0],
        "phone": key[1],
        "update_total": len(updates),
        "success": success,
        "failure": len(updates) - success,
        "circuit_open": circuit,
        "rate": None if rate is None else round(rate, 4),
        "max_gap_hours": round(max_gap, 4),
        "gap_over_threshold": gap_over,
        "longest_gap": None if longest is None else {
            "start": longest[0].isoformat(timespec="seconds"),
            "end": longest[1].isoformat(timespec="seconds"),
            "hours": round((longest[1] - longest[0]).total_seconds() / 3600, 4),
        },
        "ok": not reasons,
        "fail_reasons": reasons,
    }


def verify(
    events: list[dict[str, Any]], window_days: int, min_rate: float,
    max_gap_hours: float, now: datetime | None = None,
) -> dict[str, Any]:
    """核心评估：切窗口、按 (carrier, phone) 分组、逐项判定，返回报告 dict。"""
    now = now or datetime.now()
    requested_start = now - timedelta(days=window_days)
    notes: list[str] = []

    in_window = [e for e in events if requested_start <= e["ts"] <= now]
    if events and not in_window:
        notes.append(f"最近 {window_days} 天窗口内无事件（数据全部早于 "
                     f"{requested_start:%Y-%m-%d %H:%M}）")
    # 跨度按全部数据算：首事件早于窗口起点即说明监控已运行满 N 天，
    # 采用 requested 窗口；否则按实际跨度评估
    effective = "requested"
    window_start = requested_start
    span_start = span_end = now
    if events:
        span_start, span_end = events[0]["ts"], events[-1]["ts"]
        if span_end - span_start < timedelta(days=window_days):
            effective = "actual_span"
            window_start = span_start
            notes.append(
                f"实际数据跨度 {(span_end - span_start).total_seconds() / 86400:.2f}"
                f" 天（{span_start:%Y-%m-%d %H:%M} ~ {span_end:%Y-%m-%d %H:%M}），"
                f"不足 {window_days} 天，按实际跨度评估")
    window_end = now

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for e in in_window:
        groups[(str(e.get("carrier", "?")), str(e.get("phone", "?")))].append(e)

    accounts = [
        _analyze_account(key, evts, window_start, window_end, min_rate,
                         max_gap_hours)
        for key, evts in sorted(groups.items())
    ]
    passed = bool(accounts) and all(a["ok"] for a in accounts)
    if not accounts:
        notes.append("没有任何账号数据，无法验收")

    return {
        "schema": SCHEMA,
        "generated_at": now.isoformat(timespec="seconds"),
        "window_days": window_days,
        "thresholds": {"min_rate": min_rate, "max_gap_hours": max_gap_hours},
        "window": {
            "start": window_start.isoformat(timespec="seconds"),
            "end": window_end.isoformat(timespec="seconds"),
            "days": round((window_end - window_start).total_seconds() / 86400, 4),
            "effective": effective,
            "data_span": {
                "start": span_start.isoformat(timespec="seconds"),
                "end": span_end.isoformat(timespec="seconds"),
                "days": round((span_end - span_start).total_seconds() / 86400, 4),
            },
        },
        "overall": "PASS" if passed else "FAIL",
        "notes": notes,
        "accounts": accounts,
        "event_total": len(events),
        "events_in_window": len(in_window),
    }


def _render(report: dict[str, Any], source: str) -> str:
    t = report["thresholds"]
    w = report["window"]
    lines = ["=== cn-carrier-monitor 连续运行验收 ===",
             f"数据源: {source}（事件 {report['event_total']} 条"
             + (f"，坏行跳过 {report['lines_skipped']}"
                if report["lines_skipped"] else "") + "）",
             f"窗口: {w['start'].replace('T', ' ')} ~ "
             f"{w['end'].replace('T', ' ')}"
             f"（{w['days']:.2f} 天，"
             f"{'按实际跨度评估' if w['effective'] == 'actual_span' else '完整窗口'}）",
             f"阈值: 出值率 ≥ {t['min_rate'] * 100:.0f}%，"
             f"最长断档 ≤ {_fmt_h(t['max_gap_hours'])}"]
    for n in report["notes"]:
        lines.append(f"注记: {n}")
    for a in report["accounts"]:
        name = CARRIER_NAMES.get(a["carrier"], a["carrier"])
        rate = "N/A" if a["rate"] is None else f"{a['rate'] * 100:.1f}%"
        num = "-" if a["rate"] is None else f"{a['success']}/{a['update_total']}"
        lines.append(
            f"── {name} {a['carrier']}/{a['phone']}  "
            f"{'PASS' if a['ok'] else 'FAIL'}  出值率 {rate} ({num})  "
            f"最长断档 {_fmt_h(a['max_gap_hours'])}  熔断 {a['circuit_open']} 次")
        for r in a["fail_reasons"]:
            lines.append(f"     !! {r}")
    ok_count = sum(1 for a in report["accounts"] if a["ok"])
    lines.append(f"总体: {report['overall']}"
                 f"（{ok_count}/{len(report['accounts'])} 账号达标）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="7 天连续运行验收（出值率≥95% 且最长断档≤2h → PASS）")
    ap.add_argument("path", nargs="?", default=None,
                    help=f"events 文件路径（缺省 {FILE_NAME}；"
                         "--ssh 模式为远端路径，缺省 /config/ 下同名文件）")
    ap.add_argument("--window-days", type=int, default=7,
                    help="验收窗口天数（默认 7）")
    ap.add_argument("--min-rate", type=float, default=0.95,
                    help="出值率阈值（默认 0.95）")
    ap.add_argument("--max-gap-hours", type=float, default=2.0,
                    help="最长断档阈值小时数（默认 2）")
    ap.add_argument("--ssh", metavar="USER@HOST", nargs="?", const="",
                    default=None,
                    help="经 SSH cat 读远端文件；不带值时读环境 CN_CM_SSH_TARGET")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args(argv)

    if args.window_days <= 0:
        ap.error("--window-days 必须为正整数")
    ssh_target = args.ssh
    if ssh_target == "":
        ssh_target = os.environ.get("CN_CM_SSH_TARGET", "")
        if not ssh_target:
            ap.error("--ssh 未带值且未设置 CN_CM_SSH_TARGET")

    path = args.path
    if ssh_target:
        path = path or f"/config/{FILE_NAME}"
    else:
        path = path or FILE_NAME
        if not os.path.exists(path):
            ap.error(f"找不到事件文件: {path}（HA 配置目录下应有 {FILE_NAME}）")

    events, skipped = _load_events(_load_text(path, ssh_target))
    report = verify(events, args.window_days, args.min_rate, args.max_gap_hours)
    report["source"] = {"mode": "ssh" if ssh_target else "local", "path": path}
    report["lines_skipped"] = skipped

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        source = f"ssh://{ssh_target}{path}" if ssh_target else path
        print(_render(report, source))
    return 0 if report["overall"] == "PASS" else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
