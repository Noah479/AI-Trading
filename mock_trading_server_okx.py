from flask import Flask, request, jsonify, render_template
import requests, time, base64, hmac, hashlib, threading, json
from config import OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, OKX_BASE_URL
import json, pathlib, os, random
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

BARS = ["3m", "30m", "4h"]  # ✅ 增加 3m 短周期

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
    """生成 OKX 请求头（使用本地时间）"""
    from datetime import datetime, timezone
    
    # 直接用本地 UTC 时间
    dt = datetime.now(timezone.utc)
    timestamp = dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    
    sign = okx_sign(timestamp, method, request_path, body)
    
    return {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": timestamp,
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
    items = _tail_jsonl(SIGNAL_LOG, limit)

    # ✅ 新增：按时间字段排序（最旧→最新）
    items.sort(key=lambda s: s.get("ts") or s.get("timestamp") or s.get("time") or s.get("created_at"))

    return jsonify({"items": items})

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
    """
    模拟行情接口（带微观结构指标），
    可自动切换为 OKX 实盘行情（6个币），
    若实盘请求失败则回退为本地缓存。
    """
    out = {"data": {}}

    for sym in SYMBOLS:
        # === 尝试获取 OKX 实盘行情 ===
        try:
            r = requests.get(f"https://www.okx.com/api/v5/market/ticker?instId={sym}", timeout=5)
            js = r.json()
            if js.get("code") == "0" and js.get("data"):
                snap = js["data"][0]  # ✅ 实盘行情数据
            else:
                raise Exception("empty")
        except Exception:
            # ⚙️ 回退到本地缓存模式
            snap = fetch_snapshot_cached(sym)

        # === 基础行情 ===
        last = float(snap.get("last") or 0)
        high24h = float(snap.get("high24h") or 0)
        low24h = float(snap.get("low24h") or 0)

        # ✅ 获取 3 个时间框架的 K线
        cds_3m = fetch_candles_cached(sym, "3m", limit=240)   # 3m × 240 = 12 小时
        cds_30m = fetch_candles_cached(sym, "30m", limit=240) # 30m × 240 = 5 天
        cds_4h = fetch_candles_cached(sym, "4h", limit=240)   # 4h × 240 = 40 天

        # === ask / bid ===
        ask = float(snap.get("askPx") or snap.get("ask") or 0 or last)
        bid = float(snap.get("bidPx") or snap.get("bid") or 0 or last)
        if not ask or not bid:
            # 无盘口信息则生成随机价差
            if last > 0:
                spread_ratio = random.uniform(0.0003, 0.0008)
                spread = last * spread_ratio
                bid = last - spread / 2
                ask = last + spread / 2
            else:
                bid = ask = last
        spread_bps = abs(ask - bid) / last * 1e4 if last else 0.0

        # === 写入结果 ===
        out["data"][sym] = {
            "price": last,
            "last": last,
            "high24h": high24h,
            "low24h": low24h,
            "candles": {
                
                "30m": cds_30m,
                "4h": cds_4h
            },
            "bid": bid,
            "ask": ask,
            "spread_bps": spread_bps,
            "open_interest": random.uniform(50_000_000, 80_000_000),
            "funding_rate": random.uniform(-0.0003, 0.0003),
            "volume_24h": float(snap.get("volCcyQuote") or 0.0),
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
        # ✅ 1. 获取账户余额
        headers = okx_headers("GET", "/api/v5/account/balance")
        balance_resp = requests.get(
            OKX_BASE_URL + "/api/v5/account/balance", 
            headers=headers,
            timeout=5
        ).json()

        # ✅ 2. 检查 API 响应是否成功
        if balance_resp.get("code") != "0" or not balance_resp.get("data"):
            app.logger.error(f"❌ Balance API failed: {balance_resp}")
            return jsonify({
                "error": "OKX API error",
                "code": balance_resp.get("code", "unknown"),
                "msg": balance_resp.get("msg", "No data returned")
            }), 500

        # ✅ 3. 获取持仓信息
        headers2 = okx_headers("GET", "/api/v5/account/positions")
        pos_resp = requests.get(
            OKX_BASE_URL + "/api/v5/account/positions", 
            headers=headers2,
            timeout=5
        ).json()

        # ✅ 4. 安全提取数据
        total_eq = float(balance_resp["data"][0].get("totalEq", 0))
        unrealized = 0.0

        # 累加所有未实现盈亏
        if pos_resp.get("code") == "0" and pos_resp.get("data"):
            for p in pos_resp["data"]:
                try:
                    unrealized += float(p.get("upl", 0))
                except (ValueError, TypeError):
                    pass

        combined_total = total_eq + unrealized

        # ✅ 5. 返回统一格式
        result = {
            "code": "0",
            "msg": "success",
            "totalEq": total_eq,
            "unrealizedPnL": unrealized,
            "totalEq_incl_unrealized": combined_total,
            "details": balance_resp["data"][0].get("details", []),
        }
        
        return jsonify(result)

    except KeyError as e:
        app.logger.error(f"❌ KeyError in balance: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "error": "Missing data field in OKX response",
            "detail": str(e)
        }), 500
    except Exception as e:
        app.logger.error(f"❌ Balance request failed: {e}")
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
