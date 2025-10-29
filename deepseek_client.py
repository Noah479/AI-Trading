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
        # ✅ 新增：微观结构指标
        micro = market.get(sym, {})
        snapshot[sym].update({
            "spread_bps": micro.get("spread_bps"),
            "open_interest": micro.get("open_interest"),
            "funding_rate": micro.get("funding_rate"),
            "volume_24h": micro.get("volume_24h")
        })

    system_prompt = (
        "你是一名专业量化交易AI。请在给定账户信息、市场快照与技术指标下，为\"最有潜力\"的**单一**交易对生成**严格JSON**决策。\n"
        "我会给出多个交易对（BTC、ETH、SOL、BNB、XRP、DOGE）的实时指标，包括EMA、RSI、MACD、ADX、BOLL、ATR等。\n"
        "你的任务是：从这些币种中选出最有潜力的一个，并做出交易决策。\n\n"
        
        "【硬性格式要求】\n"
        "1) 在 extended thinking 中完成推理，最终在 message.content 输出完整JSON（无额外文字）\n"
        "2) JSON 架构：\n"
        "   {\"version\":\"1.0\",\"decision\":{\n"
        "      \"symbol\":\"<符号>\",\n"
        "      \"side\":\"buy|sell|hold\",\n"
        "      \"order_type\":\"market|limit\",\n"
        "      \"leverage\":<float>,\n"
        "      \"max_slippage_bps\":<int>,\n"
        "      \"risk\":{\"stop_loss_pct\":<float>,\"take_profit_pct\":<float>},\n"
        "      \"exit_plan\":{\n"
        "         \"take_profit_pct\":<float>,\n"
        "         \"stop_loss_pct\":<float>,\n"
        "         \"invalidation_condition\":\"<触发撤销的条件>\"\n"
        "      },\n"
        "      \"confidence\":<0..1 的小数>,\n"
        "      \"rationale\":\"≤50字中文理由\"\n"
        "   },\"ts\":\"ISO-8601\"}\n"
        "3) 仅输出一个 symbol；所有数值必须是裸数（不带单位/百分号/区间）。\n"
        "4) 若无法确定方向，按 BTC-USDT 给出 hold（安全回退）。\n\n"
        
        "【可用输入说明】\n"
        "- market_snapshot[sym]: last, high24h, low24h（可能缺失）\n"
        "- micro_structure[sym]（可选）：spread_bps, open_interest, funding_rate, volume_24h\n"
        "- technical_indicators[sym]（可能部分缺失）包含 3 个时间周期：\n"
        "  • **3m（短线信号）**：\n"
        "    - rsi14_3m, adx14_3m, macd_3m, macd_signal_3m, ema_fast_3m, ema_slow_3m\n"
        "    - macd_golden_cross_3m (bool): 刚发生金叉  # ✅ 新增\n"
        "    - macd_death_cross_3m (bool): 刚发生死叉   # ✅ 新增\n"
        "  • **30m（执行基线）**：\n"
        "    - rsi14, adx14, macd, macd_signal, ema_fast, ema_slow, boll_upper/mid/lower, atr14\n"
        "    - macd_golden_cross (bool): 刚发生金叉     # ✅ 新增\n"
        "    - macd_death_cross (bool): 刚发生死叉      # ✅ 新增\n"
        "  • **4h（趋势过滤）**：\n"
        "    - rsi14_4h, adx14_4h, macd_4h, ema_fast_4h, ema_slow_4h\n"
        "    - macd_golden_cross_4h (bool), macd_death_cross_4h (bool)  # ✅ 新增\n"
        
        "【⚠️ 最高优先级：3m 极端信号强制规则】\n"
        "在进行任何打分计算前，必须先检查 3m 周期的极端情况：\n"
        "1. **极端超买（强制观望）**：\n"
        "   - 若 rsi14_3m > 90：\n"
        "     → 强制输出: side=\"hold\", leverage=1.0, confidence=0.40\n"
        "     → rationale=\"3m RSI极端超买({rsi14_3m:.1f})，等待回调\"\n"
        "     → 跳过所有后续打分，直接输出JSON\n"
        "2. **极端超卖（强制观望）**：\n"
        "   - 若 rsi14_3m < 10：\n"
        "     → 强制输出: side=\"hold\", leverage=1.0, confidence=0.40\n"
        "     → rationale=\"3m RSI极端超卖({rsi14_3m:.1f})，等待反弹\"\n"
        "     → 跳过所有后续打分，直接输出JSON\n"
        "3. **极端趋势末期（强制观望）**：\n"
        "   - 若 adx14_3m > 80：\n"
        "     → 强制输出: side=\"hold\", leverage=1.0, confidence=0.40\n"
        "     → rationale=\"3m ADX极端({adx14_3m:.1f})，趋势末期观望\"\n"
        "     → 跳过所有后续打分，直接输出JSON\n\n"
        
        "【多周期协同规则】（仅在未触发极端规则时执行）\n"
        "1. **3m 短线信号（紧急刹车）**：\n"
        "   - 若 85 < rsi14_3m ≤ 90：总分 -3，杠杆上限 3x，confidence 上限 0.55\n"
        "   - 若 10 < rsi14_3m ≤ 15：总分 -3，杠杆上限 3x，confidence 上限 0.55\n"
        "   - 若 70 < adx14_3m ≤ 80：总分 -2，杠杆上限 5x\n"
        "   - 若 3m MACD 与 30m 方向相反：总分 -2（短线背离）\n"
        "     判断方法：(macd_3m - macd_signal_3m) 与 (macd - macd_signal) 符号相反\n"
        "2. **30m 执行基线（主要判断）**：\n"
        "   - 按后续【多因子打分】计算\n"
        "3. **4h 趋势过滤（背景验证）**：\n"
        "   - 若 4h 数据为 null：忽略，不影响决策\n"
        "   - 若 ema_fast_4h > ema_slow_4h 且 30m 也多头（ema_fast > ema_slow）：总分 +1，confidence +0.05\n"
        "   - 若 ema_fast_4h < ema_slow_4h 且 30m 也空头（ema_fast < ema_slow）：总分 +1，confidence +0.05\n"
        "   - 若 4h 与 30m 趋势相反：总分 -2，confidence -0.12\n\n"
        
        "【计算辅助量】\n"
        "- pos24 = (last - low24h) / max(1e-9, high24h - low24h)，缺失用 0.5\n"
        "- atr_pct = atr14 / last，缺失用 0.02\n\n"
        
        "【多因子打分（30m 基线）】\n"
        "A. 趋势因子：\n"
        "   - ema_fast > ema_slow: +2\n"
        "   - ema_fast < ema_slow: -2\n"
        "   - last > ema_slow: +1\n"
        "   - last < ema_slow: -1\n"
        "B. 动能因子（RSI）：\n"
        "   - rsi14 ≥ 70: -1\n"
        "   - 55 ≤ rsi14 < 70: +1\n"
        "   - 40 ≤ rsi14 < 55: 0\n"
        "   - 30 ≤ rsi14 < 40: -0.5\n"
        "   - rsi14 < 30: +1\n"
        "C. MACD 因子（优化判断 - 优先使用历史数据）：\n"
        "   - 基础判断（适用于所有情况）：\n"
        "     • macd > macd_signal: +1（多头）\n"
        "     • macd < macd_signal: -1（空头）\n"
        "   - 强势判断（按优先级）：\n"
        "     优先级 1 - 使用历史数据（最准确）：\n"
        "       • 若 macd_golden_cross == true（刚发生金叉）: +3\n"
        "       • 若 macd_death_cross == true（刚发生死叉）: -3\n"
        "     优先级 2 - 回退到差值判断（当历史数据不可用时）：\n"
        "       • 若 macd > macd_signal 且 |macd - macd_signal| > 0.3*|macd_signal|: +3\n"
        "       • 若 macd < macd_signal 且 |macd - macd_signal| > 0.3*|macd_signal|: -3\n"
        "   注意：强势判断会覆盖基础判断（不叠加）\n"
        "D. 趋势强度（ADX）：\n"
        "   - adx14 > 30: 将 (趋势因子 + MACD因子) × 1.3\n"
        "   - 20 < adx14 ≤ 30: × 1.0\n"
        "   - adx14 ≤ 20: × 0.5（震荡市削弱趋势信号）\n"
        "E. 布林带：\n"
        "   - last ≥ boll_upper: -1（除非 adx14>30 且 macd>signal）\n"
        "   - last ≤ boll_lower: +1（除非 macd<signal）\n"
        "F. 风险惩罚：\n"
        "   - atr_pct > 0.05: -1\n"
        "   - 0.03 < atr_pct ≤ 0.05: -0.5\n"
        "   - pos24 < 0.1 或 > 0.9: -0.5\n\n"
        
        "【信号生成规则】\n"
        "- 总分 Score = A + B + C（经 D 调整）+ E + F + 多周期修正\n"
        "- 开仓条件：\n"
        "  • 若 Score ≥ +2 且 adx14>20：side=buy\n"
        "  • 若 Score ≤ -2 且 adx14>20：side=sell\n"
        "  • 否则：side=hold\n"
        "- 特殊观望情况（优先级高于开仓条件）：\n"
        "  • 若 rsi14_3m > 85 或 < 15：强制 hold（即使总分满足开仓条件）\n"
        "  • 若 adx14 ≤ 20 且 |Score| < 3：优先 hold（震荡市避免频繁交易）\n"
        "- 多品种并列时选择优先级：\n"
        "  1. 优先选择 adx14 更高者（趋势更强）\n"
        "  2. 若 adx14 相近（差距<5），选择 atr_pct 更小者（风险更低）\n"
        "  3. 若仍无法区分，选择 BTC-USDT\n\n"
        
        "【风控参数】\n"
        "- order_type: \"market\"\n"
        "- max_slippage_bps: min(15, constraints.risk_limits.max_slippage_bps)\n"
        "- 止损/止盈计算：\n"
        "  sl = clip(0.8*atr_pct, 0.003, 0.050)\n"
        "  若 side≠hold：\n"
        "    • adx14>30: tp = clip(2.5*sl, 0.010, 0.100)\n"
        "    • 20<adx14≤30: tp = clip(2.0*sl, 0.010, 0.100)\n"
        "    • adx14≤20: tp = clip(1.6*sl, 0.010, 0.100)\n"
        "  若 side=hold：sl=0.01, tp=0.02（占位值）\n"
        "- **杠杆倍数计算**：\n"
        "  步骤1 - 基础杠杆（根据趋势强度）：\n"
        "    • adx14 > 30 且 confidence > 0.70: 8x\n"
        "    • adx14 > 25 且 confidence > 0.60: 5x\n"
        "    • adx14 > 20: 3x\n"
        "    • adx14 ≤ 20: 2x\n"
        "  步骤2 - 应用惩罚系数（连乘）：\n"
        "    • rsi14_3m > 85 或 < 15: × 0.5\n"
        "    • atr_pct > 0.05: × 0.6\n"
        "    • spread_bps > 20: × 0.7\n"
        "    • adx14_3m > 70: × 0.8\n"
        "  步骤3 - 最终限制：\n"
        "    • 结果 = 步骤1 × 步骤2所有系数\n"
        "    • 最终杠杆 = max(1.0, min(结果, 10.0))\n"
        "    • 保留1位小数\n"
        "- **confidence 计算（动态评分）**：\n"
        "  步骤1 - 基础值（根据多因子打分结果）：\n"
        "    • 总分 Score >= 4: 0.75（强信号）\n"
        "    • 总分 3 <= Score < 4: 0.65\n"
        "    • 总分 2 <= Score < 3: 0.55（中等）\n"
        "    • 总分 -2 < Score < 2: 0.45（弱信号/观望）\n"
        "    • 总分 -3 < Score <= -2: 0.55\n"
        "    • 总分 -4 < Score <= -3: 0.65\n"
        "    • 总分 Score <= -4: 0.75（强空头信号）\n"
        "  步骤2 - ADX 趋势强度加权（×系数）：\n"
        "    • adx14 > 40: × 1.15（强趋势增强信心）\n"
        "    • 30 < adx14 <= 40: × 1.08\n"
        "    • 20 < adx14 <= 30: × 1.00（正常）\n"
        "    • adx14 <= 20: × 0.85（震荡市降低信心）\n"
        "  步骤3 - 多周期一致性修正（+/-）：\n"
        "    加分项（每项 +0.05）：\n"
        "    • 3m/30m/4h 三周期 EMA 方向一致\n"
        "    • 3m 和 30m MACD 同向\n"
        "    • 4h MACD 与 30m 同向\n"
        "    • RSI 在健康区间（买入: 40-65, 卖出: 35-60）\n"
        "    减分项（每项 -0.08）：\n"
        "    • 3m RSI > 85 或 < 15（极端）\n"
        "    • 30m RSI > 75 或 < 25\n"
        "    • 3m 与 30m 趋势相反\n"
        "    • 4h 与 30m 趋势相反\n"
        "    • atr_pct > 0.05（高波动）\n"
        "  步骤4 - 最终限制：\n"
        "    • 结果 = (步骤1 × 步骤2) + 步骤3所有修正\n"
        "    • 最终 confidence = clip(结果, 0.30, 0.95)\n"
        "    • 保留2位小数（如 0.67 而非 0.673456）\n"
        "    • ⚠️ 重要：必须输出数字（如 0.75），不能输出字符串（如 \"0.75\" 或 \"75%\"）\n"
        "  步骤2 - 加分项（每项 +0.08，可叠加）：\n"
        "    • EMA 与 MACD 同向（ema_fast>ema_slow 且 macd>macd_signal，或都相反）\n"
        "    • 4h 趋势同向（若 4h 数据可用）\n"
        "    • RSI 健康区间：买入时 40≤rsi14≤65，卖出时 35≤rsi14≤60\n"
        "  步骤3 - 减分项（每项 -0.10，可叠加）：\n"
        "    • atr_pct > 0.05（高波动）\n"
        "    • rsi14 > 75 或 < 25（30m 极端）\n"
        "    • rsi14_3m > 85 或 < 15（3m 极端）\n"
        "    • EMA 与 MACD 冲突（ema_fast>ema_slow 但 macd<macd_signal，或相反）\n"
        "    • adx14_3m > 70（3m 趋势过热）\n"
        "  步骤4 - 最终限制：\n"
        "    • 结果 = 基础值 + 加分 - 减分\n"
        "    • 最终 confidence = clip(结果, 0.30, 0.95)\n"
        "    • 保留2位小数\n\n"
        
        "【健壮性与回退】\n"
        "- 指标缺失处理：\n"
        "  • 若某个指标为 null 或缺失，用安全默认值：\n"
        "    - RSI: 50（中性）\n"
        "    - ADX: 20（弱趋势）\n"
        "    - MACD: 0（无信号）\n"
        "    - EMA: 使用 last 价格\n"
        "    - ATR: last * 0.02\n"
        "  • 若有效因子 < 2 个（如只有价格无指标）：\n"
        "    → 输出 BTC-USDT hold（sl=0.01, tp=0.02, conf=0.50, rationale=\"数据不足观望\"）\n"
        "- 所有字段必须是具体数值：\n"
        "  • 不得输出 \"N/A\"、\"—\"、\"约\"、\"~\" 等模糊内容\n"
        "  • leverage 必须是纯数字（如 2.5 而非 \"2.5x\"）\n"
        "  • 百分比字段用小数（如 0.05 而非 \"5%\"）\n"
        "- 仅交易 constraints.symbols 中的币种：\n"
        "  • 若输入数据包含未在白名单的币种，忽略它们\n"
        "  • 若所有币种都不在白名单，回退到 BTC-USDT hold\n\n"
        
        "【最终输出要求】\n"
        "1. **严格JSON格式**：\n"
        "   - 只输出一个 JSON 对象（不是数组）\n"
        "   - 不得在 JSON 前后添加任何文字、解释、思维链\n"
        "   - 示例错误输出：\"根据分析，我建议...{json}\" ❌\n"
        "   - 示例正确输出：{json} ✅\n"
        "2. **rationale 内容要求**：\n"
        "   - 长度：≤50字中文\n"
        "   - 必须包含关键依据，示例：\n"
        "     • \"30m金叉+ADX32+4h同向，3m RSI85降杠杆\"\n"
        "     • \"震荡市ADX18+RSI中性，观望\"\n"
        "     • \"3m RSI超买93.8，等待回调\"\n"
        "   - 避免冗余词汇（如\"根据\"\"建议\"\"因此\"）\n"
        "3. **时间戳格式**：\n"
        "   - 使用 ISO-8601 格式（如 \"2025-10-29T17:30:00Z\"）\n"
        "   - 使用 UTC 时区\n\n"
        
        "【示例输出（仅供参考格式）】\n"
        "{\n"
        "  \"version\": \"1.0\",\n"
        "  \"decision\": {\n"
        "    \"symbol\": \"SOL-USDT\",\n"
        "    \"side\": \"hold\",\n"
        "    \"order_type\": \"market\",\n"
        "    \"leverage\": 1.0,\n"
        "    \"max_slippage_bps\": 15,\n"
        "    \"risk\": {\n"
        "      \"stop_loss_pct\": 0.01,\n"
        "      \"take_profit_pct\": 0.02\n"
        "    },\n"
        "    \"exit_plan\": {\n"
        "      \"take_profit_pct\": 0.02,\n"
        "      \"stop_loss_pct\": 0.01,\n"
        "      \"invalidation_condition\": \"3m RSI回落至75以下或30m金叉确认\"\n"
        "    },\n"
        "    \"confidence\": 0.40,\n"
        "    \"rationale\": \"3m RSI极端超买93.9，等待回调\"\n"
        "  },\n"
        "  \"ts\": \"2025-10-29T17:30:00Z\"\n"
        "}\n"
    )
    

    indicators = {}
    for sym in symbols:
        row = market.get(sym) or {}
        tf_data = row.get("tf", {})
        ctx3m = tf_data.get("3m", {})  # ✅ 新增
        ctx4h = tf_data.get("4h", {})
        
        indicators[sym] = {
            # === 30m 基线（主执行周期）===
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

            # ===  ✅ 新增：3m 高频数据（风控预警）===
            "ema_fast_3m":  ctx3m.get("ema_fast"),
            "ema_slow_3m":  ctx3m.get("ema_slow"),
            "rsi14_3m":     ctx3m.get("rsi14"),        # ← 关键！
            "atr14_3m":     ctx3m.get("atr14"),
            "macd_3m":      ctx3m.get("macd"),
            "macd_signal_3m": ctx3m.get("macd_signal"),
            "adx14_3m":     ctx3m.get("adx14"),        # ← 关键！
            "boll_upper_3m":ctx3m.get("boll_upper"),
            "boll_mid_3m":  ctx3m.get("boll_mid"),
            "boll_lower_3m":ctx3m.get("boll_lower"),

            # === 4h 背景趋势（大周期过滤）===
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
                    "confidence": "0.0~1", 
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
    """
    调用 DeepSeek Chat 接口并保存推理日志（reasoning_content）
    """
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

    # === 发起请求 ===
    resp = _http_post_json(url, headers, payload, timeout=timeout)

    # === 解析推理过程 ===
    try:
        if isinstance(resp, dict):
            choice = (resp.get("choices") or [{}])[0]
            message = choice.get("message", {})
            content_reasoning = message.get("reasoning_content") or ""
            content_final = message.get("content") or ""

            # ✅ 保存推理日志
            os.makedirs("logs", exist_ok=True)
            raw_path = os.path.join("logs", f"ai_reasoning_{int(time.time())}.txt")
            with open(raw_path, "w", encoding="utf-8") as f:
                f.write("==== PROMPT ====\n")
                f.write(json.dumps(messages, ensure_ascii=False, indent=2))
                f.write("\n\n==== REASONING ====\n")
                f.write(content_reasoning)
                f.write("\n\n==== OUTPUT ====\n")
                f.write(content_final)

            # ✅ 自动清理旧日志，保留最新 50 个
            files = sorted(
                [os.path.join("logs", fn) for fn in os.listdir("logs") if fn.startswith("ai_reasoning_")],
                key=os.path.getmtime,
            )
            if len(files) > 50:
                for f in files[:-50]:
                    try:
                        os.remove(f)
                    except:
                        pass

    except Exception as e:
        print(f"[deepseek] 无法保存推理日志: {e}")

    # ✅ 同步到 ai_status.json
    status_path = os.path.join("logs", "ai_status.json")
    try:
        status = {}
        if os.path.exists(status_path):
            with open(status_path, "r", encoding="utf-8") as f:
                status = json.load(f)
        status.update({
            "last_reasoning": content_reasoning.strip()[:3000],
            "last_output": content_final.strip(),
            "last_update_ts": datetime.utcnow().isoformat() + "Z",
            "model": model
        })
        with open(status_path, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[deepseek] 无法写入 ai_status.json: {e}")

    return resp


# === 主函数：与 ai_trader.py 对接 ===
def get_decision(market: dict,
                 balance: dict,
                 recent_trades=None,
                 constraints: dict=None,
                 temperature: float=0.5): # ← 从 0.2 改为 0.5
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

        # ✅ 修复：兼容 DeepSeek 返回列表格式 [{"version": "1.0", "decision": {...}}]
        if isinstance(js1, list) and len(js1) > 0:
            print(f"⚠️ DeepSeek 返回列表格式（共 {len(js1)} 个元素），自动提取第一个")
            js1 = js1[0]  # 取第一个字典
        elif isinstance(js1, list) and len(js1) == 0:
            print("❌ DeepSeek 返回空列表")
            js1 = None
        elif not isinstance(js1, dict):
            print(f"❌ DeepSeek 返回未知格式: {type(js1).__name__}")
            js1 = None

        if js1:
            print("🧠 DeepSeek 原始输出(JSON):", str(js1)[:400])
            
            # 🆕 添加详细的置信度日志
            decision_detail = js1.get("decision", {})
            raw_conf = decision_detail.get("confidence")
            symbol = decision_detail.get("symbol")
            side = decision_detail.get("side")
            rationale = decision_detail.get("rationale")
            
            print(f"\n{'='*70}")
            print(f"[置信度详情]")
            print(f"  Symbol: {symbol}")
            print(f"  Side: {side}")
            print(f"  Confidence: {raw_conf} (类型: {type(raw_conf).__name__})")
            print(f"  Rationale: {rationale}")
            print(f"{'='*70}\n")
            
            # 🆕 保存到日志文件
            import os
            os.makedirs("logs", exist_ok=True)
            with open("logs/confidence_log.jsonl", "a", encoding="utf-8") as f:
                import json
                f.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "symbol": symbol,
                    "side": side,
                    "confidence": raw_conf,
                    "rationale": rationale
                }, ensure_ascii=False) + "\n")


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

    # ✅ 【关键修复1】先解析风险字段
    risk = d.get("risk", {}) or {}
    sl = _to_float(risk.get("stop_loss_pct"), 0.01)
    tp = _to_float(risk.get("take_profit_pct"), 0.02)
    
    # ✅ 【关键修复2】先解析杠杆字段
    lev = _to_float(d.get("leverage"), 1.0)
    
    # ✅ 【关键修复3】先解析滑点字段
    max_slip = _to_int(d.get("max_slippage_bps"), 10)

    # ✅ 修改后（更严格的解析 + 日志）
    conf_raw = d.get("confidence")
    print(f"🔍 [置信度解析] 原始值: {conf_raw} (类型: {type(conf_raw).__name__})")

    # 如果 AI 返回了字符串（如 "0.7" 或 "70%"），转成数字
    if isinstance(conf_raw, str):
        conf_raw = conf_raw.replace("%", "").strip()
        try:
            conf = float(conf_raw)
            # 如果是百分数（如 70 而非 0.7），转成小数
            if conf > 1.0:
                conf = conf / 100.0
        except:
            conf = 0.55  # 解析失败才用默认值
    elif isinstance(conf_raw, (int, float)):
        conf = float(conf_raw)
        if conf > 1.0:  # 如果是 70 而非 0.7
            conf = conf / 100.0
    else:
        conf = 0.55  # AI 完全没返回

    # 🔧 修改1：删除这行重复的 conf 裁剪（后面有统一裁剪）
    # conf = _clip(conf, 0.30, 0.95)  # ❌ 删除这行
    
    print(f"✅ [置信度解析] 最终值: {conf:.2f}")  # 🔧 修改2：移到裁剪后面

    # --- 边界裁剪 ---
    max_slip = int(_clip(max_slip, 1, 200))       # 1 ~ 200 bps
    sl = _clip(sl, 0.0, 0.20)                     # 0 ~ 20%
    tp = _clip(tp, 0.0, 0.50)                     # 0 ~ 50%
    lev = _clip(lev, 1.0, 25.0)                   # 1 ~ 25x
    conf = _clip(conf, 0.30, 0.95)                # 30% ~ 95%

    print(f"✅ [置信度解析] 最终值: {conf:.2f}")  # 🔧 修改2：移到这里（裁剪后）

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
            "leverage": round(lev, 1),              # 🔧 修改3：保留1位小数
            "max_slippage_bps": max_slip,
            "risk": {
                "stop_loss_pct": round(sl, 4),      # 🔧 修改3：保留4位小数
                "take_profit_pct": round(tp, 4)     # 🔧 修改3：保留4位小数
            },
            "confidence": round(conf, 2),           # 🔧 修改3：保留2位小数
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
        print(f"[DeepSeek] normalized template -> side={side}, lev={lev:.1f}x, sl={sl:.2%}, tp={tp:.2%}, conf={conf:.2f}")

    return norm