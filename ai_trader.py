# -*- coding: utf-8 -*-
# ai_trader.py — DeepSeek 决策 → RiskManager → /order（Bridge）
import os, json, time, urllib.request
from datetime import datetime, timezone
from deepseek_client import get_decision
from risk_manager import RiskManager, RiskConfig, SymbolRule

import random
import talib
import numpy as np
import pandas as pd
import csv
from pathlib import Path

LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
AI_STATUS_FILE = LOG_DIR / "ai_status.json"


# ====== 自适应触发节奏（可调） ======
DYN_BASE_SEC = 45 * 60        # 基准间隔 45 分钟
DYN_MIN_SEC  = 5 * 60         # 最短 5 分钟（再短易抖动）
DYN_MAX_SEC  = 2 * 60 * 60    # 最长 2 小时（避免过久不评估）

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


def _indicators_from_candles(candles_arr):
    """
    candles_arr: [[open,high,low,close,volume], ...] 旧->新
    输出：一套 EMA/RSI/ATR/MACD/ADX/BOLL 指标（最后一根）
    """
    import numpy as np, talib
    closes = np.array([c[3] for c in candles_arr], dtype=float)
    highs  = np.array([c[1] for c in candles_arr], dtype=float)
    lows   = np.array([c[2] for c in candles_arr], dtype=float)

    ema_fast = float(np.nan_to_num(talib.EMA(closes, timeperiod=12)[-1]))
    ema_slow = float(np.nan_to_num(talib.EMA(closes, timeperiod=48)[-1]))
    rsi14    = float(np.nan_to_num(talib.RSI(closes, timeperiod=14)[-1]))
    atr14    = float(np.nan_to_num(talib.ATR(highs, lows, closes, timeperiod=14)[-1]))
    macd, macd_signal, _ = talib.MACD(closes, fastperiod=12, slowperiod=26, signalperiod=9)
    macd = float(np.nan_to_num(macd[-1])); macd_signal = float(np.nan_to_num(macd_signal[-1]))
    adx14 = float(np.nan_to_num(talib.ADX(highs, lows, closes, timeperiod=14)[-1]))
    bu, bm, bl = talib.BBANDS(closes, timeperiod=20, nbdevup=2, nbdevdn=2, matype=0)
    boll_upper = float(np.nan_to_num(bu[-1])); boll_mid=float(np.nan_to_num(bm[-1])); boll_lower=float(np.nan_to_num(bl[-1]))
    return dict(ema_fast=ema_fast, ema_slow=ema_slow, rsi14=rsi14, atr14=atr14,
                macd=macd, macd_signal=macd_signal, adx14=adx14,
                boll_upper=boll_upper, boll_mid=boll_mid, boll_lower=boll_lower)

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

        # 兼容两种返回：1) 列表 2) 多周期映射 {"30m":[...], "4h":[...]}
        candles_raw = v.get("candles")
        c30, c4h = None, None
        if isinstance(candles_raw, dict):
            c30 = candles_raw.get("30m")
            c4h = candles_raw.get("4h")
        elif isinstance(candles_raw, (list, tuple)):
            c30 = candles_raw  # 兼容旧结构：只有一套 K 线

        # 兜底：若 30m 不足，构造一段平滑序列防止 talib 报错
        if not c30 or len(c30) < 60:
            import numpy as np
            closes = np.array([price*(1+0.01*np.sin(i/8)) for i in range(120)], dtype=float)
            c30 = [[closes[i], closes[i]*1.01, closes[i]*0.99, closes[i], 1.0] for i in range(len(closes))]
            progress.warning(f"{s} 缺少 30m candles，使用模拟序列兜底")

        # 兜底：若没有 4h，先用 30m 做个粗聚合（30m×8≈4h）
        if not c4h or len(c4h) < 60:
            c4h = c30[::8]

        # === 指标计算（正确使用 [o,h,l,c,vol] 的索引） ===
        base30 = _indicators_from_candles(c30)
        ctx4h  = _indicators_from_candles(c4h) if c4h else None

        # === 调试输出 ===
        if ctx4h:
            progress.substep(
                f"{s} | 30m: RSI={base30['rsi14']:.1f}, MACD={base30['macd']:.4f}, ADX={base30['adx14']:.1f} | "
                f"4h: RSI={ctx4h['rsi14']:.1f}, ADX={ctx4h['adx14']:.1f}"
            )
        else:
            progress.substep(
                f"{s} | 30m: RSI={base30['rsi14']:.1f}, MACD={base30['macd']:.4f}, ADX={base30['adx14']:.1f}"
            )

        # === 汇总（30m 扁平 + 可选的 4h 背景） ===
        row = {
            "price": price,
            "last":  price,
            "high24h": float(v.get("high24h") or 0.0),
            "low24h":  float(v.get("low24h")  or 0.0),
            **base30
        }
        if ctx4h:
            row["tf"] = {"4h": ctx4h}

        m[s] = row

    progress.success(f"获取到 {len(m)} 个交易对 (含 30m & 4h 指标)")
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
        "ts","symbol","side","confidence","rationale",
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

