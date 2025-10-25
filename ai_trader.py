# -*- coding: utf-8 -*-
# ai_trader.py — DeepSeek 决策 → RiskManager → /order（Bridge）
import os, json, time, urllib.request
from datetime import datetime, timezone
from deepseek_client import get_decision
from risk_manager import RiskManager, RiskConfig, SymbolRule

import talib
import numpy as np
import pandas as pd

from pathlib import Path

LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
AI_STATUS_FILE = LOG_DIR / "ai_status.json"

def _to_float(x, default=None):
    try:
        return float(x)
    except:
        return default

def _vol24_from_market_row(row: dict) -> float:
    """用 24h 高低估算波动率：(high - low) / last"""
    last = _to_float(row.get("price") or row.get("last"), None)
    high = _to_float(row.get("high") or row.get("high24h"), None)
    low  = _to_float(row.get("low")  or row.get("low24h"), None)
    if last and high and low and last > 0:
        return (high - low) / last
    return None

def _cooldown_calc(rm, cfg, sym, ai_confidence, equity, market):
    """复用与风控一致的冷却计算逻辑（仅用于展示，不参与判断）"""
    # 动态冷却
    avg_drawdown = 0.0
    vol_pct = None
    if sym in market:
        vol_pct = _vol24_from_market_row(market[sym]) or 0.0
    # 用 24h 波动粗略近似（展示用）
    vola_for_cooldown = (vol_pct or 0.0)
    dynamic_cd = rm.adaptive_cooldown(
        consecutive_losses=rm.state.consecutive_losses,
        avg_drawdown=avg_drawdown,
        volatility=vola_for_cooldown,
        ai_confidence=ai_confidence or 0.7
    )
    # 固定/动态模式选择（与 pre_trade_checks 保持一致）
    mode = "fixed" if cfg.cooldown_global_sec < 600 else "dynamic"
    cooldown_time = cfg.cooldown_global_sec if mode == "fixed" else dynamic_cd

    # 计算剩余
    last_ts = 0
    if rm.state.last_trade_ts:
        try:
            last_ts = max(rm.state.last_trade_ts.values())
        except:
            pass
    now_ts = int(time.time())
    elapsed = now_ts - last_ts if last_ts > 0 else 1e9
    remaining = max(0, cooldown_time - elapsed)
    active = (rm.state.consecutive_losses >= cfg.max_consecutive_losses) and (remaining > 0)

    return {
        "mode": mode,
        "cooldown_seconds": int(cooldown_time),
        "remaining_seconds": int(remaining),
        "active": active
    }

def _gray_unlock_assess(rm, cfg, sym, ai_confidence, market) -> dict:
    """
    灰度智能提前解锁（只展示，不执行）
    规则：仅当处于全局冷却且剩余>0时，
         若 24h 波动率 < 1.0% 且 AI 置信度 >= 0.80 则建议提前解锁
         或 AI 置信度 >= 0.90 时强建议
    """
    cd = _cooldown_calc(rm, cfg, sym, ai_confidence, None, market)
    if not cd["active"]:
        return {"suggested": False, "level": "none", "reason": "not in cooldown"}

    vol_pct = None
    if sym in market:
        vol_pct = _vol24_from_market_row(market[sym])
    vol_ok = (vol_pct is not None) and (vol_pct < 0.01)  # <1%
    conf_ok = (ai_confidence or 0.0) >= 0.80
    strong_conf = (ai_confidence or 0.0) >= 0.90

    if strong_conf and vol_ok:
        return {"suggested": True, "level": "strong", "reason": f"vol={vol_pct:.2%}, conf={ai_confidence:.2f}"}
    if conf_ok and vol_ok:
        return {"suggested": True, "level": "normal", "reason": f"vol={vol_pct:.2%}, conf={ai_confidence:.2f}"}
    return {"suggested": False, "level": "none", "reason": f"vol={None if vol_pct is None else f'{vol_pct:.2%}'} conf={ai_confidence or 0.0:.2f}"}

def _write_ai_status(status: dict):
    try:
        AI_STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[WARN] write ai_status.json failed: {e}")

