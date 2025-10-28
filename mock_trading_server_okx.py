from flask import Flask, request, jsonify, render_template
import requests, time, base64, hmac, hashlib, threading, json
from config import OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, OKX_BASE_URL
import json, pathlib, os
from flask import send_from_directory

import logging
from logging.handlers import RotatingFileHandler

app = Flask(__name__)

# 币种列表
SYMBOLS = ["BTC-USDT", "ETH-USDT", "BNB-USDT", "SOL-USDT", "XRP-USDT", "DOGE-USDT"]

BASE_DIR = pathlib.Path(__file__).resolve().parent
LOG_DIR = pathlib.Path(os.getenv("LOG_DIR", str(BASE_DIR / "logs")))
LOG_DIR.mkdir(parents=True, exist_ok=True)

SIGNAL_LOG = LOG_DIR / "recent_signals.jsonl"
TRADE_LOG  = LOG_DIR / "recent_trades.jsonl"

# === Flask + Werkzeug 日志写入文件 ===
log_path = LOG_DIR / "flask_server.log"
LOG_DIR.mkdir(parents=True, exist_ok=True)

handler = RotatingFileHandler(
    log_path, maxBytes=5*1024*1024, backupCount=5, encoding="utf-8"
)
formatter = logging.Formatter(
    '[%(asctime)s] %(levelname)s in %(module)s: %(message)s'
)
handler.setFormatter(formatter)

# 1️⃣ 把 handler 添加到 Flask 自己的 logger
app.logger.addHandler(handler)
app.logger.setLevel(logging.INFO)

# 2️⃣ 同时添加到 Werkzeug 的访问日志
werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.addHandler(handler)
werkzeug_logger.setLevel(logging.INFO)

# 全局行情缓存
market_cache = {}
last_update = 0

_session = requests.Session()
_session.headers.update({"User-Agent":"okx-bridge/1.0"})

# --- 简单缓存，降低OKX频率 ---
_cache = {"ts":0, "snap":{}, "candles":{}}
CACHE_TTL_SNAPSHOT = 2     # 快照缓存2秒

CACHE_TTL_CANDLES  = 600   # K线缓存10分钟（OKX防抖）
CANDLE_LIMIT = 240         # 至少200根，给余量
# 多周期清单（本次我们要 30m + 4h）
BARS = ["30m", "4h"]