def _dynamic_ai_interval_secs(row: dict, ctx4h: dict=None, in_pos: bool=False) -> int:
    """
    根据 30m/4h 指标与持仓状态，返回下一次针对该币触发 AI 的动态秒数。
    规则（乘法叠加，最后夹在 [DYN_MIN_SEC, DYN_MAX_SEC]）：
      - 趋势强(ADX↑) → 缩短
      - 波动大(vol24↑) → 拉长（避免追涨杀跌）
      - RSI 极值(>70 或 <30) → 缩短（更关注极端）
      - 4h 强势 → 略缩短
      - 有持仓 → 明显缩短（更密切盯盘）
      - 加 ±15% 抖动，避免固定节奏
    """
    base = float(DYN_BASE_SEC)

    adx   = float(row.get("adx14") or 0.0)
    rsi   = float(row.get("rsi14") or 50.0)
    vol24 = _vol24_from_market_row(row) or 0.0  # (high-low)/last
    adx4h = float((ctx4h or {}).get("adx14") or 0.0)

    # 1) ADX：0~50 常见，越大越强，区间映射到 0.4~1.2
    adx_factor = max(0.4, min(1.2, 1.2 - 0.02 * min(adx, 50)))  # adx=30 → 0.6，adx=40 → 0.4

    # 2) 波动率：>5% 拉长 1.4；<2% 略缩 0.9
    if vol24 >= 0.05:
        vol_factor = 1.4
    elif vol24 <= 0.02:
        vol_factor = 0.9
    else:
        vol_factor = 1.0

    # 3) RSI 极值/中性
    if rsi >= 70 or rsi <= 30:
        rsi_factor = 0.8   # 极端 → 关注更密
    elif 45 <= rsi <= 55:
        rsi_factor = 1.1   # 中性 → 放宽一点
    else:
        rsi_factor = 1.0

    # 4) 4h 背景趋势：强则再缩短一点
    tf_factor = 0.9 if adx4h >= 25 else 1.0

    # 5) 有持仓则更密（例如 0.7）
    pos_factor = 0.7 if in_pos else 1.0

    # 6) 少许抖动 ±15%
    jitter = 1.0 + (random.random() - 0.5) * 0.30

    sec = base * adx_factor * vol_factor * rsi_factor * tf_factor * pos_factor * jitter
    return int(max(DYN_MIN_SEC, min(DYN_MAX_SEC, sec)))


def _log_decision_to_csv(decision: dict, meta: dict, market: dict, log_dir="logs"):
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
    conf = float(d.get("confidence") or 0.5)
    lev  = d.get("leverage")
    if lev is None:
        base = 1.0 + (conf - 0.5) * 8.0
        lev  = max(0.5, min(25.0, base))
    d["leverage"] = round(float(lev), 2)
    progress.substep(f"📈 自动分配杠杆: {d['leverage']:.2f}x（置信度 {conf:.2f}）")

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

    # ✅ 把本次决策写入日志
    if isinstance(decision, dict) and "decision" in decision:
        _log_decision_to_csv(decision, meta, market)
        
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
    BASE_INTERVAL = 30 * 60       # 平均间隔 30 分钟
    MIN_AI_INTERVAL_SEC = 5 * 60  # 最快 5 分钟
    MAX_AI_INTERVAL_SEC = 2 * 60 * 60  # 最慢 2 小时
    last_sig = None
    last_call_ts = 0

    while True:
        try:
            market = fetch_market()
            balance = fetch_balance()

            sym, side, score = compute_local_signal(market)
            sig = f"{sym}:{side}:{round(score,2)}"
            now = time.time()

            # === 动态计算下次触发间隔 ===
            adx = market.get(sym, {}).get("adx14", 20)
            rsi = market.get(sym, {}).get("rsi14", 50)
            # 趋势强 or RSI 极端 → 缩短；震荡 → 拉长
            factor = 0.7 if (adx > 30 or rsi > 70 or rsi < 30) else (1.3 if adx < 20 else 1.0)
            jitter = 1.0 + (random.random() - 0.5) * 0.3
            dyn_interval = max(MIN_AI_INTERVAL_SEC, min(MAX_AI_INTERVAL_SEC, BASE_INTERVAL * factor * jitter))

            need_call = (sig != last_sig) or ((now - last_call_ts) > dyn_interval)
            recently_called = (now - last_call_ts) < (dyn_interval * 0.5)

            progress.substep(
                f"[事件检测] signal={sig}, dyn_interval={int(dyn_interval)}s, "
                f"last_call={int(now - last_call_ts)}s ago, need_call={need_call}"
            )

            if need_call and not recently_called:
                progress.substep("🔔 触发 AI 决策（自适应节奏）")
                main_once()
                last_call_ts = now
                last_sig = sig
            else:
                progress.substep("⏳ 未触发条件，继续监听...")

        except Exception as e:
            progress.error(f"主循环异常: {e}")

        time.sleep(60)