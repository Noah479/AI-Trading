# -*- coding: utf-8 -*-
# ai_trader.py — DeepSeek 决策 → RiskManager → /order（Bridge）
import os, json, time, urllib.request
from datetime import datetime, timezone
from deepseek_client import get_decision
from risk_manager import RiskManager, RiskConfig, SymbolRule



import random
# import talib
import numpy as np
import pandas as pd
import csv
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

def _preprocess_macd_signals(market: dict) -> dict:
    """
    为每个交易对添加 MACD 金叉/死叉标记
    
    判断逻辑：
    - 金叉：前一周期 macd <= signal，当前周期 macd > signal
    - 死叉：前一周期 macd >= signal，当前周期 macd < signal
    """
    for sym, row in market.items():
        # === 30m 周期（主周期）===
        macd = row.get("macd", 0)
        macd_signal = row.get("macd_signal", 0)
        macd_prev = row.get("macd_prev", 0)
        macd_signal_prev = row.get("macd_signal_prev", 0)
        
        # 判断金叉/死叉
        is_golden_cross = (macd_prev <= macd_signal_prev) and (macd > macd_signal)
        is_death_cross = (macd_prev >= macd_signal_prev) and (macd < macd_signal)
        
        row["macd_golden_cross"] = is_golden_cross
        row["macd_death_cross"] = is_death_cross
        
        # === 处理 3m 和 4h 周期 ===
        tf = row.get("tf", {})
        for period in ["3m", "4h"]:
            if period not in tf:
                continue
            
            p_data = tf[period]
            p_macd = p_data.get("macd", 0)
            p_macd_signal = p_data.get("macd_signal", 0)
            p_macd_prev = p_data.get("macd_prev", 0)
            p_macd_signal_prev = p_data.get("macd_signal_prev", 0)
            
            p_data["macd_golden_cross"] = (p_macd_prev <= p_macd_signal_prev) and (p_macd > p_macd_signal)
            p_data["macd_death_cross"] = (p_macd_prev >= p_macd_signal_prev) and (p_macd < p_macd_signal)
    
    return market

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


def _indicators_from_candles(candles_arr):
    """
    candles_arr: [[open,high,low,close,volume], ...] 旧->新
    输出：一套 EMA/RSI/ATR/MACD/ADX/BOLL 指标（最后一根）
    ✅ 新增：返回前一周期 MACD 用于判断金叉/死叉
    """
    import numpy as np, talib
    closes = np.array([c[3] for c in candles_arr], dtype=float)
    highs  = np.array([c[1] for c in candles_arr], dtype=float)
    lows   = np.array([c[2] for c in candles_arr], dtype=float)

    ema_fast = float(np.nan_to_num(talib.EMA(closes, timeperiod=12)[-1]))
    ema_slow = float(np.nan_to_num(talib.EMA(closes, timeperiod=48)[-1]))
    rsi14    = float(np.nan_to_num(talib.RSI(closes, timeperiod=14)[-1]))
    atr14    = float(np.nan_to_num(talib.ATR(highs, lows, closes, timeperiod=14)[-1]))
    
    # ✅ MACD 改进：同时返回当前值和前一周期
    macd_arr, macd_signal_arr, _ = talib.MACD(closes, fastperiod=12, slowperiod=26, signalperiod=9)
    macd = float(np.nan_to_num(macd_arr[-1]))
    macd_signal = float(np.nan_to_num(macd_signal_arr[-1]))
    
    # ✅ 新增：前一周期的 MACD（用于判断趋势变化）
    macd_prev = float(np.nan_to_num(macd_arr[-2])) if len(macd_arr) > 1 else macd
    macd_signal_prev = float(np.nan_to_num(macd_signal_arr[-2])) if len(macd_signal_arr) > 1 else macd_signal
    
    adx14 = float(np.nan_to_num(talib.ADX(highs, lows, closes, timeperiod=14)[-1]))
    bu, bm, bl = talib.BBANDS(closes, timeperiod=20, nbdevup=2, nbdevdn=2, matype=0)
    boll_upper = float(np.nan_to_num(bu[-1]))
    boll_mid = float(np.nan_to_num(bm[-1]))
    boll_lower = float(np.nan_to_num(bl[-1]))
    
    return dict(
        ema_fast=ema_fast, ema_slow=ema_slow, rsi14=rsi14, atr14=atr14,
        macd=macd, macd_signal=macd_signal,
        macd_prev=macd_prev, macd_signal_prev=macd_signal_prev,  # ✅ 新增
        adx14=adx14,
        boll_upper=boll_upper, boll_mid=boll_mid, boll_lower=boll_lower
    )