def okx_get(path, params=None, timeout=5):
    r = _session.get(OKX_BASE_URL + path, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()

def fetch_snapshot_once(instId):
    """OKX Ticker快照（含 last / high24h / low24h）。"""
    js = okx_get("/api/v5/market/ticker", {"instId":instId})
    data = (js.get("data") or [{}])[0]
    # OKX返回字符串，需要转float
    last = float(data.get("last", "0") or 0.0)
    high24h = float(data.get("high24h", "0") or 0.0)
    low24h  = float(data.get("low24h", "0") or 0.0)
    return {"last": last, "high24h": high24h, "low24h": low24h}

def fetch_candles_once(instId, bar="30m", limit=CANDLE_LIMIT):
    """OKX 返回 [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]，按时间**倒序**。
       我们需要**正序**+精简为 [open, high, low, close, volume]。
    """
    js = okx_get("/api/v5/market/candles", {"instId":instId, "bar":bar, "limit":limit})
    arr = js.get("data") or []
    candles = []
    for row in reversed(arr):  # 反转为从旧到新
        # row: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        o = float(row[1]); h = float(row[2]); l = float(row[3]); c = float(row[4]); vol = float(row[5])
        candles.append([o, h, l, c, vol])
    return candles

def fetch_snapshot_cached(instId):
    now = time.time()
    if now - _cache["ts"] > CACHE_TTL_SNAPSHOT:
        _cache["snap"].clear()
        _cache["ts"] = now
    if instId not in _cache["snap"]:
        _cache["snap"][instId] = fetch_snapshot_once(instId)
    return _cache["snap"][instId]

def fetch_candles_cached(instId, bar, limit=CANDLE_LIMIT):
    now = time.time()
    key = f"{instId}:{bar}:{limit}"
    if key not in _cache["candles"] or (now - _cache["candles"][key]["ts"] > CACHE_TTL_CANDLES):
        cds = fetch_candles_once(instId, bar=bar, limit=limit)
        _cache["candles"][key] = {"ts": now, "data": cds}
    return _cache["candles"][key]["data"]

# ==============================
# 签名与请求头生成
# ==============================
def okx_sign(timestamp, method, request_path, body=""):
    message = f"{timestamp}{method.upper()}{request_path}{body}"
    mac = hmac.new(OKX_SECRET_KEY.encode("utf-8"), message.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def okx_headers(method, request_path, body=""):
    """自动同步 OKX 服务器时间"""
    try:
        r = requests.get(OKX_BASE_URL + "/api/v5/public/time")
        server_time = str(float(r.json()["data"][0]["ts"]) / 1000)
    except Exception:
        server_time = str(time.time())

    sign = okx_sign(server_time, method, request_path, body)
    return {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": server_time,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json",
        "x-simulated-trading": "1"
    }



# ==============================
# 异步行情更新线程
# ==============================
def update_market_cache():
    global market_cache, last_update
    while True:
        data = {}
        for sym in SYMBOLS:
            path = f"/api/v5/market/ticker?instId={sym}"
            try:
                r = requests.get(OKX_BASE_URL + path, headers=okx_headers("GET", path), timeout=3)
                tick = r.json()["data"][0]
                data[sym] = {
                    "last": tick["last"],
                    "bid": tick["bidPx"],
                    "ask": tick["askPx"],
                    "high": tick["high24h"],
                    "low": tick["low24h"]
                }
            except Exception as e:
                data[sym] = {"error": str(e)}
        market_cache = data
        last_update = time.time()
        time.sleep(2)  # 每 2 秒刷新一次

threading.Thread(target=update_market_cache, daemon=True).start()

# ==============================
# API 路由
# ==============================

def _tail_jsonl(path: pathlib.Path, limit: int):
    if not path.exists(): return []
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    arr = [json.loads(x) for x in lines[-limit:]] if lines else []
    # 倒序（最新在前）
    return list(reversed(arr))


@app.get("/logs/<path:filename>")
def serve_logs(filename):
    # 直接从绝对 logs 目录输出任何日志文件（CSV/JSONL等）
    return send_from_directory(str(LOG_DIR), filename)

@app.get("/recent/signals")
def recent_signals():
    limit = int(request.args.get("limit", 50))
    return jsonify({"items": _tail_jsonl(SIGNAL_LOG, limit)})

@app.get("/recent/trades")
def recent_trades():
    limit = int(request.args.get("limit", 50))
    return jsonify({"items": _tail_jsonl(TRADE_LOG, limit)})


# === AI 状态接口（统一标准版） ===
@app.get("/leaderboard")
def leaderboard_page():
    return render_template("leaderboard.html")

@app.get("/api/status")
def api_status():
    """
    返回 AI 状态 JSON，用于前端轮询展示
    """
    try:
        path = LOG_DIR / "ai_status.json"
        if not path.exists():
            return jsonify({
                "status": "empty",
                "message": "ai_status.json not found"
            }), 200

        return jsonify(json.loads(path.read_text(encoding="utf-8")))
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500


@app.get("/market")
def market():
    out = {"data": {}}
    for sym in SYMBOLS:
        snap = fetch_snapshot_cached(sym)
        # 逐bar取回缓存
        cds_30m = fetch_candles_cached(sym, "30m")
        cds_4h  = fetch_candles_cached(sym, "4h")

        out["data"][sym] = {
            "price":  snap["last"],
            "last":   snap["last"],
            "high24h": snap["high24h"],
            "low24h":  snap["low24h"],
            # ★ 多时间尺度：返回映射而不是单一列表
            "candles": {
                "30m": cds_30m,   # [[o,h,l,c,v], ...] 旧->新
                "4h":  cds_4h
            }
        }
    return jsonify(out)

@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.route('/balance', methods=['GET'])
def get_balance():
    """
    获取账户余额并合并未实现盈亏（Unrealized PnL）
    """
    try:
        timestamp = str(time.time())
        headers = okx_headers("GET", "/api/v5/account/balance")
        balance_resp = requests.get(OKX_BASE_URL + "/api/v5/account/balance", headers=headers).json()

        headers2 = okx_headers("GET", "/api/v5/account/positions")
        pos_resp = requests.get(OKX_BASE_URL + "/api/v5/account/positions", headers=headers2).json()

        total_eq = float(balance_resp["data"][0].get("totalEq", 0))
        unrealized = 0.0

        # 累加所有未实现盈亏
        for p in pos_resp.get("data", []):
            try:
                unrealized += float(p.get("upl", 0))
            except:
                pass

        combined_total = total_eq + unrealized

        # 在原始响应中加入新的字段
        result = {
            "code": "0",
            "msg": "success",
            "totalEq": total_eq,
            "unrealizedPnL": unrealized,
            "totalEq_incl_unrealized": combined_total,
            "details": balance_resp["data"][0].get("details", []),
        }
        return jsonify(result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    
    
@app.route("/order", methods=["POST"])
def place_order():
    data = request.get_json()
    symbol = data.get("symbol", "BTC-USDT")
    side = data.get("side", "buy").lower()
    size = str(data.get("size", 0.001))
    order_type = data.get("order_type", "market")

    # 模拟延迟与随机滑点
    import random, time
    time.sleep(random.uniform(0.2, 1.0))  # 模拟网络延迟
    mock_price = random.uniform(0.99, 1.01)  # ±1% 随机滑点

    mock_response = {
        "code": "0",
        "msg": "success",
        "data": [{
            "orderId": f"MOCK-{int(time.time())}",
            "instId": symbol,
            "side": side,
            "sz": size,
            "price": mock_price,
            "ordType": order_type
        }]
    }

    print(f"[MOCK ORDER] {side.upper()} {size} {symbol} @ {mock_price:.2%}")
    return jsonify(mock_response)


# ==============================
# /verify 接口
# ==============================
@app.route("/verify", methods=["GET"])
def verify_connection():
    """验证当前 API Key 是否有效、环境是否正确"""
    try:
        # 获取账户基本信息
        path = "/api/v5/account/balance"
        r = requests.get(OKX_BASE_URL + path, headers=okx_headers("GET", path))
        res = r.json()

        if res.get("code") == "0":
            eq = res["data"][0].get("totalEq", "0")
            return jsonify({
                "status": "success",
                "environment": "Simulated (Demo Trading)",
                "total_equity_usd": eq,
                "keys_valid": True,
                "message": "Connection successful ✅"
            })
        else:
            return jsonify({
                "status": "failed",
                "keys_valid": False,
                "error": res
            }), 400
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

# ==============================
# 主程序入口
# ==============================
if __name__ == "__main__":
    print("✅ OKX Trading Server started at http://127.0.0.1:5001")
    app.run(host="0.0.0.0", port=5001)
