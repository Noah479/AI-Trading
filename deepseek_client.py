# -*- coding: utf-8 -*-
# DeepSeek 决策客户端 (Hybrid): 稳健HTTP/解析 + 中文严格JSON Prompt + 多层回退
import os, json, time, re
from datetime import datetime, timezone
from config import DEEPSEEK_API_KEY, DEEPSEEK_API_BASE, DEEPSEEK_MODEL

# === 环境变量 ===
DEEPSEEK_API_KEY   = DEEPSEEK_API_KEY
DEEPSEEK_API_BASE  = DEEPSEEK_API_BASE
MODEL_PRIMARY      = DEEPSEEK_MODEL



# === HTTP 辅助：requests 优先，失败回退 urllib ===
def _http_post_json(url, headers, payload, timeout=60):
    try:
        import requests
        r = requests.post(url, headers=headers, json=payload, timeout=timeout)
        # 调试输出
        print(f"[DeepSeek] POST {url} -> HTTP {r.status_code}, len={len(r.text or '')}")
        r.raise_for_status()
        return r.json()
    except Exception as e:
        # fallback 到 urllib
        import urllib.request, urllib.error
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
                print(f"[DeepSeek:urllib] HTTP 200, len={len(body)}")
                return json.loads(body)
        except urllib.error.HTTPError as he:
            body = he.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"HTTPError {he.code}: {body}") from he
        except Exception as e2:
            raise


# === 解析 deepseek 响应为 JSON：支持 reasoning_content / content ===

def _parse_content_to_json(resp: dict):
    """
    解析 deepseek-reasoner/chat 的响应：
    - 优先读取 message.content（最终答案）
    - 次优读取 message.reasoning_content（推理过程）
    - 过滤 <think> 块
    - 直接 json.loads；失败则用正则提取最外层 { ... } 再解析
    """
    try:
        choice = (resp.get("choices") or [{}])[0]
        message = choice.get("message", {}) or {}

        # ✅ 关键：先读 content（最终输出），再读 reasoning_content（解释）
        content_final = message.get("content") or ""
        content_reason = message.get("reasoning_content") or ""

        # 打印两者长度，便于诊断
        print(f"[DeepSeek] message keys={list(message.keys())}, content_len={len(content_final)}, reasoning_len={len(content_reason)}")

        raw = content_final if content_final else content_reason
        if not raw:
            return None

        # 去除 <think> 思维链块
        if "<think>" in raw:
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)

        raw = raw.strip()
        if not raw:
            return None

        # 先直解析
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # 再尝试提取 JSON 块
            m = re.search(r"\{[\s\S]*\}", raw)
            if m:
                try:
                    return json.loads(m.group(0))
                except Exception:
                    return None
        return None
    except Exception as e:
        print("[DeepSeek] parse error:", e)
        return None



