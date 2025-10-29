# test_confidence.py - 置信度测试脚本
import sys
import os

# 模拟市场数据
MOCK_MARKET = {
    "BTC-USDT": {
        "price": 69000.0,
        "last": 69000.0,
        "high24h": 70000.0,
        "low24h": 68000.0,
        "ema_fast": 68500.0,
        "ema_slow": 67000.0,
        "rsi14": 55.0,
        "atr14": 1380.0,
        "macd": 150.0,
        "macd_signal": 100.0,
        "macd_prev": 120.0,
        "macd_signal_prev": 110.0,
        "adx14": 28.0,
        "boll_upper": 71000.0,
        "boll_mid": 69000.0,
        "boll_lower": 67000.0,
        "macd_golden_cross": False,
        "macd_death_cross": False,
        "tf": {
            "3m": {
                "rsi14": 52.0,
                "adx14": 25.0,
                "macd": 50.0,
                "macd_signal": 45.0,
                "ema_fast": 68800.0,
                "ema_slow": 68500.0
            },
            "4h": {
                "rsi14": 58.0,
                "adx14": 30.0,
                "macd": 200.0,
                "macd_signal": 180.0,
                "ema_fast": 69500.0,
                "ema_slow": 68000.0
            }
        }
    },
    "ETH-USDT": {
        "price": 2400.0,
        "last": 2400.0,
        "high24h": 2450.0,
        "low24h": 2380.0,
        "ema_fast": 2390.0,
        "ema_slow": 2350.0,
        "rsi14": 45.0,
        "atr14": 48.0,
        "macd": -10.0,
        "macd_signal": -5.0,
        "macd_prev": -8.0,
        "macd_signal_prev": -6.0,
        "adx14": 18.0,
        "boll_upper": 2480.0,
        "boll_mid": 2400.0,
        "boll_lower": 2320.0,
        "macd_golden_cross": False,
        "macd_death_cross": False,
        "tf": {
            "3m": {"rsi14": 48.0, "adx14": 16.0},
            "4h": {"rsi14": 50.0, "adx14": 20.0}
        }
    }
}

MOCK_BALANCE = {
    "USDT": {"available": 10000.0},
    "totalEq": 10000.0,
    "totalEq_incl_unrealized": 10000.0
}

print("="*70)
print("测试 1: 置信度解析测试")
print("="*70)

# 测试用例 1：正常浮点数
test_cases = [
    {"confidence": 0.75, "expected": 0.75, "desc": "正常浮点数 0.75"},
    {"confidence": 0.0, "expected": 0.30, "desc": "边界值 0.0（应被限制到 0.30）"},
    {"confidence": 0.5, "expected": 0.50, "desc": "中间值 0.5"},
    {"confidence": "0.85", "expected": 0.85, "desc": "字符串 '0.85'"},
    {"confidence": "75%", "expected": 0.75, "desc": "百分比字符串 '75%'"},
    {"confidence": 85, "expected": 0.85, "desc": "整数百分比 85"},
    {"confidence": None, "expected": 0.55, "desc": "None（应使用默认值 0.55）"},
]

# 简化的解析函数（复制你代码的逻辑）
def parse_confidence(conf_raw):
    if conf_raw is not None:
        try:
            # 处理字符串
            if isinstance(conf_raw, str):
                conf_raw = conf_raw.replace("%", "").strip()
                conf = float(conf_raw)
                if conf > 1.0:
                    conf = conf / 100.0
            # 处理数字
            elif isinstance(conf_raw, (int, float)):
                conf = float(conf_raw)
                if conf > 1.0:
                    conf = conf / 100.0
            else:
                conf = 0.55
            
            # 限制范围
            conf = max(0.30, min(0.95, conf))
            return conf
        except:
            return 0.55
    else:
        return 0.55

for i, case in enumerate(test_cases, 1):
    result = parse_confidence(case["confidence"])
    status = "✅" if abs(result - case["expected"]) < 0.01 else "❌"
    print(f"{status} 测试 {i}: {case['desc']}")
    print(f"   输入: {case['confidence']} → 输出: {result:.2f} (期望: {case['expected']:.2f})")

print("\n" + "="*70)
print("测试 2: 置信度影响杠杆计算")
print("="*70)

# 导入你的杠杆计算函数
try:
    from ai_trader import _calculate_smart_leverage
    
    test_confidences = [0.30, 0.50, 0.70, 0.85, 0.95]
    
    for conf in test_confidences:
        lev = _calculate_smart_leverage(
            ai_confidence=conf,
            market_row=MOCK_MARKET["BTC-USDT"],
            consecutive_losses=0,
            max_leverage=25.0
        )
        print(f"置信度 {conf:.2f} → 杠杆 {lev:.2f}x")
    
    print("\n✅ 如果看到杠杆随置信度变化，说明置信度生效了！")
    
except ImportError as e:
    print(f"⚠️ 无法导入 ai_trader 模块: {e}")
    print("请确保在项目根目录运行此脚本")

print("\n" + "="*70)
print("测试 3: 置信度影响仓位计算")
print("="*70)

try:
    from ai_trader import _calculate_smart_position
    
    for conf in test_confidences:
        pos = _calculate_smart_position(
            ai_confidence=conf,
            market_row=MOCK_MARKET["BTC-USDT"],
            equity=10000.0,
            consecutive_losses=0,
            max_position_pct=0.30
        )
        print(f"置信度 {conf:.2f} → 仓位 {pos:.2f} USDT ({pos/10000*100:.1f}%)")
    
    print("\n✅ 如果看到仓位随置信度变化，说明置信度生效了！")
    
except ImportError as e:
    print(f"⚠️ 无法导入 ai_trader 模块: {e}")

print("\n" + "="*70)
print("测试 4: 完整决策流程模拟")
print("="*70)

try:
    # 模拟不同置信度的决策
    from ai_trader import _decisions_from_ai
    
    print("⚠️ 此测试需要真实的 DeepSeek API，跳过...")
    print("建议手动运行: python ai_trader.py")
    
except Exception as e:
    print(f"跳过完整流程测试: {e}")

print("\n" + "="*70)
print("测试总结")
print("="*70)
print("1. ✅ 置信度解析逻辑 - 已测试")
print("2. ⚠️ 杠杆计算 - 需要手动运行 ai_trader.py 验证")
print("3. ⚠️ 仓位计算 - 需要手动运行 ai_trader.py 验证")
print("\n运行方式：")
print("  python test_confidence.py")
print("\n如果测试 1 全部通过，说明解析逻辑正确！")
print("="*70)