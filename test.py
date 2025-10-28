#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Smoketest for Quant Mock Stack: AI -> Risk -> Order -> Frontend
- Checks endpoints, schemas, logs, market data shapes, and basic system resources
- Writes a markdown report to logs/smoketest_report.md
"""

import os, sys, json, time, argparse, math, shutil, traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple

# -------- Optional deps (graceful fallback) --------
try:
    import requests
except ImportError:
    print("❌ 缺少 requests，请先: pip install requests")
    sys.exit(1)

try:
    import psutil  # optional
    HAS_PSUTIL = True
except Exception:
    HAS_PSUTIL = False


# -------- Formatting helpers --------
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
GRAY = "\033[90m"
RESET = "\033[0m"

def ok(msg):    print(f"{GREEN}✔ PASS{RESET} {msg}")
def warn(msg):  print(f"{YELLOW}● WARN{RESET} {msg}")
def fail(msg):  print(f"{RED}✖ FAIL{RESET} {msg}")

def pct(x):  # 0.1234 -> "12.34%"
    try:
        return f"{x*100:.2f}%"
    except Exception:
        return "—"

def is_number(x):
    try:
        float(x)
        return True
    except Exception:
        return False


# -------- HTTP helpers --------
def jget(session, url, timeout=6):
    try:
        r = session.get(url, timeout=timeout)
        ctype = r.headers.get("Content-Type","")
        if "application/json" in ctype or r.text.strip().startswith("{") or r.text.strip().startswith("["):
            return True, r.status_code, r.json(), r.text
        else:
            return True, r.status_code, None, r.text
    except Exception as e:
        return False, 0, None, str(e)


# -------- JSONL helpers --------
def tail_jsonl(path, n=50):
    items = []
    if not os.path.exists(path):
        return items
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()[-n:]
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            items.append(json.loads(ln))
        except Exception:
            continue
    return items


# -------- Checks --------
def check_endpoints(base, session):
    """Check core endpoints exist and return 2xx."""
    results = []
    endpoints = [
        ("/api/status", "json"),
        ("/recent/signals?limit=50", "json"),
        ("/recent/trades?limit=50", "json"),
        ("/market", "json"),
        ("/balance", "json"),
        ("/verify", "json"),          # 可能没有，容错
        ("/leaderboard", "html"),
    ]
    for path, expect in endpoints:
        ok_http, code, js, txt = jget(session, base + path)
        if not ok_http:
            fail(f"[HTTP] {path} 请求异常: {txt}")
            results.append(("fail", path, code))
            continue
        if code // 100 != 2:
            fail(f"[HTTP] {path} 状态码 {code}")
            results.append(("fail", path, code))
            continue
        if expect == "json" and js is None:
            warn(f"[HTTP] {path} 返回非 JSON（可能是 HTML/空），内容预览: {txt[:120]}")
            results.append(("warn", path, code))
        else:
            ok(f"[HTTP] {path} 200 OK")
            results.append(("ok", path, code))
    return results


def check_signals_schema(base, session, logs_dir):
    """Validate recent signals via API + file JSONL."""
    # via API
    ok_http, code, js, txt = jget(session, base + "/recent/signals?limit=50")
    issues = []
    count = 0
    if ok_http and code == 200 and isinstance(js, dict):
        items = js.get("items", [])
        count = len(items)
        if count == 0:
            warn("signals API 返回 0 条（如果刚启动可能正常）")
        else:
            # sample validate
            bad_conf = 0
            bad_side = 0
            for s in items:
                # confidence 必须是数字
                conf = s.get("confidence", s.get("conf"))
                if not is_number(conf):
                    bad_conf += 1
                # side/signal 至少有一个
                sd = (s.get("side") or s.get("action") or s.get("signal"))
                if not sd:
                    bad_side += 1
            if bad_conf == 0:
                ok(f"signals API: confidence 字段均为数字（样本 {count}）")
            else:
                issues.append(f"signals API: 有 {bad_conf}/{count} 条 confidence 不是数字")
            if bad_side == 0:
                ok("signals API: side/action/signal 字段存在")
            else:
                issues.append(f"signals API: 有 {bad_side}/{count} 条缺少 side/action/signal")

    # via file
    path = os.path.join(logs_dir, "recent_signals.jsonl")
    file_items = tail_jsonl(path, n=50)
    if os.path.exists(path):
        if file_items:
            ok(f"signals 文件存在且可解析（{path}，最近 {len(file_items)} 条）")
        else:
            warn(f"signals 文件存在但无法解析或为空（{path}）")
    else:
        warn(f"signals 文件不存在（{path}）")

    if issues:
        for m in issues:
            fail(m)
    return count, issues


def check_trades_schema(base, session, logs_dir):
    ok_http, code, js, txt = jget(session, base + "/recent/trades?limit=50")
    issues = []
    count = 0
    if ok_http and code == 200 and isinstance(js, dict):
        items = js.get("items", [])
        count = len(items)
        if count:
            missing_core = 0
            for t in items:
                if not (t.get("instId") and t.get("side") and (t.get("sz") or t.get("size"))):
                    missing_core += 1
            if missing_core == 0:
                ok("trades API: instId/side/size 基本字段齐全")
            else:
                issues.append(f"trades API: 有 {missing_core}/{count} 条缺少 instId/side/size")
        else:
            warn("trades API 返回 0 条（若尚未下单属正常）")

    path = os.path.join(logs_dir, "recent_trades.jsonl")
    file_items = tail_jsonl(path, n=50)
    if os.path.exists(path):
        if file_items:
            ok(f"trades 文件存在且可解析（{path}，最近 {len(file_items)} 条）")
        else:
            warn(f"trades 文件存在但无法解析或为空（{path}）")
    else:
        warn(f"trades 文件不存在（{path}）")

    if issues:
        for m in issues:
            fail(m)
    return count, issues


def check_market_shape(base, session):
    ok_http, code, js, txt = jget(session, base + "/market")
    issues = []
    checked = 0
    if not (ok_http and code == 200 and isinstance(js, dict)):
        fail("market API 返回异常或非 JSON")
        return 0, ["market API 不可用"]
    data = js.get("data", {})
    if not data:
        fail("market API: data 为空")
        return 0, ["market data 为空"]
    for sym, row in data.items():
        last = row.get("last")
        hi = row.get("high24h")
        lo = row.get("low24h")
        if not (is_number(last) and is_number(hi) and is_number(lo)):
            issues.append(f"{sym}: last/high24h/low24h 必须是数字")
        else:
            if float(hi) < float(lo):
                issues.append(f"{sym}: high24h < low24h")
            if not (float(lo) <= float(last) <= float(hi)):
                issues.append(f"{sym}: last 不在 [low24h, high24h] 区间")

        candles = row.get("candles", {})
        for tf in ("30m", "4h"):
            arr = candles.get(tf, [])
            if not isinstance(arr, list) or not arr:
                issues.append(f"{sym}: candles[{tf}] 为空")
                continue
            # 取几根检查 [o,h,l,c,v]
            for k in arr[:3] + arr[-3:]:
                if not (isinstance(k, list) and len(k) == 5 and all(is_number(x) for x in k)):
                    issues.append(f"{sym}: candles[{tf}] 中存在非 [o,h,l,c,v] 结构")
                    break
        checked += 1

    if checked and not issues:
        ok(f"market API: {checked} 个交易对结构与K线形态校验通过")
    else:
        for m in issues:
            fail("market: " + m)
    return checked, issues


def check_balance(base, session):
    ok_http, code, js, txt = jget(session, base + "/balance")
    if not (ok_http and code == 200 and isinstance(js, dict)):
        fail("balance API 返回异常或非 JSON")
        return None, ["balance 不可用"]
    issues = []
    teq = js.get("totalEq")
    teqi = js.get("totalEq_incl_unrealized", teq)
    if not is_number(teqi):
        issues.append("totalEq_incl_unrealized 缺失或非数字")
    if issues:
        for m in issues:
            fail("balance: " + m)
    else:
        ok(f"balance: totalEq_incl_unrealized = {float(teqi):,.2f}")
    return teqi, issues


def check_logs_growth(base, session, logs_dir):
    """Check that hitting endpoints causes flask_server.log and signals/trades to grow."""
    report = []
    # measure sizes
    files = [
        os.path.join(logs_dir, "flask_server.log"),
        os.path.join(logs_dir, "recent_signals.jsonl"),
        os.path.join(logs_dir, "recent_trades.jsonl"),
    ]
    before = {f: (os.path.getsize(f) if os.path.exists(f) else -1) for f in files}

    # hit a few endpoints to generate traffic
    for _ in range(2):
        for path in ("/api/status","/recent/signals?limit=5","/recent/trades?limit=5","/market","/balance"):
            jget(session, base + path)
        time.sleep(1.0)

    after = {f: (os.path.getsize(f) if os.path.exists(f) else -1) for f in files}

    for f in files:
        b, a = before[f], after[f]
        if b == -1 and a == -1:
            warn(f"日志文件尚不存在：{f}")
            report.append(("warn", f))
        elif a > b:
            ok(f"日志文件增长：{os.path.basename(f)} ({b} -> {a} bytes)")
            report.append(("ok", f))
        elif a == b and a > 0:
            warn(f"日志文件未增长（可能短期内无新数据）：{os.path.basename(f)}")
            report.append(("warn", f))
        else:
            warn(f"日志文件为空或未创建：{os.path.basename(f)}")
            report.append(("warn", f))
    return report


def check_system_resources():
    issues = []
    lines = []
    # disk
    try:
        total, used, free = shutil.disk_usage(".")
        free_gb = free / (1024**3)
        lines.append(f"Disk free: {free_gb:.2f} GB")
        if free_gb < 5:
            issues.append("磁盘剩余 < 5GB")
    except Exception as e:
        lines.append(f"Disk check error: {e}")

    # cpu/mem via psutil if available
    if HAS_PSUTIL:
        try:
            cpu = psutil.cpu_percent(interval=0.5)
            mem = psutil.virtual_memory().percent
            lines.append(f"CPU: {cpu:.1f}%  MEM: {mem:.1f}%")
            if cpu > 85: issues.append("CPU 使用率 > 85%")
            if mem > 85: issues.append("内存使用率 > 85%")
        except Exception as e:
            lines.append(f"psutil error: {e}")
    else:
        lines.append("psutil 不可用，跳过 CPU/MEM 细节（可 pip install psutil）")

    if issues:
        for m in issues:
            warn("资源: " + m)
        return lines, issues
    else:
        ok("系统资源正常（磁盘/CPU/内存）")
        return lines, []


def write_report(md_path, ctx):
    try:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(f"# Smoketest Report\n\n")
            f.write(f"- Time: {datetime.utcnow().isoformat()}Z\n")
            f.write(f"- Base: {ctx['base']}\n")
            f.write(f"- Logs Dir: {ctx['logs_dir']}\n\n")

            def sec(title):
                f.write(f"## {title}\n\n")

            sec("Endpoints")
            for status, path, code in ctx["endpoints"]:
                f.write(f"- [{status.upper()}] `{path}` HTTP {code}\n")
            f.write("\n")

            sec("Signals")
            f.write(f"- items: {ctx['signals_count']}\n")
            for m in ctx["signals_issues"]:
                f.write(f"  - [ISSUE] {m}\n")
            f.write("\n")

            sec("Trades")
            f.write(f"- items: {ctx['trades_count']}\n")
            for m in ctx["trades_issues"]:
                f.write(f"  - [ISSUE] {m}\n")
            f.write("\n")

            sec("Market")
            f.write(f"- checked symbols: {ctx['market_checked']}\n")
            for m in ctx["market_issues"]:
                f.write(f"  - [ISSUE] {m}\n")
            f.write("\n")

            sec("Balance")
            f.write(f"- totalEq_incl_unrealized: {ctx['balance_teqi']}\n")
            for m in ctx["balance_issues"]:
                f.write(f"  - [ISSUE] {m}\n")
            f.write("\n")

            sec("Logs Growth")
            for status, path in ctx["logs_growth"]:
                f.write(f"- [{status.upper()}] {os.path.basename(path)}\n")
            f.write("\n")

            sec("System")
            for ln in ctx["system_lines"]:
                f.write(f"- {ln}\n")
            for m in ctx["system_issues"]:
                f.write(f"  - [ISSUE] {m}\n")
            f.write("\n")

            # summary
            sec("Summary")
            total_fail = ctx["fail_count"]
            total_warn = ctx["warn_count"]
            f.write(f"- FAIL: {total_fail}\n")
            f.write(f"- WARN: {total_warn}\n")
            f.write(f"- PASS: {ctx['pass_count']}\n")
            f.write("\n")
        return True
    except Exception as e:
        print("写报告失败：", e)
        return False


def main():
    parser = argparse.ArgumentParser(description="One-click smoketest for Quant Mock stack.")
    parser.add_argument("--base", default="http://127.0.0.1:5001", help="Base URL of mock server")
    parser.add_argument("--logs", default="./logs", help="Logs directory")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    logs_dir = args.logs
    os.makedirs(logs_dir, exist_ok=True)

    print(f"\n=== Smoketest @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    print(f"Base: {base}")
    print(f"Logs: {logs_dir}\n")

    s = requests.Session()
    pass_cnt = warn_cnt = fail_cnt = 0

    # 1) Endpoints
    print("1) Endpoints 探活")
    ep_results = check_endpoints(base, s)
    for st, _, _ in ep_results:
        if st == "ok": pass_cnt += 1
        elif st == "warn": warn_cnt += 1
        else: fail_cnt += 1
    print()

    # 2) Signals schema
    print("2) Signals 结构校验")
    sig_count, sig_issues = check_signals_schema(base, s, logs_dir)
    if sig_issues: fail_cnt += len(sig_issues)
    else: pass_cnt += 1
    print()

    # 3) Trades schema
    print("3) Trades 结构校验")
    trd_count, trd_issues = check_trades_schema(base, s, logs_dir)
    if trd_issues: fail_cnt += len(trd_issues)
    else: pass_cnt += 1
    print()

    # 4) Market shape
    print("4) Market 结构与K线形态")
    mkt_checked, mkt_issues = check_market_shape(base, s)
    if mkt_issues: fail_cnt += len(mkt_issues)
    else: pass_cnt += 1
    print()

    # 5) Balance
    print("5) Balance 账户状态")
    teqi, bal_issues = check_balance(base, s)
    if bal_issues: fail_cnt += len(bal_issues)
    else: pass_cnt += 1
    print()

    # 6) Logs growth
    print("6) 日志增长（访问行为触发）")
    lg = check_logs_growth(base, s, logs_dir)
    # count warns/ok
    for st, _ in lg:
        if st == "ok": pass_cnt += 1
        else: warn_cnt += 1
    print()

    # 7) System resources
    print("7) 系统资源")
    sys_lines, sys_issues = check_system_resources()
    for ln in sys_lines:
        print("   ", ln)
    if sys_issues: warn_cnt += len(sys_issues)
    else: pass_cnt += 1
    print()

    # Write report
    ctx = dict(
        base=base,
        logs_dir=logs_dir,
        endpoints=ep_results,
        signals_count=sig_count, signals_issues=sig_issues,
        trades_count=trd_count, trades_issues=trd_issues,
        market_checked=mkt_checked, market_issues=mkt_issues,
        balance_teqi=teqi, balance_issues=bal_issues,
        logs_growth=lg,
        system_lines=sys_lines, system_issues=sys_issues,
        pass_count=pass_cnt, warn_count=warn_cnt, fail_count=fail_cnt
    )
    md_path = os.path.join(logs_dir, "smoketest_report.md")
    if write_report(md_path, ctx):
        print(f"{GRAY}报告已写入: {md_path}{RESET}")

    # Summary
    print("\n=== 总结 ===")
    print(f"{GREEN}PASS: {pass_cnt}{RESET}  {YELLOW}WARN: {warn_cnt}{RESET}  {RED}FAIL: {fail_cnt}{RESET}")
    if fail_cnt == 0:
        print(f"{GREEN}→ Smoketest 通过，可进入下一阶段（伪实盘/接入真盘）{RESET}")
        sys.exit(0)
    else:
        print(f"{RED}→ Smoketest 存在未通过项，请修复后重试{RESET}")
        sys.exit(1)


if __name__ == "__main__":
    main()