def _build_messages_cn(market: dict, balance: dict, constraints: dict, recent_trades=None):
    """
    构建带技术指标与量化约束的 Prompt
    """
    symbols = (constraints or {}).get("symbols") or list(market.keys())
    snapshot = {}
    for sym in symbols:
        row = market.get(sym) or {}
        last = row.get("price") or row.get("last")
        hi   = row.get("high") or row.get("high24h")
        lo   = row.get("low")  or row.get("low24h")
        try:
            last = float(last) if last is not None else None
            hi   = float(hi) if hi is not None else None
            lo   = float(lo) if lo is not None else None
        except Exception:
            last = hi = lo = None
        vol24 = (hi - lo) / last if last and hi and lo and last > 0 else None
        snapshot[sym] = {"last": last, "high24h": hi, "low24h": lo, "vol24": vol24}

    system_prompt = (
        "你是一名专业量化交易AI。请在给定账户信息、市场快照与技术指标下，为“最有潜力”的**单一**交易对生成**严格JSON**决策。\n"
        "我会给出多个交易对（BTC、ETH、SOL、BNB、XRP、DOGE）的实时指标，包括EMA、RSI、MACD、ADX、BOLL、ATR等。\n"
        "你的任务是：从这些币种中选出最有潜力的一个，并做出交易决策。\n\n"
        "\n"
        "【硬性格式要求】\n"
        "1) 在 extended thinking 中完成推理，最终在 message.content 输出完整JSON（无额外文字）"
        "2) JSON 架构：\n"
        "   {\"version\":\"1.0\",\"decision\":{\n"
        "      \"symbol\":\"<符号>\",\n"
        "      \"side\":\"buy|sell|hold\",\n"
        "      \"order_type\":\"market|limit\",\n"
        "      \"leverage\":<float>,\n"                           
        "      \"max_slippage_bps\":<int>,\n"
        "      \"risk\":{\"stop_loss_pct\":<float>,\"take_profit_pct\":<float>},\n"
        "      \"confidence\":<0..1.5 的小数>,\n"                  
        "      \"rationale\":\"≤30字中文理由\"\n"
        "   },\"ts\":\"ISO-8601\"}\n"
        "3) 仅输出一个 symbol；所有数值必须是裸数（不带单位/百分号/区间）。\n"
        "4) 若无法确定方向，按 BTC-USDT 给出 hold（安全回退）。\n"
        "\n"
        "【可用输入说明】\n"
        "- market_snapshot[sym]: last / high24h / low24h（可能缺失）；\n"
        "- technical_indicators[sym]（可能部分缺失）：ema_fast, ema_slow, rsi14, atr14, macd, macd_signal, adx14, boll_upper, boll_mid, boll_lower；\n"
        "- constraints.risk_limits.max_slippage_bps（上限），constraints.symbols（允许交易列表），symbol_rules（精度/步长）。\n"
        "technical_indicators[sym].tf_4h 为 4h 背景指标；以 30m 为执行基线，以 4h 作为趋势过滤/增强：同向放大分数，反向减弱甚至观望。"
        " - 如果行情波动太大或信号不明，请选择 hold。"
        " - leverage 可根据信心自由决定（系统会自动封顶 25x）。"
        "\n"
        "【计算辅助量】（缺失则跳过或取安全默认）\n"
        "- pos24 = (last - low24h) / max(1e-9, high24h - low24h)，缺失用 0.5；\n"
        "- atr_pct = atr14 / last（波动率，缺失用 0.02 进行保守估算）。\n"
        "\n"
        "【多因子打分（选一只最高分）】\n"
        "A. 趋势因子（基础）：\n"
        "   - ema_fast > ema_slow: +2；ema_fast < ema_slow: -2；\n"
        "   - last > ema_slow: +1；last < ema_slow: -1。\n"
        "B. 动能因子（RSI）：\n"
        "   - rsi14 ≥ 70: -1（短线回调风险）；\n"
        "   - 55 ≤ rsi14 < 70: +1；\n"
        "   - 40 ≤ rsi14 < 55: 0；\n"
        "   - 30 ≤ rsi14 < 40: -0.5（动能偏弱）；\n"
        "   - rsi14 < 30: +1（超卖反弹，但需趋势配合）。\n"
        "C. MACD 因子：\n"
        "   - macd > macd_signal: +1；macd < macd_signal: -1；\n"
        "   - 若出现“上穿/下穿”（由符号变化近因判断）：金叉 +3，死叉 -3。\n"
        "D. 趋势强度（ADX）：\n"
        "   - adx14 > 30: 将(趋势因子 + MACD因子)×1.3；\n"
        "   - 20 < adx14 ≤ 30: ×1.0；\n"
        "   - adx14 ≤ 20: ×0.5（震荡，削弱趋势信号）。\n"
        "E. 布林带（BOLL）辅助：\n"
        "   - last ≥ boll_upper: -1（超买），若 adx14>30 且 macd>signal 则改为 0；\n"
        "   - last ≤ boll_lower: +1（超卖），若 macd<signal 则改为 0。\n"
        "F. 风险/环境惩罚：\n"
        "   - atr_pct > 0.05: -1； 0.03 < atr_pct ≤ 0.05: -0.5；\n"
        "   - pos24<0.1 或 pos24>0.9：±0.5（极端位置适度惩罚）。\n"
        "\n"
        "【信号生成（阈值与冲突消解）】\n"
        "- 记总分为 Score：\n"
        "  • 若 Score ≥ +2 且 (adx14>20 或 出现金叉)：side=buy；\n"
        "  • 若 Score ≤ -2 且 (adx14>20 或 出现死叉)：side=sell；\n"
        "  • 否则 side=hold。\n"
        "- 若 RSI、MACD 与 EMA 结论冲突：以(趋势因子+MACD因子)为主，RSI 仅作强弱修正；ADX≤20 优先观望。\n"
        "- 多品种并列：优先 ADX 高者；若相同，选 atr_pct 更小（风险更低）；再相同选 BTC-USDT。\n"
        "\n"
        "【风控与参数映射】\n"
        "- order_type：默认 \"market\"；max_slippage_bps = min(15, constraints.risk_limits.max_slippage_bps 或默认值)。\n"
        "- 止损/止盈：\n"
        "  sl = clip(0.8*atr_pct, 0.003, 0.050)；\n"
        "  若 side≠hold：\n"
        "    • 若 adx14>30：tp = clip(2.5*sl, 0.010, 0.100)；\n"
        "    • 若 20<adx14≤30：tp = clip(2.0*sl, 0.010, 0.100)；\n"
        "    • 若 adx14≤20：tp = clip(1.6*sl, 0.010, 0.100)；\n"
        "  若 side=hold：sl=0.01，tp=0.02（占位）。\n"
        "- confidence：\n"
        "  • 基础：buy/sell=0.55，hold=0.50；\n"
        "  • 每项强一致性(+EMA多头且MACD>signal；或 金叉/死叉；或 RSI处于[55,65]/[35,45]之外的强区间) +0.05；\n"
        "  • 每项明显冲突 -0.05；adx14>30 +0.05；adx14≤18 -0.05；atr_pct>0.05 -0.05；\n"
        "  • 结果四舍五入到小数点后2位，限制在[0,1.5]。\n"
        "- rationale：用不超过30字的**中文**给出主因（如“MACD金叉+ADX走强”或“ADX<20震荡观望”）。\n"
        "\n"
        "【健壮性与回退】\n"
        "- 指标缺失时仍需给出结论：用可用因子计算；若有效因子<2，则对 BTC-USDT 输出 hold（sl=0.01,tp=0.02,conf=0.50,rationale=\"数据不足，暂观望\"）。\n"
        "- 所有字段必须填入具体数值；不得输出“N/A”“—”“约”等模糊内容。\n"
        "- 仅使用 constraints.symbols 中的交易对；若输入不在列表内，默认 BTC-USDT。\n"
        "\n"
        "【最终提醒】\n"
        "- 只输出**一个**严格JSON对象到 content；不得额外输出任何文字。\n"
        "请只输出一个严格的 JSON 对象，而不是列表。"
    )

    indicators = {}
    for sym in symbols:
        row = market.get(sym) or {}
        ctx4h = (row.get("tf") or {}).get("4h", {})
        indicators[sym] = {
            # === 30m 基线 ===
            "ema_fast": row.get("ema_fast"),
            "ema_slow": row.get("ema_slow"),
            "rsi14":    row.get("rsi14"),
            "atr14":    row.get("atr14"),
            "macd":         row.get("macd"),
            "macd_signal":  row.get("macd_signal"),
            "adx14":        row.get("adx14"),
            "boll_upper":   row.get("boll_upper"),
            "boll_mid":     row.get("boll_mid"),
            "boll_lower":   row.get("boll_lower"),

            # === 4h 背景趋势 ===
            "ema_fast_4h":  ctx4h.get("ema_fast"),
            "ema_slow_4h":  ctx4h.get("ema_slow"),
            "rsi14_4h":     ctx4h.get("rsi14"),
            "atr14_4h":     ctx4h.get("atr14"),
            "macd_4h":      ctx4h.get("macd"),
            "macd_signal_4h": ctx4h.get("macd_signal"),
            "adx14_4h":     ctx4h.get("adx14"),
            "boll_upper_4h":ctx4h.get("boll_upper"),
            "boll_mid_4h":  ctx4h.get("boll_mid"),
            "boll_lower_4h":ctx4h.get("boll_lower"),
        }


    user_payload = {
        "objective": "根据市场数据和技术指标，判断趋势方向，输出 buy/sell/hold。",
        "account": {"equity_usdt": float(balance.get('USDT', {}).get('available', 0.0) or 0.0)},
        "market_snapshot": snapshot,
        "technical_indicators": indicators,
        "constraints": {
            "symbols": symbols,
            "symbol_rules": (constraints or {}).get("symbol_rules", {}),
            "risk_limits": {"max_open_risk_pct": 0.03, "max_slippage_bps": 15},
            "output_json_schema": {
                "version": "1.0",
                "decision": {
                    "symbol": "BTC-USDT",
                    "side": "buy|sell|hold",
                    "order_type": "market|limit",
                    "leverage": "float",  
                    "confidence": "0.0~1.5", 
                    "max_slippage_bps": "int",
                    "risk": {"stop_loss_pct": "0.0~0.05", "take_profit_pct": "0.0~0.1"},
                    "rationale": "≤30字简短理由"
                },
                "ts": "ISO-8601"
            },
            "format_rule": "仅输出严格 JSON；不要解释；不要思维链。"
        }
    }

    # === ✅ 在这里添加说明 ===
    user_payload["instruction"] = (
        "分析上述多个交易对的指标，比较它们的趋势强度与风险，"
        "从中选出最具潜力的1个币种，并给出 buy/sell/hold 决策与杠杆倍数（最大25倍）。"
        "请以JSON格式输出，字段包括 symbol、side、leverage、risk、confidence、rationale。"
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)}
    ]