FLASK_BASE_URL = os.getenv("FLASK_BASE_URL", "http://127.0.0.1:5001")
RUN_STATE_FILE = "run_state.json"

# 你要交易/跟踪的品种
SYMBOLS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT", "DOGE-USDT"]

#测试模式(测试代码已删除)
TEST_MODE = False 

# 交易所规则（最小变动/步长）；按需改
SYMBOL_RULES = {
    "BTC-USDT": SymbolRule(price_tick=0.1,    lot_size_min=0.0001, lot_size_step=0.0001),
    "ETH-USDT": SymbolRule(price_tick=0.01,   lot_size_min=0.001,  lot_size_step=0.001),
    "SOL-USDT": SymbolRule(price_tick=0.001,  lot_size_min=0.01,   lot_size_step=0.01),
    "XRP-USDT": SymbolRule(price_tick=0.0001, lot_size_min=1.0,    lot_size_step=1.0),
    "DOGE-USDT":SymbolRule(price_tick=0.00001,lot_size_min=1.0,    lot_size_step=1.0),
    "BNB-USDT": SymbolRule(price_tick=0.01,   lot_size_min=0.01,   lot_size_step=0.01),
}

# ===================== 新增：进度打印工具 =====================
class ProgressLogger:
    def __init__(self):
        self.step_num = 0
        self.start_time = time.time()
    
    def step(self, msg: str, detail: str = ""):
        """打印进度步骤"""
        self.step_num += 1
        elapsed = time.time() - self.start_time
        timestamp = datetime.now().strftime("%H:%M:%S")
        
        print(f"\n{'='*70}")
        print(f"[{timestamp}] 步骤 {self.step_num} | 耗时: {elapsed:.2f}s")
        print(f">>> {msg}")
        if detail:
            print(f"    {detail}")
        print(f"{'='*70}")
    
    def substep(self, msg: str):
        """子步骤（不增加步骤号）"""
        elapsed = time.time() - self.start_time
        print(f"  ⏳ [{elapsed:.2f}s] {msg}")
    
    def success(self, msg: str):
        """成功提示"""
        elapsed = time.time() - self.start_time
        print(f"  ✅ [{elapsed:.2f}s] {msg}")
    
    def warning(self, msg: str):
        """警告提示"""
        elapsed = time.time() - self.start_time
        print(f"  ⚠️  [{elapsed:.2f}s] {msg}")
    
    def error(self, msg: str):
        """错误提示"""
        elapsed = time.time() - self.start_time
        print(f"  ❌ [{elapsed:.2f}s] {msg}")

# 全局进度记录器
progress = ProgressLogger()

# --------------------- 基础 HTTP ---------------------
def _http_get_json(path: str):
    url = f"{FLASK_BASE_URL}{path}"
    progress.substep(f"发送 HTTP 请求: {url}")
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            progress.success(f"接收到响应: {len(str(data))} 字符")
            return data
    except Exception as e:
        progress.error(f"HTTP 请求失败: {e}")
        raise

import talib
import numpy as np

