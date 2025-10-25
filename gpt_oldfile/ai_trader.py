# -*- coding: utf-8 -*-
# ai_trader.py — DeepSeek 决策 → RiskManager → /order（Bridge）
import os, json, time, urllib.request
from datetime import datetime, timezone

from gpt_oldfile.deepseek_client import get_decision
from gpt_oldfile.risk_manager_first import RiskManager, RiskConfig, SymbolRule

FLASK_BASE_URL = os.getenv("FLASK_BASE_URL", "http://127.0.0.1:5001")
RUN_STATE_FILE = "run_state.json"

# ====== 测试开关：True 时会强制打一笔最小量，验证“信号→下单→日志”闭环 ======
TEST_MODE = True

# 你要交易/跟踪的品种
SYMBOLS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT", "DOGE-USDT"]

# 交易所规则（最小变动/步长）；按需改
SYMBOL_RULES = {
    "BTC-USDT": SymbolRule(price_tick=0.1,    lot_size_min=0.0001, lot_size_step=0.0001),
    "ETH-USDT": SymbolRule(price_tick=0.01,   lot_size_min=0.001,  lot_size_step=0.001),
    "SOL-USDT": SymbolRule(price_tick=0.001,  lot_size_min=0.01,   lot_size_step=0.01),
    "XRP-USDT": SymbolRule(price_tick=0.0001, lot_size_min=1.0,    lot_size_step=1.0),
    "DOGE-USDT":SymbolRule(price_tick=0.00001,lot_size_min=1.0,    lot_size_step=1.0),
    "BNB-USDT": SymbolRule(price_tick=0.01,   lot_size_min=0.01,   lot_size_step=0.01),
}

# --------------------- 基础 HTTP ---------------------
def _http_get_json(path: str):
    url = f"{FLASK_BASE_URL}{path}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))

def fetch_market() -> dict:
    """GET /market → /data 内的 last → price（float）"""
    resp = _http_get_json("/market")
    inner = resp.get("data", {})  # 你的 /market 把行情放在 data 里
    m = {}
    for s in SYMBOLS:
        v = inner.get(s)
        if isinstance(v, dict):
            px = v.get("price")
            if px is None:
                px = v.get("last")
            if px is None:
                continue
            m[s] = {**v, "price": float(px)}
    return m

def fetch_balance() -> dict:
    """GET /balance → 映射 totalEq_incl_unrealized/totalEq 为 USDT.available"""
    b = _http_get_json("/balance")
    # 你的 /balance 返回 totalEq、unrealizedPnL、totalEq_incl_unrealized
    eq = float(b.get("totalEq_incl_unrealized", b.get("totalEq", 0.0)))
    # TEST_MODE：余额为 0 时给一个默认权益，防止风控挡单
    if TEST_MODE and eq <= 0:
        eq = 10000.0
    b["USDT"] = {"available": eq}
    return b

def _pos_qty(balance: dict, symbol: str) -> float:
    """从 /balance 尽可能提取当前持仓数量（可选）"""
    pos_map = balance.get("positions") or {}
    if isinstance(pos_map, dict) and symbol in pos_map:
        for k in ("qty", "quantity", "size"):
            if k in pos_map[symbol]:
                try:
                    return float(pos_map[symbol][k])
                except:
                    pass
    lst = balance.get("positions_list") or []
    if isinstance(lst, list):
        for p in lst:
            if p.get("symbol") in (symbol, symbol.replace("-", "")):
                for k in ("qty", "quantity", "size"):
                    if k in p:
                        try:
                            return float(p[k])
                        except:
                            pass
    return 0.0

# --------------------- 状态记录（运行时长/调用计数） ---------------------
def _load_run_state():
    if os.path.exists(RUN_STATE_FILE):
        try:
            return json.load(open(RUN_STATE_FILE, "r", encoding="utf-8"))
        except:
            pass
    return {"start_ts": time.time(), "invocations": 0}