def _call_chat(model: str, messages: list, json_mode=True, temperature=0.2, max_tokens=2000, timeout=60):
    url = f"{DEEPSEEK_API_BASE}/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": messages,
        # 开启 JSON 强约束；fallback 时可尝试 text
        "response_format": {"type": "json_object"} if json_mode else {"type": "text"},
        "temperature": temperature,
        "max_tokens": max_tokens
    }
    return _http_post_json(url, headers, payload, timeout=timeout)

# === 主函数：与 ai_trader.py 对接 ===
def get_decision(market: dict,
                 balance: dict,
                 recent_trades=None,
                 constraints: dict=None,
                 temperature: float=0.2):
    """
    返回: (decision_dict, meta)
    """
    ts = int(time.time())
    messages = _build_messages_cn(market, balance, constraints or {}, recent_trades)
    meta = {"model_used": None, "raw_response": None, "error": None}

    # 0) 关键环境检查
    if not DEEPSEEK_API_KEY:
        meta["error"] = "DEEPSEEK_API_KEY missing"
        fallback = {
            "version":"1.0",
            "decision":{
                "symbol": (list(market.keys()) or ["BTC-USDT"])[0],
                "side":"hold","order_type":"market",
                "max_slippage_bps":10,
                "risk":{"stop_loss_pct":0.01,"take_profit_pct":0.02},
                "confidence":0.5,
                "rationale":"未配置API Key，暂不交易"
            },
            "ts": datetime.now(timezone.utc).isoformat()
        }
        return fallback, meta

    # 1) 主模型：JSON模式
    try:
        resp1 = _call_chat(MODEL_PRIMARY, messages, json_mode=True, temperature=temperature)
        meta["model_used"] = MODEL_PRIMARY
        meta["raw_response"] = resp1
        
        # ✅ 打印完整响应（调试用）
        choice = (resp1.get("choices") or [{}])[0]
        message = choice.get("message", {})
        print(f"\n{'='*70}")
        print(f"[DeepSeek 原始响应]")
        print(f"  content: {message.get('content', '')[:200]}")
        print(f"  reasoning_content: {message.get('reasoning_content', '')[:200]}")
        print(f"{'='*70}\n")
        
        js1 = _parse_content_to_json(resp1)
        if js1:
            print("🧠 DeepSeek 原始输出(JSON):", str(js1)[:300])
            
            # ✅ 关键修改：捕获 normalize 异常
            try:
                decision = _normalize_decision(js1, market)
                print("✅ 决策标准化成功")
                return decision, meta
            except Exception as norm_err:
                print(f"❌ 决策标准化失败: {norm_err}")
                meta["error"] = f"normalize error: {norm_err}"
                # 继续到 fallback
        else:
            print("⚠️ DeepSeek 返回了数据但无法解析为 JSON")
            meta["error"] = "parse failed: no valid JSON"
            
    except Exception as e:
        print(f"❌ DeepSeek API 调用失败: {e}")
        meta["error"] = f"primary error: {e}"


    # 全部失败：回退 HOLD
    print("⚠️ 所有尝试失败，返回 HOLD 决策")
    fallback = {
        "version":"1.0",
        "decision":{
            "symbol": (list(market.keys()) or ["BTC-USDT"])[0],
            "side":"hold","order_type":"market",
            "max_slippage_bps":10,
            "risk":{"stop_loss_pct":0.01,"take_profit_pct":0.02},
            "confidence":0.5,
            "rationale":"连接失败或解析异常，暂不交易"
        },
        "ts": datetime.now(timezone.utc).isoformat()
    }
    return fallback, meta