def fetch_market() -> dict:
    """
    获取行情数据 + 自动计算技术指标（EMA / RSI / ATR / MACD / ADX / BOLL）
    """
    progress.step("获取市场行情", "调用 /market 接口")
    resp = _http_get_json("/market")
    inner = resp.get("data", {})
    m = {}

    for s in SYMBOLS:
        v = inner.get(s)
        if not isinstance(v, dict):
            continue

        price = float(v.get("price") or v.get("last") or 0)
        if price <= 0:
            progress.warning(f"{s} 没有价格数据")
            continue

        # === 模拟最近K线数据（或直接从交易所拉K线） ===
        candles = v.get("candles")  # 例如最近 100 根 [open, high, low, close, volume]
        if not candles:
            # 没有蜡烛数据时，构造一个虚拟价格序列防止talib报错
            prices = np.array([price * (1 + np.sin(i/10)*0.02) for i in range(120)], dtype=float)
            highs = prices * 1.01
            lows = prices * 0.99
            closes = prices
        else:
            closes = np.array([c[4] for c in candles], dtype=float)  # close
            highs  = np.array([c[2] for c in candles], dtype=float)  # high
            lows   = np.array([c[3] for c in candles], dtype=float)  # low

        # === 用 TA-Lib 计算基础指标 ===
        ema_fast = float(talib.EMA(closes, timeperiod=7)[-1])
        ema_slow = float(talib.EMA(closes, timeperiod=25)[-1])
        rsi14 = float(talib.RSI(closes, timeperiod=14)[-1])
        atr14 = float(talib.ATR(highs, lows, closes, timeperiod=14)[-1])

        # === 新增：趋势确认与波动指标 ===
        macd, macd_signal, _ = talib.MACD(closes, fastperiod=12, slowperiod=26, signalperiod=9)
        adx14 = talib.ADX(highs, lows, closes, timeperiod=14)[-1]
        boll_upper, boll_middle, boll_lower = talib.BBANDS(
            closes, timeperiod=20, nbdevup=2, nbdevdn=2, matype=0
        )

        # === 🧹 在这里插入 NaN 清洗 ===
        ema_fast = float(np.nan_to_num(ema_fast))
        ema_slow = float(np.nan_to_num(ema_slow))
        rsi14 = float(np.nan_to_num(rsi14))
        atr14 = float(np.nan_to_num(atr14))
        macd = np.nan_to_num(macd)
        macd_signal = np.nan_to_num(macd_signal)
        adx14 = float(np.nan_to_num(adx14))
        boll_upper = float(np.nan_to_num(boll_upper[-1]))
        boll_middle = float(np.nan_to_num(boll_middle[-1]))
        boll_lower = float(np.nan_to_num(boll_lower[-1]))

        # === 调试输出 ===
        progress.substep(
            f"{s}: EMAf={ema_fast:.2f}, EMAs={ema_slow:.2f}, RSI={rsi14:.1f}, MACD={macd[-1]:.4f}, ADX={adx14:.2f}"
        )


        # === 汇总所有指标 ===
        m[s] = {
            **v,
            "price": price,
            # 趋势类
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            # 动能类
            "rsi14": rsi14,
            # 波动类
            "atr14": atr14,
            # 趋势确认
            "macd": float(macd[-1]),
            "macd_signal": float(macd_signal[-1]),
            # 趋势强度
            "adx14": float(adx14),
            # 波动区间
            "boll_upper": float(boll_upper),
            "boll_mid": float(boll_middle),
            "boll_lower": float(boll_lower),
        }

        # progress.substep(
        #     f"{s}: price={price:.2f}, RSI={rsi14:.1f}, EMA={ema_fast:.1f}/{ema_slow:.1f}, "
        #     f"MACD={macd[-1]:.4f}, ADX={adx14:.2f}"
        # )
        print(f"[DEBUG] {s}: EMAf={ema_fast:.2f}, EMAs={ema_slow:.2f}, RSI={rsi14:.2f}, MACD={macd[-1]:.4f}, MACD_sig={macd_signal[-1]:.4f}, ADX={adx14:.2f}, BOLL=({boll_lower:.2f}, {boll_middle:.2f}, {boll_upper:.2f})")
    
    progress.success(f"获取到 {len(m)} 个交易对的行情（含EMA/RSI/ATR/MACD/ADX/BOLL指标）")
    return m

def fetch_balance() -> dict:
    """GET /balance → 映射 totalEq_incl_unrealized/totalEq 为 USDT.available"""
    progress.step("获取账户余额", "调用 /balance 接口")
    
    b = _http_get_json("/balance")
    eq = float(b.get("totalEq_incl_unrealized", b.get("totalEq", 0.0)))
    
    # TEST_MODE：余额为 0 时给一个默认权益
    if TEST_MODE and eq <= 0:
        progress.warning("余额为 0，TEST_MODE 使用默认权益 10000 USDT")
        eq = 10000.0
    
    b["USDT"] = {"available": eq}
    progress.success(f"账户权益: {eq:.2f} USDT")
    return b

def _pos_qty(balance: dict, symbol: str) -> float:
    """从 /balance 尽可能提取当前持仓数量"""
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

