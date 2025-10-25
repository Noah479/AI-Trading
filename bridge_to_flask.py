# -*- coding: utf-8 -*-
# bridge_to_flask.py — 将 TRADING_DECISIONS JSON 直接接到 Flask /order
import os, json, time, hashlib, pathlib
from typing import Dict, Any, Tuple

FLASK_BASE = os.getenv("FLASK_BASE_URL", "http://127.0.0.1:5001")
LOG_DIR = pathlib.Path(os.getenv("LOG_DIR", "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
SIGNAL_LOG = LOG_DIR / "recent_signals.jsonl"
TRADE_LOG  = LOG_DIR / "recent_trades.jsonl"
IDEMP_FILE = LOG_DIR / "idempotency_keys.json"


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

def _append_jsonl(path: pathlib.Path, obj: Dict[str, Any]):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def _load_idemp() -> set:
    if IDEMP_FILE.exists():
        try:
            return set(json.load(open(IDEMP_FILE, "r", encoding="utf-8")))
        except Exception:
            return set()
    return set()

def _save_idemp(keys: set):
    json.dump(sorted(list(keys)), open(IDEMP_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def _symbol_of(coin: str) -> str:
    # 统一映射：ETH -> ETH-USDT
    return f"{coin.upper()}-USDT"

def _side_for(signal: str) -> str:
    s = signal.lower()
    if s in {"entry","buy","open","long"}:
        return "buy"
    if s in {"close","exit","sell"}:
        return "sell"
    return "hold"

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

def _log_signal(meta: Dict[str, Any], coin: str, args: Dict[str, Any], extra: Dict[str, Any]):
    rec = {
        "ts": meta.get("current_time"),
        "runtime_minutes": meta.get("runtime_minutes"),
        "invocations": meta.get("invocations"),
        "account_value": meta.get("account_value"),
        "available_cash": meta.get("available_cash"),
        "coin": coin,
        **args,
        **extra,
    }
    _append_jsonl(SIGNAL_LOG, rec)

def _log_trade(meta: Dict[str, Any], req: Dict[str, Any], resp: Dict[str, Any]):
    rec = {
        "ts": meta.get("current_time"),
        "coin": req.get("symbol","").split("-")[0],
        "symbol": req.get("symbol"),
        "side": req.get("side"),
        "size": req.get("size"),
        "order_type": req.get("order_type","market"),
        "limit_price": req.get("limit_price"),
        "flask_response": resp
    }
    _append_jsonl(TRADE_LOG, rec)

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

def route_trading_decisions(decisions: Dict[str, Any]) -> Tuple[int,int,int]:
    """
    将 TRADING_DECISIONS JSON 直接接到 Flask /order。
    返回 (下单成功数, 拒绝/跳过数, 记录数)
    """
    meta = decisions.get("meta") or {}
    # 幂等键集合
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

        # 统一日志（所有信号）
        _log_signal(meta, coin, {"signal": signal, **args}, extra={})
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