def _coerce_side(x: str) -> str:
    if not x:
        return "hold"
    sx = str(x).strip().lower()
    if "|" in sx:     # 像 "buy|sell|hold" → 视为不确定
        return "hold"
    if sx in ("buy", "sell", "hold"):
        return sx
    # 尝试从字符串中抓信号词
    if "buy" in sx and "sell" in sx:
        return "hold"
    if "buy" in sx:
        return "buy"
    if "sell" in sx:
        return "sell"
    return "hold"

def _coerce_order_type(x: str) -> str:
    if not x:
        return "market"
    sx = str(x).strip().lower()
    if "|" in sx:   # "market|limit"
        return "market"
    if "market" in sx:
        return "market"
    if "limit" in sx:
        return "limit"
    return "market"

def _to_int(x, default=10) -> int:
    try:
        if isinstance(x, (int, float)):
            return int(x)
        if x is None:
            return default
        s = str(x).strip()
        # 提取首个整数（支持 '15bps' / '约10'）
        m = re.search(r"-?\d+", s)
        if m:
            return int(m.group(0))
        return default
    except:
        return default

def _to_float(x, default=0.0) -> float:
    try:
        if isinstance(x, (int, float)):
            return float(x)
        if x is None:
            return default
        s = str(x).strip().replace("%", "")
        # 处理范围 '0.0~0.05' / '0.0-0.05'
        if "~" in s or "-" in s:
            parts = re.split(r"[~-]", s)
            nums = []
            for p in parts:
                p = p.strip()
                if re.match(r"^-?\d+(\.\d+)?$", p):
                    nums.append(float(p))
            if len(nums) >= 2:
                return (nums[0] + nums[1]) / 2.0
            if len(nums) == 1:
                return nums[0]
            return default
        # 抓取首个数字（支持 '0.01' / '15bps' / '≈0.02'）
        m = re.search(r"-?\d+(\.\d+)?", s)
        if m:
            return float(m.group(0))
        return default
    except:
        return default