# --------------------- 状态记录 ---------------------
def _load_run_state():
    progress.substep("加载运行状态文件")
    if os.path.exists(RUN_STATE_FILE):
        try:
            state = json.load(open(RUN_STATE_FILE, "r", encoding="utf-8"))
            progress.substep(f"已运行 {state.get('invocations', 0)} 次")
            return state
        except:
            pass
    progress.substep("首次运行，创建新状态")
    return {"start_ts": time.time(), "invocations": 0}

def _save_run_state(st: dict):
    json.dump(st, open(RUN_STATE_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

# --------------------- 约束拼装 → DeepSeek 调用 ---------------------
def _build_constraints():
    progress.substep("构建交易约束规则")
    rules = {k: {"price_tick": v.price_tick,
                 "lot_size_min": v.lot_size_min,
                 "lot_size_step": v.lot_size_step}
             for k, v in SYMBOL_RULES.items()}
    return {"symbols": SYMBOLS, "symbol_rules": rules,
            "defaults": {"max_slippage_bps": 15}}

def _decisions_from_ai(market: dict, balance: dict) -> dict:
    """DeepSeek 决策 → TRADING_DECISIONS JSON"""
    progress.step("调用 AI 决策引擎", "DeepSeek 分析市场数据...")
    
    st = _load_run_state()
    st["invocations"] += 1
    now = datetime.now(timezone.utc).isoformat()
    runtime_minutes = int((time.time() - st["start_ts"]) / 60)

    bal_snapshot = {"USDT": {"available": float(balance.get("USDT", {}).get("available", 0.0))}}
    constraints = _build_constraints()

    # 1) 调用 DeepSeek
    progress.substep("🤖 等待 DeepSeek API 响应（可能需要 5-30 秒）...")
    start_ai = time.time()
    
    decision, meta = get_decision(market, bal_snapshot, recent_trades=[], constraints=constraints)
    
    ai_time = time.time() - start_ai

        # ✅ 修改判断逻辑
    if meta and meta.get('error') and meta['error'] is not None:
        progress.warning(f"DeepSeek 调用失败：{meta['error']}（已回退 HOLD）")
    else:
        progress.success(f"AI 决策完成，耗时 {ai_time:.2f}s")

    # ✅ 详细检查返回值
    print(f"\n{'='*70}")
    print(f"[决策返回值检查]")
    print(f"  decision 类型: {type(decision)}")
    print(f"  decision 内容: {decision}")
    print(f"  meta: {meta}")
    print(f"{'='*70}\n")
    
    # ✅ 修改判断逻辑
    if isinstance(meta, dict) and meta.get('error'):
        progress.warning(f"DeepSeek 调用失败：{meta['error']}（已回退 HOLD）")
    else:
        progress.success(f"AI 决策完成，耗时 {ai_time:.2f}s")

    # 2) 风控管理器
    progress.step("执行风控检查", "RiskManager 验证订单...")
    
    cfg = RiskConfig(symbol_rules=SYMBOL_RULES)
    rm = RiskManager(cfg)
    rm.equity_provider = lambda: float(bal_snapshot["USDT"]["available"])
    rm.price_provider = lambda s: float(market[s]["price"])

    # 3) 构造默认 HOLD
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
    
    progress.substep(f"初始化 {len(SYMBOLS)} 个交易对为 HOLD 状态")
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

    # 4) 应用 AI 决策
    if isinstance(decision, dict) and "decision" in decision:
        d = decision["decision"]
        sym = d.get("symbol")
        side = d.get("side")
        
        if sym in SYMBOLS and side in ("buy", "sell"):
            progress.substep(f"AI 建议: {side.upper()} {sym}, 数量: {d.get('size')}")
            
            approved, order, reason = rm.pre_trade_checks(decision, market, balance)
            
            equity = rm._estimate_equity()
            progress.substep(f"风控结果: {'✅ 通过' if approved else '❌ 拒绝'} | 原因: {reason} | 权益: {equity:.2f}")
            
            coin = sym.split("-")[0]

            ai_reason = (d.get("rationale") or d.get("reason"))
            
            if approved and order and float(order.get("size", 0)) > 0:
                progress.success(f"生成订单: {side} {order['size']} {sym}")
                
                td[coin]["trade_signal_args"].update({
                    "signal": "entry" if side == "buy" else ("close" if side == "sell" else "hold"),
                    "quantity": float(order["size"]) if approved and order else _pos_qty(balance, sym),
                    "order_type": order.get("order_type", d.get("order_type","market")),
                    "limit_price": order.get("limit_price"),
                    "max_slippage_bps": d.get("max_slippage_bps"),
                    "confidence": d.get("confidence"),
                    "profit_target": (d.get("risk") or {}).get("take_profit_pct"),
                    "stop_loss": (d.get("risk") or {}).get("stop_loss_pct"),
                    "invalidation_condition": None,
                    "ai_reason": ai_reason   # ★ 新增：把模型理由放进信号里，后续用于前端
                })
            else:
                # 未通过风控
                td[coin]["trade_signal_args"]["note"] = f"risk_blocked: {reason}"

                # TEST_MODE：强制打单
                if TEST_MODE:
                    progress.warning("TEST_MODE：忽略风控，强制下单最小量")
                    rule = SYMBOL_RULES[sym]
                    forced_size = max(float(d.get("size", 0) or 0.0), rule.lot_size_min)
                    
                    td[coin]["trade_signal_args"].update({
                        "signal": side,  # ← 直接用 side（"buy" 或 "sell"）
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
    progress.success("决策生成完成")
    return td

# --------------------- 主流程（单次执行） ---------------------
def main_once():
    market = fetch_market()
    balance = fetch_balance()
    decisions = _decisions_from_ai(market, balance)

    # === 提取最近一个非 hold 信号（若都 hold，可回退到 BTC）
    last_sym = None; last_side = None; last_size = None; last_conf = None; last_reason = None
    for sym in SYMBOLS:
        coin = sym.split("-")[0]
        args = decisions.get(coin, {}).get("trade_signal_args", {})
        # ✅ 支持 buy/sell/entry/close 四种信号类型
        if args.get("signal") in ("entry", "close", "buy", "sell"):
            last_sym = sym
            last_side = ("buy" if args.get("signal") in ("entry", "buy") else "sell")
            last_size = _to_float(args.get("quantity"), None)
            last_conf = _to_float(args.get("confidence"), None)
            last_reason = args.get("ai_reason") or args.get("reason")  # ✅ 新增：AI 理由
            break

    # 风控对象（与决策阶段保持一致）
    cfg = RiskConfig(symbol_rules=SYMBOL_RULES)
    rm = RiskManager(cfg)
    rm.equity_provider = lambda: float(balance.get("USDT", {}).get("available", 0.0))
    # 计算全局冷却信息（展示）
    sym_for_view = last_sym or "BTC-USDT"
    cooldown = _cooldown_calc(rm, cfg, sym_for_view, last_conf, balance.get("USDT",{}).get("available",0.0), market)
    gray_unlock = _gray_unlock_assess(rm, cfg, sym_for_view, last_conf, market)

    # 24h 波动率
    vol24 = None
    if sym_for_view in market:
        vol24 = _vol24_from_market_row(market[sym_for_view])

    status = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "engine": {
            "test_mode": (os.getenv("TEST_MODE").lower() in ("true","1")) if os.getenv("TEST_MODE") is not None else bool(TEST_MODE),
            "invocations": _load_run_state().get("invocations", 0),
        },
        "account": {
            "equity": _to_float(balance.get("USDT",{}).get("available", None)),
        },
        "decision": {
            "symbol": last_sym,
            "side": last_side,
            "size": last_size,
            "confidence": last_conf,
            "reason": last_reason   # ✅ 新增：写入前端页面
        },
        "risk": {
            "consecutive_losses": getattr(rm.state, "consecutive_losses", 0),
            "cooldown": cooldown,
        },
        "market": {
            "symbol": sym_for_view,
            "volatility_24h_pct": None if vol24 is None else round(vol24, 6)
        },
        "gray_unlock": gray_unlock
    }
    _write_ai_status(status)

    # === 原有下单桥接 ===
    from bridge_to_flask import route_trading_decisions
    ok, skipped, logged = route_trading_decisions(decisions)
    print(f"[ai_trader] orders_ok={ok}, skipped={skipped}, logged={logged}")


if __name__ == "__main__":
    main_once()