def _save_run_state(st: dict):
    json.dump(st, open(RUN_STATE_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

# --------------------- 约束拼装 → DeepSeek 调用 ---------------------
def _build_constraints():
    rules = {k: {"price_tick": v.price_tick,
                 "lot_size_min": v.lot_size_min,
                 "lot_size_step": v.lot_size_step}
             for k, v in SYMBOL_RULES.items()}
    return {"symbols": SYMBOLS, "symbol_rules": rules,
            "defaults": {"max_slippage_bps": 15}}

def _decisions_from_ai(market: dict, balance: dict) -> dict:
    """DeepSeek 决策 → TRADING_DECISIONS JSON"""
    st = _load_run_state()
    st["invocations"] += 1
    now = datetime.now(timezone.utc).isoformat()
    runtime_minutes = int((time.time() - st["start_ts"]) / 60)

    bal_snapshot = {"USDT": {"available": float(balance.get("USDT", {}).get("available", 0.0))}}
    constraints = _build_constraints()

    # 1) 调用 DeepSeek（单次返回一个最佳指令；没有强信号则可能是 hold）
    decision, meta = get_decision(market, bal_snapshot, recent_trades=[], constraints=constraints)

    # TEST_MODE：在这里覆盖 AI 输出，生成一条买入信号（用于联调）
    if TEST_MODE:
        print("[TEST_MODE] Generating manual BUY signal for BTC-USDT")
        decision = {
            "version": "1.0",
            "decision": {
                "symbol": "BTC-USDT",
                "side": "buy",
                "order_type": "market",
                "size": 0.001,
                "max_slippage_bps": 10,
                "risk": {"stop_loss_pct": 0.01, "take_profit_pct": 0.02},
                "confidence": 0.95,
                "reason": "Manual test trigger"
            },
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        }

    # 2) 风控管理器（用于 sizing）
    cfg = RiskConfig(symbol_rules=SYMBOL_RULES)
    rm = RiskManager(cfg)
    rm.equity_provider = lambda: float(bal_snapshot["USDT"]["available"])
    rm.price_provider = lambda s: float(market[s]["price"])

    # 3) 先为所有品种构造 HOLD（默认）
    td = {
        "meta": {
            "runtime_minutes": runtime_minutes,
            "runtime_hms": f"{runtime_minutes//1440}d {runtime_minutes%1440//60}h {runtime_minutes%60}m",
            "current_time": now,
            "invocations": st["invocations"],
            "account_value": float(balance.get("account_value", bal_snapshot["USDT"]["available"])),
            "available_cash": bal_snapshot["USDT"]["available"],
            "overall_return_pct": balance.get("overall_return_pct", None)
        }
    }
    for sym in SYMBOLS:
        coin = sym.split("-")[0]
        td[coin] = {
            "trade_signal_args": {
                "coin": coin,
                "signal": "hold",
                "quantity": _pos_qty(balance, sym),
                "leverage": (balance.get("positions") or {}).get(sym, {}).get("leverage", None),
                "confidence": None
            }
        }

    # 4) 将 AI 的 buy/sell 覆盖到对应币种，并做一次风险 sizing
    if isinstance(decision, dict) and "decision" in decision:
        d = decision["decision"]
        sym = d.get("symbol")
        side = d.get("side")
        if sym in SYMBOLS and side in ("buy", "sell"):
            approved, order, reason = rm.pre_trade_checks(decision, market, balance)
            print(f"[DEBUG] risk approved={approved}, reason={reason}, equity={rm._estimate_equity():.2f}")
            coin = sym.split("-")[0]

            if approved and order and float(order.get("size", 0)) > 0:
                td[coin]["trade_signal_args"].update({
                    "signal": "entry" if side == "buy" else "close",
                    "quantity": float(order["size"]),
                    "order_type": order.get("order_type", "market"),
                    "limit_price": order.get("limit_price"),
                    "max_slippage_bps": d.get("max_slippage_bps"),
                    "confidence": d.get("confidence"),
                    "profit_target": (d.get("risk") or {}).get("take_profit_pct"),
                    "stop_loss": (d.get("risk") or {}).get("stop_loss_pct"),
                    "invalidation_condition": None
                })
            else:
                # 未通过风控，仍保持 hold，并备注原因（便于调试）
                td[coin]["trade_signal_args"]["note"] = f"risk_blocked: {reason}"

                # ★ TEST_MODE：强制打一笔最小量，确保 /order 被触发，验证闭环
                if TEST_MODE:
                    rule = SYMBOL_RULES[sym]
                    forced_size = max(float(d.get("size", 0) or 0.0), rule.lot_size_min)
                    td[coin]["trade_signal_args"].update({
                        "signal": "entry" if side == "buy" else "close",
                        "quantity": forced_size,
                        "order_type": "market",
                        "limit_price": None,
                        "max_slippage_bps": d.get("max_slippage_bps", 10),
                        "confidence": d.get("confidence", 0.9),
                        "profit_target": (d.get("risk") or {}).get("take_profit_pct"),
                        "stop_loss": (d.get("risk") or {}).get("stop_loss_pct"),
                        "invalidation_condition": None,
                        "note": f"force-entry in TEST_MODE (reason={reason})"
                    })

    _save_run_state(st)
    return td

# --------------------- 主流程（单次执行） ---------------------
def main_once():
    market = fetch_market()
    balance = fetch_balance()
    decisions = _decisions_from_ai(market, balance)   # 构造 TRADING_DECISIONS JSON

    # 发送给 Bridge（日志/下单）
    from bridge_to_flask import route_trading_decisions
    ok, skipped, logged = route_trading_decisions(decisions)
    print(f"[ai_trader] orders_ok={ok}, skipped={skipped}, logged={logged}")

if __name__ == "__main__":
    main_once()
