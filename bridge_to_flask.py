# -*- coding: utf-8 -*-
# bridge_to_flask.py — 将 TRADING_DECISIONS JSON 直接接到 Flask /order
import os, json, time, hashlib, pathlib
from typing import Dict, Any, Tuple
from datetime import datetime, timezone

FLASK_BASE = os.getenv("FLASK_BASE_URL", "http://127.0.0.1:5001")
LOG_DIR = pathlib.Path(os.getenv("LOG_DIR", "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
SIGNAL_LOG = LOG_DIR / "recent_signals.jsonl"
TRADE_LOG  = LOG_DIR / "recent_trades.jsonl"
IDEMP_FILE = LOG_DIR / "idempotency_keys.json"

# ✅ 新增：持仓历史文件
POSITION_HISTORY = LOG_DIR / "position_history.json"


# ✅ 新增：限制 JSONL 文件行数的函数
def _append_jsonl_limited(path: pathlib.Path, data: dict, max_lines: int = 100):
    """
    写入 JSONL 并自动限制行数
    
    Args:
        path: 文件路径
        data: 要写入的字典
        max_lines: 最多保留的行数（默认 100）
    """
    lines = []
    
    # 读取现有内容
    if path.exists():
        try:
            content = path.read_text(encoding="utf-8").strip()
            if content:
                lines = content.splitlines()
        except Exception as e:
            print(f"⚠️ 读取 {path.name} 失败: {e}")
    
    # 添加新行
    lines.append(json.dumps(data, ensure_ascii=False))
    
    # 只保留最新的 N 行
    lines = lines[-max_lines:]
    
    # 写回文件
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        print(f"❌ 写入 {path.name} 失败: {e}")

def _http_get(path: str) -> Dict[str, Any]:
    url = f"{FLASK_BASE}{path}"
    try:
        import requests
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:
        # 兜底：urllib
        import urllib.request, json as _json
        with urllib.request.urlopen(url, timeout=15) as resp:
            return _json.loads(resp.read().decode("utf-8"))

def _http_post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{FLASK_BASE}{path}"
    try:
        import requests
        r = requests.post(url, json=payload, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception:
        import urllib.request, json as _json
        data = _json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return _json.loads(resp.read().decode("utf-8"))

def _load_idemp() -> set:
    if IDEMP_FILE.exists():
        try:
            return set(json.load(open(IDEMP_FILE, "r", encoding="utf-8")))
        except Exception:
            return set()
    return set()

def _save_idemp(keys: set):
    # ✅ 限制幂等键数量为最近 500 个
    MAX_KEYS = 500
    keys_list = sorted(list(keys))
    keys_list = keys_list[-MAX_KEYS:]  # 只保留最新的 500 个
    
    json.dump(keys_list, open(IDEMP_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def _symbol_of(coin: str) -> str:
    # 统一映射：ETH -> ETH-USDT
    return f"{coin.upper()}-USDT"

def _side_for(signal: str) -> str:
    s = signal.lower()
    # ✅ 新代码：直接返回标准化后的值
    if s in {"buy", "long", "open"}:
        return "buy"
    if s in {"sell", "short"}:
        return "sell"
    return "hold"  # 包括 hold 和任何未识别的值

def _mk_idempotency_key(coin: str, signal: str, ts_str: str) -> str:
    raw = f"{coin}|{signal}|{ts_str}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

def _current_position_size(coin: str) -> float:
    """
    尝试从 /balance 读到当前持仓数量（若你的 /balance 返回 positions）
    结构示例（自行适配）：{"positions":{"ETH-USDT":{"qty":3.11,"side":"long"}}}
    """
    try:
        b = _http_get("/balance")
        pos = (b.get("positions") or {}).get(_symbol_of(coin), {})
        qty = float(pos.get("qty", 0.0))
        return qty
    except Exception:
        return 0.0

# ===== ✅ 新增：持仓时长追踪函数 =====
def _load_position_history() -> list:
    """加载历史持仓记录"""
    if not POSITION_HISTORY.exists():
        return []
    try:
        return json.loads(POSITION_HISTORY.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️ 加载持仓历史失败: {e}")
        return []

def _save_position_history(history: list):
    """保存持仓历史（限制最多 500 条）"""
    MAX_HISTORY = 500
    history = history[-MAX_HISTORY:]  # 只保留最近 500 条
    try:
        POSITION_HISTORY.write_text(
            json.dumps(history, ensure_ascii=False, indent=2), 
            encoding="utf-8"
        )
    except Exception as e:
        print(f"❌ 保存持仓历史失败: {e}")

def _record_position_open(symbol: str, side: str, size: float, price: float, 
                          args: Dict[str, Any], meta: Dict[str, Any]):
    """
    记录开仓（BUY 信号触发时）
    
    Args:
        symbol: 交易对（如 BTC-USDT）
        side: 方向（buy/sell）
        size: 数量
        price: 入场价格
        args: 交易参数（包含 stop_loss, take_profit 等）
        meta: 元数据（包含时间戳）
    """
    # 从 active_positions.json 获取现有持仓（避免重复记录）
    from exit_plan_monitor import load_positions, save_positions
    
    active = load_positions()
    
    # 检查是否已存在该持仓
    for pos in active:
        if pos.get("symbol") == symbol and pos.get("side") == side:
            print(f"ℹ️ {symbol} 持仓已存在，跳过重复记录")
            return
    
    # 构造新持仓记录
    position = {
        "symbol": symbol,
        "side": side,
        "entry_price": price,
        "size": size,
        "entry_time": meta.get("current_time") or datetime.now(timezone.utc).isoformat(),
        "leverage": args.get("leverage"),
        "confidence": args.get("confidence"),
        "exit_plan": {
            "stop_loss_pct": args.get("stop_loss") or args.get("stop_loss_pct"),
            "take_profit_pct": args.get("take_profit") or args.get("take_profit_pct"),
            "invalidation_condition": args.get("invalidation_condition", "")
        }
    }
    
    # 添加到活跃持仓
    active.append(position)
    save_positions(active)
    
    print(f"✅ 记录 {symbol} 开仓: {size} @ {price}")

def _record_position_close(symbol: str, exit_price: float, meta: Dict[str, Any], 
                           exit_reason: str = "AI决策平仓"):
    """
    记录平仓（SELL 信号触发时）
    
    Args:
        symbol: 交易对
        exit_price: 平仓价格
        meta: 元数据
        exit_reason: 平仓原因
    """
    from exit_plan_monitor import load_positions, save_positions
    
    active = load_positions()
    history = _load_position_history()
    
    # 查找匹配的持仓
    matched = None
    remaining = []
    
    for pos in active:
        if pos.get("symbol") == symbol:
            matched = pos
        else:
            remaining.append(pos)
    
    if not matched:
        print(f"⚠️ 未找到 {symbol} 的活跃持仓，无法记录平仓")
        return
    
    # 计算持仓时长
    try:
        entry_time = datetime.fromisoformat(matched["entry_time"].replace("Z", "+00:00"))
        exit_time = datetime.now(timezone.utc)
        duration_hours = (exit_time - entry_time).total_seconds() / 3600
    except Exception as e:
        print(f"⚠️ 时长计算失败: {e}")
        duration_hours = 0.0
    
    # 计算收益率
    entry_price = matched.get("entry_price")
    profit_pct = None
    if entry_price and exit_price:
        if matched.get("side") == "buy":
            profit_pct = ((exit_price - entry_price) / entry_price) * 100
        else:  # sell/short
            profit_pct = ((entry_price - exit_price) / entry_price) * 100
        
        # 考虑杠杆
        leverage = matched.get("leverage") or 1.0
        profit_pct *= float(leverage)
    
    # 构造历史记录
    record = {
        **matched,  # 包含 entry_price, size, exit_plan 等
        "exit_time": exit_time.isoformat(),
        "exit_price": exit_price,
        "profit_pct": round(profit_pct, 2) if profit_pct is not None else None,
        "exit_reason": exit_reason,
        "duration_hours": round(duration_hours, 2),
        "duration_days": round(duration_hours / 24, 2),
        "account_value": meta.get("account_value")
    }
    
    # 保存到历史
    history.append(record)
    _save_position_history(history)
    
    # 更新活跃持仓（移除已平仓）
    save_positions(remaining)
    
    print(f"✅ 记录 {symbol} 平仓: 持仓 {duration_hours:.2f}h, 收益 {profit_pct or 0:.2f}%")

def _log_signal(meta: Dict[str, Any], coin: str, args: Dict[str, Any], 
                extra: Dict[str, Any], market: Dict[str, Any] = None):  # ✅ 新增 market 参数
    """
    记录交易信号到日志
    
    Args:
        meta: 元数据（时间、权益等）
        coin: 币种（如 BTC）
        args: 交易参数
        extra: 额外信息
        market: 市场数据（包含价格）  # ✅ 新增
    """
    raw_signal = args.get("signal", "hold")
    if isinstance(raw_signal, str):
        raw_signal = raw_signal.lower()
    
    side = "buy" if raw_signal in ("entry", "buy", "open", "long") else \
           "sell" if raw_signal in ("close", "exit", "sell", "short") else \
           "hold"
    
    # ✅ 从 market 提取价格
    symbol = _symbol_of(coin)
    price = None
    if market and symbol in market:
        price = market[symbol].get("price") or market[symbol].get("last")
    
    rec = {
        "ts": meta.get("current_time"),
        "runtime_minutes": meta.get("runtime_minutes"),
        "invocations": meta.get("invocations"),
        "account_value": meta.get("account_value"),
        "available_cash": meta.get("available_cash"),
        "coin": coin,
        "symbol": symbol,
        "side": side,
        "price": float(price) if price else None,  # ✅ 新增价格字段
        **args,
        **extra,
    }
    _append_jsonl_limited(SIGNAL_LOG, rec, max_lines=100)

def _log_trade(meta: Dict[str, Any], req: Dict[str, Any], resp: Dict[str, Any]):
    """
    扩展交易日志：记录仓位、置信度、成交价等信息
    """
    try:
        # 兼容不同响应格式
        filled_price = None
        pnl = None
        if isinstance(resp, dict):
            data = resp.get("data") or []
            if isinstance(data, list) and data:
                filled_price = data[0].get("price") or data[0].get("fillPx")
                pnl = data[0].get("pnl") or data[0].get("profit")
        rec = {
            "ts": meta.get("current_time"),
            "coin": req.get("symbol", "").split("-")[0],
            "symbol": req.get("symbol"),
            "side": req.get("side"),
            "size": float(req.get("size") or 0),
            "order_type": req.get("order_type", "market"),
            "limit_price": req.get("limit_price"),
            # ✅ 新增字段
            "leverage": req.get("leverage") or meta.get("leverage"),
            "confidence": req.get("confidence") or meta.get("confidence"),
            "filled_price": filled_price,
            "account_value": meta.get("account_value"),
            "pnl": pnl,
            "flask_response": resp
        }
        _append_jsonl_limited(TRADE_LOG, rec, max_lines=50)  # ✅ 限制 50 条
    except Exception as e:
        print(f"[log_trade] error: {e}")


def _prepare_order(symbol: str, side: str, args: Dict[str, Any]) -> Dict[str, Any]:
    order_type = args.get("order_type", "market")
    payload = {
        "symbol": symbol,
        "side": side,                     # buy / sell
        "size": float(args.get("quantity", 0.0)) or 0.0,
        "order_type": order_type
    }
    if order_type == "limit":
        payload["price"] = float(args.get("limit_price"))
    # 兼容 max_slippage_bps（若后端支持，可一起传）
    if "max_slippage_bps" in args:
        payload["max_slippage_bps"] = args["max_slippage_bps"]
    return payload

def route_trading_decisions(decisions: Dict[str, Any], 
                           market: Dict[str, Any] = None) -> Tuple[int,int,int]:  # ✅ 新增参数
    """
    将 TRADING_DECISIONS JSON 直接接到 Flask /order。
    返回 (下单成功数, 拒绝/跳过数, 记录数)
    
    Args:
        decisions: AI 决策 JSON
        market: 市场数据（包含价格）  # ✅ 新增
    """
    meta = decisions.get("meta") or {}
    idemp = _load_idemp()

    ok_orders = 0
    skipped = 0
    logged = 0

    for coin, body in decisions.items():
        if coin == "meta":
            continue
        args = (body.get("trade_signal_args") or {})
        signal = str(args.get("signal","hold")).lower()
        side = _side_for(signal)
        ts = meta.get("current_time") or time.strftime("%Y-%m-%d %H:%M:%S")

        # ✅ 提取 AI 理由作为主要 reason
        ai_reason = args.get("ai_reason") or args.get("rationale") or ""

        # 构造详细的 reason（含止盈止损信息）
        if side != "hold":
            tp = args.get("take_profit") or args.get("take_profit_pct")
            sl = args.get("stop_loss") or args.get("stop_loss_pct")
            inv = args.get("invalidation_condition", "")
            
            # 格式化止盈止损
            tp_str = f"{tp*100:.2f}%" if tp else "未设置"
            sl_str = f"{sl*100:.2f}%" if sl else "未设置"
            inv_str = inv if inv else "无"
            
            # 拼接完整理由
            reason_detail = f"{ai_reason}\n📈 TP:{tp_str} · SL:{sl_str} · 失效:{inv_str}"
        else:
            reason_detail = ai_reason or "观望中"

        # 统一日志（所有信号）
        _log_signal(meta, coin, {
            "signal": signal,
            **args,
            "reason": reason_detail,  # ✅ 使用完整理由
            "confidence": float(args.get("confidence", 0.5))
        }, extra={}, market=market)
        logged += 1

        # HOLD：仅记录，不下单
        if side == "hold":
            skipped += 1
            continue

        # 幂等检查
        idem_key = _mk_idempotency_key(coin, signal, ts)
        if idem_key in idemp:
            skipped += 1
            continue

        symbol = _symbol_of(coin)

        # close/exit 若 quantity 未给，自动用当前持仓数
        if side == "sell" and (not args.get("quantity") or args.get("quantity") <= 0):
            qty_now = _current_position_size(coin)
            args["quantity"] = qty_now

        order_req = _prepare_order(symbol, side, args)

        # 仅在 size > 0 时下单
        if float(order_req.get("size",0)) <= 0:
            skipped += 1
            continue

        try:
            resp = _http_post("/order", order_req)
            _log_trade(meta, order_req, resp)
            
            # ===== ✅ 新增：根据方向记录开仓/平仓 =====
            filled_price = None
            if isinstance(resp, dict):
                data = resp.get("data") or []
                if isinstance(data, list) and data:
                    filled_price = data[0].get("price") or data[0].get("fillPx")
            
            if side == "buy":
                # 开仓
                _record_position_open(
                    symbol=symbol,
                    side=side,
                    size=float(order_req.get("size")),
                    price=filled_price or 0.0,  # 优先用成交价，否则为 0
                    args=args,
                    meta=meta
                )
            elif side == "sell":
                # 平仓
                _record_position_close(
                    symbol=symbol,
                    exit_price=filled_price or 0.0,
                    meta=meta,
                    exit_reason="AI决策平仓"
                )
            
            idemp.add(idem_key)
            ok_orders += 1
        except Exception as e:
            _log_trade(meta, order_req, {"status":"error","message":str(e)})
            skipped += 1

    _save_idemp(idemp)
    return ok_orders, skipped, logged

if __name__ == "__main__":
    # 支持直接传文件运行：python bridge_to_flask.py decisions.json
    import sys
    if len(sys.argv) >= 2:
        decisions = json.load(open(sys.argv[1], "r", encoding="utf-8"))
        ok, sk, lg = route_trading_decisions(decisions)
        print(f"orders_ok={ok}, skipped={sk}, logged={lg}")