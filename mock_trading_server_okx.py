from flask import Flask, request, jsonify, render_template
import requests, time, base64, hmac, hashlib, threading, json
from config import OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, OKX_BASE_URL
import json, pathlib, os

app = Flask(__name__)

# 币种列表
SYMBOLS = ["BTC-USDT", "ETH-USDT", "BNB-USDT", "SOL-USDT", "XRP-USDT", "DOGE-USDT"]

LOG_DIR = pathlib.Path(os.getenv("LOG_DIR", "logs"))
SIGNAL_LOG = LOG_DIR / "recent_signals.jsonl"
TRADE_LOG  = LOG_DIR / "recent_trades.jsonl"

# 全局行情缓存
market_cache = {}
last_update = 0

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

@app.get("/recent/signals")
def recent_signals():
    limit = int(request.args.get("limit", 50))
    return jsonify({"items": _tail_jsonl(SIGNAL_LOG, limit)})

@app.get("/recent/trades")
def recent_trades():
    limit = int(request.args.get("limit", 50))
    return jsonify({"items": _tail_jsonl(TRADE_LOG, limit)})


@app.route("/market", methods=["GET"])
def get_market():
    """读取缓存行情"""
    age = round(time.time() - last_update, 2)
    return jsonify({
        "age_seconds": age,
        "updated_at": last_update,
        "data": market_cache
    })

@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")

# @app.route("/balance", methods=["GET"])
# def get_balance():
#     # path = "/api/v5/account/balance"
#     path = "/api/v5/account/positions"
#     r = requests.get(OKX_BASE_URL + path, headers=okx_headers("GET", path))
#     return jsonify(r.json())

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
    """
    模拟下单接口（不访问OKX服务器）
    仅用于测试 AI → 风控 → 下单 → 日志 流程是否正常。
    """
    data = request.get_json()
    symbol = data.get("symbol", "BTC-USDT")
    side = data.get("side", "buy").lower()
    size = str(data.get("size", 0.001))
    order_type = data.get("order_type", "market")

    # 模拟回执
    mock_response = {
        "code": "0",
        "msg": "success",
        "data": [{
            "orderId": f"MOCK-{int(time.time())}",
            "instId": symbol,
            "side": side,
            "sz": size,
            "ordType": order_type
        }]
    }

    # 控制台日志
    print(f"[MOCK ORDER] {side.upper()} {size} {symbol}")
    return jsonify(mock_response)

# @app.route("/order", methods=["POST"])
# def place_order():
#     data = request.get_json()
#     symbol = data.get("symbol")
#     side = data.get("side", "buy").lower()
#     sz = str(data.get("size", 0.001))
#     ord_type = "market"

#     path = "/api/v5/trade/order"
#     body = {
#         "instId": symbol,
#         "tdMode": "cross",
#         "side": side,
#         "ordType": ord_type,
#         "sz": sz
#     }
#     body_json = json.dumps(body)
#     headers = okx_headers("POST", path, body_json)

#     # ✅ 就在这一行上面插入 ↓↓↓
#     print("[DEBUG] Posting to", OKX_BASE_URL + path)

#     r = requests.post(OKX_BASE_URL + path, headers=headers, data=body_json)
#     return jsonify(r.json())

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