def fetch_market() -> dict:
    """
    获取行情数据 + (30m 基线 & 4h 背景) 指标
    返回：
      market[sym] = {
        price,last,high24h,low24h,
        # 30m 扁平字段（与 deepseek_client 现有读取兼容）
        ema_fast, ema_slow, rsi14, atr14, macd, macd_signal, adx14, boll_upper, boll_mid, boll_lower,
        # 4h 背景（如果服务端提供或可近似聚合）
        "tf": {"4h": {同上键}}
      }
    """
    progress.step("获取市场行情", "调用 /market 接口")
    resp = _http_get_json("/market")
    inner = resp.get("data", {})
    m = {}

    for s in SYMBOLS:
        v = inner.get(s) or {}
        if not isinstance(v, dict):
            continue

        price = float(v.get("price") or v.get("last") or 0.0)
        if price <= 0:
            progress.warning(f"{s} 没有价格数据")
            continue

        # ✅ 兼容三周期：3m + 30m + 4h
        candles_raw = v.get("candles")
        c3m, c30m, c4h = None, None, None

        if isinstance(candles_raw, dict):
            c3m = candles_raw.get("3m")   # ✅ 新增
            c30m = candles_raw.get("30m")
            c4h = candles_raw.get("4h")
        elif isinstance(candles_raw, (list, tuple)):
            c30m = candles_raw  # 兼容旧结构：默认当作 30m

        progress.substep(f"{s} | 获取到 3m:{len(c3m or [])} / 30m:{len(c30m or [])} / 4h:{len(c4h or [])} 根K线")

        # ✅ 兜底逻辑：优先级 30m > 3m > 模拟数据
        if not c30m or len(c30m) < 60:
            if c3m and len(c3m) >= 60:
                # 用 3m 聚合成 30m（每 10 根聚合为 1 根）
                c30m = []
                for i in range(0, len(c3m) - 10, 10):
                    chunk = c3m[i:i+10]
                    o = chunk[0][0]
                    h = max(x[1] for x in chunk)
                    l = min(x[2] for x in chunk)
                    c = chunk[-1][3]
                    vol = sum(x[4] for x in chunk)
                    c30m.append([o, h, l, c, vol])
                progress.warning(f"{s} 用 3m 聚合生成 30m ({len(c30m)} 根)")
            else:
                # 最终兜底：生成模拟数据
                import numpy as np
                closes = np.array([price*(1+0.01*np.sin(i/8)) for i in range(120)], dtype=float)
                c30m = [[closes[i], closes[i]*1.01, closes[i]*0.99, closes[i], 1.0] for i in range(len(closes))]
                progress.warning(f"{s} 缺少真实 K线，使用模拟序列兜底")

        # ✅ 兜底 4h：30m × 8 聚合
        if not c4h or len(c4h) < 60:
            if c30m and len(c30m) >= 8:
                c4h = []
                for i in range(0, len(c30m) - 8, 8):
                    chunk = c30m[i:i+8]
                    o = chunk[0][0]
                    h = max(x[1] for x in chunk)
                    l = min(x[2] for x in chunk)
                    c = chunk[-1][3]
                    vol = sum(x[4] for x in chunk)
                    c4h.append([o, h, l, c, vol])
                progress.substep(f"{s} 用 30m 聚合生成 4h ({len(c4h)} 根)")
            else:
                c4h = c30m[::8] if c30m else []  # 最终兜底：稀疏采样

        # ✅ 修复：兜底 3m（如果没有，从最近 72 根 30m 拆分）
        if not c3m or len(c3m) < 60:
            if c30m and len(c30m) > 0:
                c3m = []
                # ✅ 关键修改：只取最近 72 根 30m（相当于 36 小时）
                recent_30m = c30m[-72:] if len(c30m) >= 72 else c30m
                
                for candle in recent_30m:
                    # 将 1 根 30m 拆成 10 根 3m（价格微调模拟）
                    o, h, l, c, vol = candle
                    step = (c - o) / 10
                    for j in range(10):
                        mini_o = o + step * j
                        mini_c = o + step * (j + 1)
                        mini_h = max(mini_o, mini_c) * 1.001
                        mini_l = min(mini_o, mini_c) * 0.999
                        c3m.append([mini_o, mini_h, mini_l, mini_c, vol / 10])
                
                progress.substep(f"{s} 用最近 {len(recent_30m)} 根 30m 拆分生成 3m ({len(c3m)} 根)")

        # ✅ 计算三个周期的指标
        base3m = _indicators_from_candles(c3m) if c3m and len(c3m) >= 30 else None
        base30m = _indicators_from_candles(c30m) if c30m and len(c30m) >= 30 else None
        ctx4h = _indicators_from_candles(c4h) if c4h and len(c4h) >= 30 else None

        # ✅ 三周期调试输出
        debug_msg = f"{s} |"
        if base3m:
            debug_msg += f" 3m: RSI={base3m['rsi14']:.1f} ADX={base3m['adx14']:.1f} |"
        if base30m:
            debug_msg += f" 30m: RSI={base30m['rsi14']:.1f} MACD={base30m['macd']:.4f} ADX={base30m['adx14']:.1f}"
        if ctx4h:
            debug_msg += f" | 4h: RSI={ctx4h['rsi14']:.1f} ADX={ctx4h['adx14']:.1f}"

        progress.substep(debug_msg)

        # ✅ 汇总：30m 扁平 + 3m/4h 嵌套
        row = {
            "price": price,
            "last": price,
            "high24h": float(v.get("high24h") or 0.0),
            "low24h": float(v.get("low24h") or 0.0),
            **(base30m or {})  # 30m 指标作为主指标（扁平）
        }

        # ✅ 多周期嵌套（供高级策略使用）
        row["tf"] = {}
        if base3m:
            row["tf"]["3m"] = base3m
        if ctx4h:
            row["tf"]["4h"] = ctx4h

        m[s] = row

    progress.success(f"获取到 {len(m)} 个交易对 (含 30m & 4h 指标)")
    # ✅ 在这里添加（return 之前）
    m = _preprocess_macd_signals(m)

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

