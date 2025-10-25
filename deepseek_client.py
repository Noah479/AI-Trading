# -*- coding: utf-8 -*-
# DeepSeek 决策客户端（严格 JSON + 回退策略）
import os, json, time, re
from datetime import datetime, timezone

_DEEPSEEK_BASE = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
_DEEPSEEK_MODEL_PRIMARY = os.getenv("DEEPSEEK_MODEL_PRIMARY", "deepseek-reasoner")
_DEEPSEEK_MODEL_FALLBACK = os.getenv("DEEPSEEK_MODEL_FALLBACK", "deepseek-chat")
_API_KEY = os.getenv("DEEPSEEK_API_KEY", "sk-8ba30dbdcb814f75b3ba6141ab220163")

# --- HTTP helper: prefer requests, fallback to urllib ---
def _http_post_json(url, headers, payload, timeout=30):
    try:
        import requests
        r = requests.post(url, headers=headers, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        # fallback
        import urllib.request
        import urllib.error
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers=headers | {"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as he:
            body = he.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"HTTPError {he.code}: {body}") from he
        except Exception as e2:
            raise

def _extract_json_maybe(s: str):
    """从文本中提取最大 JSON 块；若失败返回 None"""
    if not s:
        return None
    # 贪婪匹配最大 {...} 或 [...]
    for pat in (r'\{.*\}', r'\[.*\]'):
        m = re.search(pat, s, re.S)
        if m:
            txt = m.group(0)
            try:
                return json.loads(txt)
            except Exception:
                continue
    return None

def build_messages(market_snapshot: dict, balance_snapshot: dict,
                   recent_trades: list | None, constraints: dict) -> list[dict]:
    # system
    system = (
        "You are an AI quant trader. "
        "Output STRICT JSON ONLY (no code fences, no explanations), "
        "conforming to the provided schema. Put the final JSON in `content`."
    )
    # user
    user_lines = [
        "json",
        "CONTEXT:",
        f"- market snapshot: {json.dumps(market_snapshot, ensure_ascii=False)}",
        f"- account balance: {json.dumps(balance_snapshot, ensure_ascii=False)}",
        f"- recent trades: {json.dumps(recent_trades or [], ensure_ascii=False)}",
        "CONSTRAINTS:",
        f"- tradable symbols: {json.dumps(constraints.get('symbols', []), ensure_ascii=False)}",
        f"- lot/price rules: {json.dumps(constraints.get('symbol_rules', {}), ensure_ascii=False)}",
        f"- defaults: {json.dumps(constraints.get('defaults', {}), ensure_ascii=False)}",
        "REQUIRED JSON OUTPUT EXAMPLE:",
        json.dumps({
            "version": "1.0",
            "decision": {
                "symbol": "BTC-USDT", "side": "buy", "order_type": "market",
                "size": 0.001, "limit_price": None, "max_slippage_bps": 10,
                "risk": {"stop_loss_pct": 0.01, "take_profit_pct": 0.02},
                "confidence": 0.75, "reason": "..."
            },
            "ts": datetime.now(timezone.utc).isoformat()
        }, ensure_ascii=False)
    ]
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(user_lines)}
    ]

def _call_chat_completions(model: str, messages: list[dict],
                           response_format_json: bool = True,
                           max_tokens: int = 800, timeout: int = 30) -> dict:
    url = f"{_DEEPSEEK_BASE}/chat/completions"
    headers = {
        "Authorization": f"Bearer {_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": messages,
        # deepseek-reasoner 支持 JSON Output；我们开启严格 JSON
        "response_format": {"type": "json_object"} if response_format_json else {"type": "text"},
        "max_tokens": max_tokens
    }
    return _http_post_json(url, headers, payload, timeout=timeout)

def _parse_content_to_json(resp: dict) -> dict | None:
    try:
        content = resp["choices"][0]["message"]["content"]
    except Exception:
        return None
    if not content:
        return None
    try:
        return json.loads(content)
    except Exception:
        return _extract_json_maybe(content)

def get_decision(market_snapshot: dict, balance_snapshot: dict,
                 recent_trades: list | None, constraints: dict,
                 retries: int = 2) -> tuple[dict | None, dict]:
    """
    返回 (decision_json | None, meta)
    meta 包含 model_used / raw_response / error
    """
    messages = build_messages(market_snapshot, balance_snapshot, recent_trades, constraints)
    meta = {"model_used": None, "raw_response": None, "error": None}

    # 1) 主路径：deepseek-reasoner + JSON 严格模式
    try:
        resp = _call_chat_completions(_DEEPSEEK_MODEL_PRIMARY, messages, True)
        meta["model_used"] = _DEEPSEEK_MODEL_PRIMARY
        meta["raw_response"] = resp
        js = _parse_content_to_json(resp)
        if js:
            return js, meta
    except Exception as e:
        meta["error"] = f"primary error: {e}"

    # 2) 重试（提示轻微改写）
    if retries >= 1:
        messages2 = messages.copy()
        messages2[-1] = {
            "role": "user",
            "content": messages[-1]["content"] + "\nIMPORTANT: Return ONLY JSON strictly following the example."
        }
        try:
            resp2 = _call_chat_completions(_DEEPSEEK_MODEL_PRIMARY, messages2, True)
            meta["model_used"] = _DEEPSEEK_MODEL_PRIMARY
            meta["raw_response"] = resp2
            js2 = _parse_content_to_json(resp2)
            if js2:
                return js2, meta
        except Exception as e2:
            meta["error"] = f"retry error: {e2}"

    # 3) 回退：deepseek-chat（JSON 模式）
    try:
        resp3 = _call_chat_completions(_DEEPSEEK_MODEL_FALLBACK, messages, True)
        meta["model_used"] = _DEEPSEEK_MODEL_FALLBACK
        meta["raw_response"] = resp3
        js3 = _parse_content_to_json(resp3)
        if js3:
            return js3, meta
    except Exception as e3:
        meta["error"] = f"fallback error: {e3}"

    # 4) 最后尝试：text 模式 + 正则提取
    try:
        resp4 = _call_chat_completions(_DEEPSEEK_MODEL_FALLBACK, messages, False)
        meta["model_used"] = _DEEPSEEK_MODEL_FALLBACK
        meta["raw_response"] = resp4
        js4 = _parse_content_to_json(resp4)
        if js4:
            return js4, meta
    except Exception as e4:
        meta["error"] = f"final text-mode error: {e4}"

    return None, meta