def _clip(x, lo, hi):
    try:
        return max(lo, min(float(x), hi))
    except:
        return max(lo, min(x, hi))

def _normalize_decision(decision: dict, market: dict) -> dict:
    # --- 容错处理 ---
    if decision is None:
        decision = {}
    elif isinstance(decision, list):
        # 有些模型输出 [ {...} ] 形式
        decision = decision[0] if decision else {}

    if not isinstance(decision, dict):
        raise TypeError(f"decision must be dict, got {type(decision)}")
    
    d = (decision or {}).get("decision", {}) or {}

    # --- 符号 ---
    sym = d.get("symbol") or (list(market.keys()) or ["BTC-USDT"])[0]

    # --- 方向 / 委托类型 ---
    side = _coerce_side(d.get("side"))
    order_type = _coerce_order_type(d.get("order_type"))

    # --- 数值字段（鲁棒解析 + 边界约束） ---
    max_slip = _to_int(d.get("max_slippage_bps"), default=10)
    risk = d.get("risk") or {}
    sl = _to_float(risk.get("stop_loss_pct"), default=0.01)     # 默认 1% 止损
    tp = _to_float(risk.get("take_profit_pct"), default=0.02)   # 默认 2% 止盈
    conf = _to_float(d.get("confidence"), default=0.6)
    lev  = _to_float(d.get("leverage"),  default=None)

    # --- 边界裁剪 ---
    max_slip = int(_clip(max_slip, 1, 200))       # 1 ~ 200 bps
    sl = _clip(sl, 0.0, 0.20)                     # 0 ~ 20%
    tp = _clip(tp, 0.0, 0.50)                     # 0 ~ 50%
    conf = _clip(conf, 0.0, 1.5) # 上界改为 1.5（与 prompt 一致）

    # --- 理由 ---
    rationale = d.get("rationale") or d.get("reason") or "无"

    # --- 若 side 仍不确定，退回 hold（安全闸） ---
    if side not in ("buy", "sell", "hold"):
        side = "hold"

    # --- 组装规范化结果 ---
    norm = {
        "version": "1.0",
        "decision": {
            "symbol": sym,
            "side": side,
            "order_type": order_type,
            "max_slippage_bps": max_slip,
            "risk": {"stop_loss_pct": sl, "take_profit_pct": tp},
            "confidence": conf,
            "leverage": lev,  # 新增：把 leverage 放进标准化结果
            "rationale": rationale
        },
         # ✅ 强制用当前 UTC 时间，避免模型旧 ts 滞留
        "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    }

    # --- 调试：如出现模板占位被更正，打印一次（便于定位） ---
    if any([
        isinstance(d.get("max_slippage_bps"), str),
        isinstance(risk.get("stop_loss_pct"), str),
        isinstance(risk.get("take_profit_pct"), str),
        isinstance(d.get("confidence"), str),
        ("|" in str(d.get("side") or "")),
        ("|" in str(d.get("order_type") or "")),
    ]):
        print(f"[DeepSeek] normalized template -> side={side}, sl={sl}, tp={tp}, slip={max_slip}, conf={conf}")

    return norm