def _log_decision_to_csv(decision: dict, meta: dict, market: dict, log_dir="logs"):
    """
    把每次 AI 决策结果记录到 logs/ai_decision_log.csv
    """
    os.makedirs(log_dir, exist_ok=True)
    file_path = os.path.join(log_dir, "ai_decision_log.csv")
    headers = [
        "ts","symbol","side","confidence","rationale","leverage",
        "stop_loss_pct","take_profit_pct","adx14","rsi14","macd","price"
    ]

    d = decision.get("decision", {})
    sym = d.get("symbol")
    row = market.get(sym, {})

    record = {
        "ts": decision.get("ts"),
        "symbol": sym,
        "side": d.get("side"),
        "confidence": d.get("confidence"),
        "leverage": d.get("leverage"), 
        "rationale": d.get("rationale"),
        "stop_loss_pct": (d.get("risk") or {}).get("stop_loss_pct"),
        "take_profit_pct": (d.get("risk") or {}).get("take_profit_pct"),
        "adx14": row.get("adx14"),
        "rsi14": row.get("rsi14"),
        "macd": row.get("macd"),
        "price": row.get("price")
    }

    write_header = not os.path.exists(file_path)
    with open(file_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        if write_header:
            writer.writeheader()
        writer.writerow(record)

    print(f"🧾 已写入日志: {file_path}")

# === 新增：记录所有币种信号到 CSV（含 HOLD） ===
def _reason_explain_from_indicators(row: dict) -> str:
    """给 HOLD/本地信号生成可读理由（中文）。"""
    try:
        adx = float(row.get("adx14") or 0.0)
    except Exception:
        adx = 0.0
    try:
        rsi = float(row.get("rsi14") or 50.0)
    except Exception:
        rsi = 50.0
    try:
        macd = float(row.get("macd") or 0.0)
        macds = float(row.get("macd_signal") or 0.0)
    except Exception:
        macd, macds = 0.0, 0.0

    emaf = row.get("ema_fast")
    emas = row.get("ema_slow")
    trend_up = (emaf is not None and emas is not None and emaf > emas)

    # === 根据指标逻辑生成中文理由 ===
    if adx < 20:
        return "ADX<20震荡观望"
    if macd >= macds and adx >= 20:
        return "MACD金叉+ADX走强" if trend_up else "MACD金叉但均线未多头"
    if macd < macds and trend_up and 20 <= adx < 25:
        return "趋势多但动能转弱，谨慎观望"
    if rsi < 30:
        return "RSI超卖反弹观察"
    if rsi > 70:
        return "RSI超买回落观察"
    return "数据中性，继续等待"


def _log_all_signals_to_csv(trading_decisions: dict, market: dict, log_dir: str | None = None):
    """
    将 TRADING_DECISIONS（含所有币种）记录到 logs/all_signals.csv
    每次运行写入6行（或 N 行）：每个币一行，哪怕是 HOLD 也会写入。
    """
    import csv, os
    from datetime import datetime, timezone
    from pathlib import Path

    # 默认路径
    log_dir = str(Path("logs"))
    os.makedirs(log_dir, exist_ok=True)
    path = str(Path(log_dir) / "all_signals.csv")

    headers = [
        "ts","symbol","signal","quantity","confidence","leverage","ai_reason",
        "adx14","rsi14","macd","macd_signal","ema_fast","ema_slow","price"
    ]
    write_header = not os.path.exists(path)

    ts = (trading_decisions.get("meta") or {}).get("current_time")
    if not ts:
        ts = datetime.now(timezone.utc).isoformat()

    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        if write_header:
            w.writeheader()

        for coin, body in (trading_decisions or {}).items():
            if coin == "meta":
                continue
            args = (body.get("trade_signal_args") or {})
            signal = (args.get("signal") or "hold").lower()
            symbol = f"{coin}-USDT"

            mrow = market.get(symbol, {}) if isinstance(market, dict) else {}
            # 如果 signal 是 hold，就强制使用本地解释
            reason = (
                _reason_explain_from_indicators(mrow)
                if signal == "hold"
                else (args.get("ai_reason") or _reason_explain_from_indicators(mrow))
            )

            rec = {
                "ts": ts,
                "symbol": symbol,
                "signal": signal,
                "quantity": args.get("quantity", 0.0),
                "confidence": args.get("confidence", 0.5),
                "leverage": args.get("leverage", ""),
                "ai_reason": reason,
                "adx14": mrow.get("adx14"),
                "rsi14": mrow.get("rsi14"),
                "macd": mrow.get("macd"),
                "macd_signal": mrow.get("macd_signal"),
                "ema_fast": mrow.get("ema_fast"),
                "ema_slow": mrow.get("ema_slow"),
                "price": mrow.get("price"),
            }
            w.writerow(rec)

    print(f"🧾 已写入日志(全量): {path}")


def compute_local_signal(market: dict):
    """
    返回: (symbol, side, score) 轻量信号，用于“是否触发 AI 决策”的事件驱动开关
    规则（简洁可调）：
      - 30m: ema_fast>ema_slow + macd>signal + adx>22 => 多 +2
      - 30m: ema_fast<ema_slow + macd<signal + adx>22 => 空 -2
      - 4h : 4h adx>20 且 4h ema_fast>ema_slow => 多 +0.5（反向 -0.5）
    """
    best = (None, "hold", 0.0)
    for sym, row in market.items():
        b = row
        ctx = (row.get("tf") or {}).get("4h", {})
        score = 0.0
        score += (1 if b.get("ema_fast") > b.get("ema_slow") else -1)
        score += (0.7 if b.get("macd") > b.get("macd_signal") else -0.7)
        if (b.get("adx14") or 0) > 22:  # 趋势强化
            score *= 1.2
        if ctx and (ctx.get("adx14") or 0) > 20:
            score += (0.5 if ctx.get("ema_fast") > ctx.get("ema_slow") else -0.5)
        side = "buy" if score >= 1.6 else ("sell" if score <= -1.6 else "hold")
        if abs(score) > abs(best[2]):
            best = (sym, side, score)
    return best

def _dynamic_ai_interval_secs(row: dict, ctx4h: dict=None, ctx3m: dict=None, in_pos: bool=False) -> int:
    """
    根据 3m/30m/4h 指标与持仓状态，返回下一次针对该币触发 AI 的动态秒数。
    """
    base = float(BASE_INTERVAL)

    # ✅ 提取多周期指标
    adx30 = float(row.get("adx14") or 0.0)
    rsi30 = float(row.get("rsi14") or 50.0)
    adx4h = float((ctx4h or {}).get("adx14") or 0.0)
    adx3m = float((ctx3m or {}).get("adx14") or 0.0)  # ✅ 新增
    rsi3m = float((ctx3m or {}).get("rsi14") or 50.0)  # ✅ 新增
    
    vol24 = _vol24_from_market_row(row) or 0.0

    # 1) ADX 综合评分（多周期加权）
    adx_combined = (adx3m * 0.3 + adx30 * 0.5 + adx4h * 0.2)  # ✅ 3m 权重 30%
    adx_factor = max(0.4, min(1.2, 1.2 - 0.02 * min(adx_combined, 50)))

    # 2) 波动率
    if vol24 >= 0.05:
        vol_factor = 1.4
    elif vol24 <= 0.02:
        vol_factor = 0.9
    else:
        vol_factor = 1.0

    # 3) RSI 极值（优先看 3m）
    if rsi3m >= 70 or rsi3m <= 30:
        rsi_factor = 0.7  # ✅ 3m 极值 → 高度关注
    elif rsi30 >= 70 or rsi30 <= 30:
        rsi_factor = 0.8  # 30m 极值
    elif 45 <= rsi30 <= 55:
        rsi_factor = 1.1  # 中性
    else:
        rsi_factor = 1.0

    # 4) 4h 背景趋势
    tf_factor = 0.9 if adx4h >= 25 else 1.0

    # 5) 持仓状态
    pos_factor = 0.7 if in_pos else 1.0

    # 6) 抖动
    jitter = 1.0 + (random.random() - 0.5) * 0.30

    sec = base * adx_factor * vol_factor * rsi_factor * tf_factor * pos_factor * jitter
    return int(max(MIN_AI_INTERVAL_SEC, min(MAX_AI_INTERVAL_SEC, sec)))

def _calculate_smart_leverage(
    ai_confidence: float,
    market_row: dict,
    consecutive_losses: int = 0,
    max_leverage: float = 10.0
) -> float:
    """
    多因素智能杠杆计算
    
    Args:
        ai_confidence: AI 置信度 (0.5-1.0)
        market_row: 市场数据（包含 ADX, RSI, 波动率等）
        consecutive_losses: 连续亏损次数
        max_leverage: 最大杠杆倍数（默认 10 倍）
    
    Returns:
        float: 最终杠杆倍数 (0.5-max_leverage)
    """
    # ===== 1. 基础杠杆（置信度驱动）=====
    # 置信度映射：0.5→1x, 0.65→2x, 0.8→4x, 0.95→8x
    if ai_confidence < 0.55:
        base_lev = 1.0
    elif ai_confidence < 0.7:
        base_lev = 1.0 + (ai_confidence - 0.55) / 0.15 * 1.0  # 1-2x
    elif ai_confidence < 0.85:
        base_lev = 2.0 + (ai_confidence - 0.7) / 0.15 * 2.0   # 2-4x
    else:
        base_lev = 4.0 + (ai_confidence - 0.85) / 0.15 * 4.0  # 4-8x
    
    # ===== 2. ADX 趋势调整（±50%）=====
    adx30 = float(market_row.get("adx14") or 0.0)
    tf_data = market_row.get("tf", {})
    adx4h = float(tf_data.get("4h", {}).get("adx14") or 0.0)
    adx3m = float(tf_data.get("3m", {}).get("adx14") or 0.0)
    
    # 多周期 ADX 综合（3m:30%, 30m:50%, 4h:20%）
    adx_combined = adx3m * 0.3 + adx30 * 0.5 + adx4h * 0.2
    
    if adx_combined < 15:
        adx_factor = 0.5  # 震荡市：杠杆减半
    elif adx_combined < 25:
        adx_factor = 0.7 + (adx_combined - 15) / 10 * 0.3  # 0.7-1.0
    elif adx_combined < 40:
        adx_factor = 1.0 + (adx_combined - 25) / 15 * 0.3  # 1.0-1.3
    else:
        adx_factor = 1.3 + min((adx_combined - 40) / 20 * 0.2, 0.2)  # 最高 1.5x
    
    # ===== 3. 波动率调整（±30%）=====
    vol24 = _vol24_from_market_row(market_row) or 0.0
    
    if vol24 < 0.02:
        vol_factor = 1.2  # 低波动：适度提升杠杆
    elif vol24 < 0.05:
        vol_factor = 1.0  # 正常波动
    elif vol24 < 0.10:
        vol_factor = 0.8  # 高波动：降低杠杆
    else:
        vol_factor = 0.6  # 极端波动：大幅降低
    
    # ===== 4. RSI 极值惩罚（-50%）=====
    rsi30 = float(market_row.get("rsi14") or 50.0)
    rsi3m = float(tf_data.get("3m", {}).get("rsi14") or 50.0)
    
    rsi_factor = 1.0
    if rsi3m >= 75 or rsi3m <= 25:
        rsi_factor = 0.5  # 3m 极端超买超卖：杠杆减半
    elif rsi30 >= 70 or rsi30 <= 30:
        rsi_factor = 0.7  # 30m 超买超卖：降低 30%
    
    # ===== ✅ 新增：5. MACD 趋势确认（±15%）=====
    macd = float(market_row.get("macd") or 0.0)
    macd_signal = float(market_row.get("macd_signal") or 0.0)

    macd_factor = 1.0
    if (macd > macd_signal) and (macd > 0):
        macd_factor = 1.15  # 金叉且在零轴上方：+15%
    elif (macd < macd_signal) and (macd < 0):
        macd_factor = 0.85  # 死叉且在零轴下方：-15%
    elif abs(macd - macd_signal) < abs(macd * 0.1):
        macd_factor = 0.95  # MACD 粘合（即将变盘）：-5%

    # ===== 5. 连败惩罚（每次 -0.5x）=====
    loss_penalty = max(0.5, 1.0 - consecutive_losses * 0.15)  # 最多降到 0.5x
    
    # ===== 6. 综合计算（加入 MACD）=====
    final_lev = base_lev * adx_factor * vol_factor * rsi_factor * macd_factor * loss_penalty
    
    # ===== 7. 限制范围 =====
    final_lev = max(0.5, min(max_leverage, final_lev))
    
    # ===== 8. 调试输出 =====
    print(f"\n{'='*70}")
    print(f"[智能杠杆计算]")
    print(f"  AI 置信度: {ai_confidence:.2f} → 基础杠杆: {base_lev:.2f}x")
    print(f"  ADX (3m/30m/4h): {adx3m:.1f}/{adx30:.1f}/{adx4h:.1f} → 综合 {adx_combined:.1f} → 系数 {adx_factor:.2f}x")
    print(f"  24h 波动率: {vol24:.2%} → 系数 {vol_factor:.2f}x")
    print(f"  RSI (3m/30m): {rsi3m:.1f}/{rsi30:.1f} → 系数 {rsi_factor:.2f}x")
    # ✅ 新增这一行
    print(f"  MACD 趋势: {macd:.4f} vs 信号 {macd_signal:.4f} → 系数 {macd_factor:.2f}x")
    print(f"  连续亏损: {consecutive_losses} 次 → 惩罚 {loss_penalty:.2f}x")
    print(f"  最终杠杆: {final_lev:.2f}x (上限 {max_leverage}x)")
    print(f"{'='*70}\n")
    
    return round(final_lev, 2)


def _calculate_smart_position(
    ai_confidence: float,
    market_row: dict,
    equity: float,
    consecutive_losses: int = 0,
    max_position_pct: float = 0.30  # 单笔最大 30% 资金
) -> float:
    """
    智能仓位计算（基于 Kelly 公式改进版）
    
    Args:
        ai_confidence: AI 置信度 (0.5-1.0)
        market_row: 市场数据（包含 ADX, RSI, 波动率等）
        equity: 当前账户权益（USDT）
        consecutive_losses: 连续亏损次数
        max_position_pct: 单笔最大资金比例（默认 30%）
    
    Returns:
        float: 建议仓位金额（USDT）
    """
    # ===== 1. 基础仓位比例（置信度驱动）=====
    if ai_confidence < 0.55:
        base_pct = 0.03  # 3%
    elif ai_confidence < 0.70:
        base_pct = 0.03 + (ai_confidence - 0.55) / 0.15 * 0.07  # 3%-10%
    elif ai_confidence < 0.85:
        base_pct = 0.10 + (ai_confidence - 0.70) / 0.15 * 0.10  # 10%-20%
    else:
        base_pct = 0.20 + (ai_confidence - 0.85) / 0.15 * 0.10  # 20%-30%
    
    # ===== 2. 波动率调整（币圈适配版）=====
    vol24 = _vol24_from_market_row(market_row) or 0.0
    if vol24 < 0.03:
        vol_factor = 1.2  # 低波动（<3%）+20%
    elif vol24 < 0.08:
        vol_factor = 1.0  # 正常波动（3%-8%）
    elif vol24 < 0.15:
        vol_factor = 0.8  # 高波动（8%-15%）-20%
    else:
        vol_factor = 0.6  # 极端波动（>15%）-40%
    
    
    # ===== 5. 连亏惩罚（每次 -20%）=====
    loss_penalty = max(0.3, 1.0 - consecutive_losses * 0.20)
    
    # ===== 3. 简化综合计算（只保留波动率和连亏惩罚）=====
    final_pct = base_pct * vol_factor * loss_penalty
    final_pct = max(0.01, min(max_position_pct, final_pct))  # 限制 1%-30%
    
    position_value = equity * final_pct
    
    # ===== 7. 调试输出 =====
    print(f"\n{'='*70}")
    print(f"[智能仓位计算（简化版）]")
    print(f"  账户权益: {equity:.2f} USDT")
    print(f"  置信度: {ai_confidence:.2f} → 基础比例: {base_pct:.2%}")
    print(f"  24h 波动率: {vol24:.2%} → 系数 {vol_factor:.2f}x")
    print(f"  连续亏损: {consecutive_losses} 次 → 惩罚 {loss_penalty:.2f}x")
    print(f"  ✅ 最终仓位: {position_value:.2f} USDT ({final_pct:.2%})")
    print(f"  📝 说明: ADX/RSI 调整已移至 AI 杠杆计算")
    print(f"{'='*70}\n")
    
    return round(position_value, 2)

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

    d = decision.get("decision", {})
    sym = d.get("symbol")

    # ✅ 关键修改：优先使用 AI 返回的置信度
    conf_raw = d.get("confidence")
    if conf_raw is not None:
        try:
            conf = float(conf_raw)
            # 确保在合理范围内
            conf = max(0.30, min(0.95, conf))
        except:
            conf = 0.55  # 解析失败才用默认值
            print(f"⚠️ [置信度解析失败] 使用默认值 0.55")
    else:
        conf = 0.55  # AI 完全没返回
        print(f"⚠️ [置信度缺失] AI 未返回置信度，使用默认值 0.55")

    # ✅ 新增：日志输出（调试用）
    print(f"🎯 [AI 置信度] {conf:.2f} (原始值: {conf_raw}, 类型: {type(conf_raw).__name__})")

    lev = d.get("leverage")

    # ✅ 使用智能杠杆系统
    if lev is None and sym in market:
        # 获取风控状态（连续亏损次数）
        cfg = RiskConfig(symbol_rules=SYMBOL_RULES)
        rm = RiskManager(cfg)
        consecutive_losses = getattr(rm.state, "consecutive_losses", 0)
        
        # ✅ 新增：调用前打印输入参数
        print(f"📊 [杠杆计算输入] 置信度={conf:.2f}, 币种={sym}, 连败={consecutive_losses}")
        
        # 调用智能杠杆函数
        lev = _calculate_smart_leverage(
            ai_confidence=conf,
            market_row=market.get(sym, {}),
            consecutive_losses=consecutive_losses,
            max_leverage=25.0
        )
        
        # ✅ 新增：调用后打印结果
        print(f"📈 [杠杆计算结果] {lev:.2f}x")
    else:
        lev = float(lev or 1.0)  # 如果 DeepSeek 返回了杠杆，优先使用

    d["leverage"] = round(float(lev), 2)
    progress.substep(f"📈 智能杠杆: {d['leverage']:.2f}x (置信度 {conf:.2f})")

    ai_time = time.time() - start_ai
    
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
        row = market.get(sym, {})
        
        # ✅ 根据市场状态动态计算 HOLD 的置信度
        adx = float(row.get("adx14") or 0.0)
        rsi = float(row.get("rsi14") or 50.0)
        
        hold_confidence = 0.5  # 默认
        if adx < 15:  # 震荡市
            hold_confidence = 0.6
        elif 45 <= rsi <= 55:  # RSI 中性
            hold_confidence = 0.55
        
        td[coin] = {
            "trade_signal_args": {
                "coin": coin,
                "signal": "hold",
                "quantity": _pos_qty(balance, sym),
                "leverage": (balance.get("positions") or {}).get(sym, {}).get("leverage", None),
                "confidence": hold_confidence,  # ✅ 动态置信度
                "ai_reason": _reason_explain_from_indicators(row)  # ✅ 本地理由
            }
        }

    # 4) 应用 AI 决策
    if isinstance(decision, dict) and "decision" in decision:
        d = decision["decision"]
        sym = d.get("symbol")
        side = d.get("side")
        
        if sym in SYMBOLS and side in ("buy", "sell"):
            progress.substep(f"AI 建议: {side.upper()} {sym}")
                    
            # ===== ✅ 新增：计算智能仓位 =====
            equity = float(bal_snapshot["USDT"]["available"])
            consecutive_losses = getattr(rm.state, "consecutive_losses", 0)

            # ✅ 新增：调用前打印输入参数
            print(f"💰 [仓位计算输入] 置信度={conf:.2f}, 权益={equity:.2f}, 连败={consecutive_losses}")

            position_value = _calculate_smart_position(
                ai_confidence=conf,
                market_row=market.get(sym, {}),
                equity=equity,
                consecutive_losses=consecutive_losses,
                max_position_pct=0.30
            )

            # ✅ 新增：调用后打印结果
            print(f"💵 [仓位计算结果] {position_value:.2f} USDT")
                    
            # ===== ✅ 覆盖 AI 的 size =====
            ai_size = d.get("size")
            if ai_size:
                # AI 返回了 size，计算对应的金额
                price = float(market[sym]["price"])
                ai_value = float(ai_size) * price
                # 取本地计算和 AI 建议的较小值（保守策略）
                final_value = min(position_value, ai_value)
                progress.substep(f"  AI size={ai_size:.4f} ({ai_value:.2f} USDT), 本地={position_value:.2f}, 取较小值={final_value:.2f}")
            else:
                final_value = position_value
                progress.substep(f"  AI 未返回 size，使用本地计算: {final_value:.2f} USDT")
                    
            # ===== ✅ 更新决策中的 size =====
            price = float(market[sym]["price"])
            d["size"] = final_value / price
            progress.substep(f"  最终下单数量: {d['size']:.6f} {sym.split('-')[0]}")
                    
            # 风控检查（现在用的是本地计算的 size）
            approved, order, reason = rm.pre_trade_checks(decision, market, balance)
            
            equity = rm._estimate_equity()
            progress.substep(f"风控结果: {'✅ 通过' if approved else '❌ 拒绝'} | 原因: {reason} | 权益: {equity:.2f}")
            
            coin = sym.split("-")[0]

            ai_reason = (d.get("rationale") or d.get("reason"))
            
            # ✅ 新增：处理 exit_plan
            exit_plan = d.get("exit_plan") or {}
            
            # 兼容两种字段名
            stop_loss = exit_plan.get("stop_loss_pct") or exit_plan.get("stop_loss") or \
                        (d.get("risk") or {}).get("stop_loss_pct")
                        
            take_profit = exit_plan.get("take_profit_pct") or exit_plan.get("profit_target") or \
                        (d.get("risk") or {}).get("take_profit_pct")

            invalidation = exit_plan.get("invalidation_condition") or "无"

            if approved and order and float(order.get("size", 0)) > 0:
                progress.success(f"生成订单: {side} {order['size']} {sym}")
                
                td[coin]["trade_signal_args"].update({
                    "signal": side,
                    "quantity": float(order["size"]) if approved and order else _pos_qty(balance, sym),
                    "order_type": order.get("order_type", d.get("order_type","market")),
                    "limit_price": order.get("limit_price"),
                    "max_slippage_bps": d.get("max_slippage_bps"),
                    "confidence": d.get("confidence"),
                    "leverage": d.get("leverage"),  # ✅ 新增这一行！
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "invalidation_condition": invalidation,
                    "ai_reason": ai_reason
                })
            else:
                # ❌ 这里也要加！
                td[coin]["trade_signal_args"].update({
                    "confidence": d.get("confidence"),
                    "leverage": d.get("leverage"),  # ✅ 新增这一行！
                    "ai_reason": ai_reason,
                    "note": f"risk_blocked: {reason}"
                })

    _save_run_state(st)

    # ✅ 把本次决策写入日志
    if isinstance(decision, dict) and "decision" in decision:
        _log_decision_to_csv(decision, meta, market)
        _log_all_signals_to_csv(td, market)  # <== 新增：记录所有币
        
    progress.success("决策生成完成")
    return td

# --------------------- 主流程（单次执行） ---------------------
def main_once(market: dict = None, balance: dict = None): 
    # ✅ 只在没有传入数据时才获取
    if market is None:
        market = fetch_market()
    if balance is None:
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

    # ✅ 传递 market 参数
    ok, skipped, logged = route_trading_decisions(decisions, market=market)
    
    # ✅ 新增：记录活跃持仓
    if ok > 0:  # 有成功下单
        positions = []
        for sym in SYMBOLS:
            coin = sym.split("-")[0]
            args = decisions.get(coin, {}).get("trade_signal_args", {})
            
            # 只记录 buy/entry 信号
            if args.get("signal") in ("entry", "buy"):
                exit_plan = {}
                if "exit_plan" in decisions.get(coin, {}):
                    exit_plan = decisions[coin]["exit_plan"]
                else:
                    # 从 args 中提取
                    exit_plan = {
                        "stop_loss_pct": args.get("stop_loss"),
                        "take_profit_pct": args.get("take_profit"),
                        "invalidation_condition": args.get("invalidation_condition", "")
                    }
                
                positions.append({
                    "symbol": sym,
                    "side": "buy",
                    "entry_price": market.get(sym, {}).get("price"),
                    "size": args.get("quantity"),
                    "entry_time": datetime.now().isoformat(),
                    "exit_plan": exit_plan
                })
        
        if positions:
            from exit_plan_monitor import save_positions
            save_positions(positions)
            print(f"✅ 已记录 {len(positions)} 个持仓到监控器")
    
    print(f"[ai_trader] orders_ok={ok}, skipped={skipped}, logged={logged}")       
        

if __name__ == "__main__":
    # 平衡
    BASE_INTERVAL = 10 * 60       # 10 分钟 = 600 秒
    MIN_AI_INTERVAL_SEC = 3 * 60  # 3 分钟 = 180 秒
    MAX_AI_INTERVAL_SEC = 30 * 60 # 30 分钟 = 1800 秒


    last_sig = None
    last_call_ts = 0

    # === 打印当前运行模式 ===
    from urllib.parse import urlparse
    # 这里假设你 fetch_market() 或 config 里定义了 MARKET_URL
    MARKET_URL = "http://127.0.0.1:5001/market"  # 若你已有这个变量可删
    def detect_mode():
        url = MARKET_URL.lower()
        if "127.0.0.1" in url or "localhost" in url:
            return "🧩 当前运行环境：Mock 模式（本地模拟）"
        elif "okx.com" in url or "binance.com" in url:
            return "🚀 当前运行环境：实盘模式（交易所 API）"
        else:
            return "⚙️ 当前运行环境：未知/测试模式"
    print(detect_mode())

    while True:
        try:
            market = fetch_market()
            balance = fetch_balance()

            # === 轻量信号检测 ===
            sym, side, score = compute_local_signal(market)
            sig = f"{sym}:{side}:{round(score,2)}"
            now = time.time()

            # ✅ 新增：检测是否有持仓
            has_position = (_pos_qty(balance, sym) > 0)  # 使用已有的函数

            # ✅ 动态计算下次触发间隔（使用 3m + 30m + 4h 自适应函数）
            tf = market.get(sym, {}).get("tf", {})
            ctx3m = tf.get("3m")  # ✅ 新增
            ctx4h = tf.get("4h")

            dyn_interval = _dynamic_ai_interval_secs(
                market.get(sym, {}), 
                ctx4h=ctx4h,
                ctx3m=ctx3m,  # ✅ 新增参数
                in_pos=has_position
            )

            progress.substep(
                f"[事件检测] signal={sig}, 下次AI间隔≈{int(dyn_interval)}秒 "
                f"(≈{dyn_interval/60:.1f}分钟)"
            )

            # === 判断是否触发 AI 决策 ===
            need_call = (sig != last_sig) or ((now - last_call_ts) > dyn_interval)
            recently_called = (now - last_call_ts) < (dyn_interval * 0.5)

            progress.substep(
                f"上次触发距今 {int(now - last_call_ts)} 秒, "
                f"need_call={need_call}, recently_called={recently_called}"
            )

            if need_call and not recently_called:
                progress.substep("🔔 触发 AI 决策（自适应节奏）")
                main_once(market, balance) 
                last_call_ts = now
                last_sig = sig
            else:
                progress.substep("⏳ 未触发条件，继续监听...")

        except Exception as e:
            progress.error(f"主循环异常: {e}")

        # 每分钟检查一次触发条件
        time.sleep(60)