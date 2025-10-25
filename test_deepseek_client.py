# -*- coding: utf-8 -*-
"""
RiskManager 完整测试套件
测试所有风控功能和边界条件
"""

import sys
import time
from datetime import date

# 导入 RiskManager
from risk_manager import RiskManager, RiskConfig, SymbolRule

def create_test_config() -> RiskConfig:
    """创建测试用的风控配置"""
    return RiskConfig(
        daily_loss_limit_pct=0.03,           # 日亏损 3%
        max_open_risk_pct=0.03,              # 最大开仓风险 3%
        max_gross_exposure_pct=1.0,          # 最大总敞口 100%
        balance_reserve_pct=0.10,            # 保留 10% 余额
        max_consecutive_losses=3,            # 最多连亏 3 次
        cooldown_global_sec=5,              # 全局冷却 10 秒（测试用）
        max_symbol_exposure_pct=0.30,        # 单品种最大 30%
        symbol_cooldown_sec=5,               # 品种冷却 5 秒（测试用）
        risk_per_trade_pct=0.005,            # 每笔风险 0.5%
        max_trade_ratio=0.30,                # 单笔最大 30%
        max_slippage_bps=20,                 # 最大滑点 20bps
        deviation_guard_bps=30,              # 限价偏离保护 30bps
        atr_lookback=14,
        atr_floor_bps=25,
        atr_mult_stop=2.0,
        atr_mult_tp=3.0,
        fee_rate_bps=8.0,
        symbol_rules={
            "BTC-USDT": {
                "price_tick": 0.1,
                "lot_size_min": 0.001,
                "lot_size_step": 0.001
            },
            "ETH-USDT": {
                "price_tick": 0.01,
                "lot_size_min": 0.01,
                "lot_size_step": 0.01
            }
        }
    )

def create_test_decision(symbol: str, side: str, size: float = None, 
                        order_type: str = "market", limit_price: float = None) -> dict:
    """创建测试决策"""
    decision = {
        "timestamp": int(time.time()),
        "decision": {
            "symbol": symbol,
            "side": side,
            "order_type": order_type
        }
    }
    
    if size is not None:
        decision["decision"]["size"] = size
    
    if limit_price is not None:
        decision["decision"]["limit_price"] = limit_price
    
    return decision

def create_test_market(symbol: str, price: float) -> dict:
    """创建测试市场数据"""
    return {
        symbol: {
            "price": price,
            "bid": price * 0.9999,
            "ask": price * 1.0001
        }
    }

def create_test_balance(usdt_amount: float) -> dict:
    """创建测试余额"""
    return {
        "USDT": {
            "available": usdt_amount,
            "total": usdt_amount
        }
    }

# ============================================
# 测试用例
# ============================================

def test_1_basic_order(rm: RiskManager):
    """测试 1: 基础订单通过"""
    print("=" * 70)
    print("测试 1: 基础订单通过")
    print("=" * 70)
    
    decision = create_test_decision("BTC-USDT", "buy")
    market = create_test_market("BTC-USDT", 50000.0)
    balance = create_test_balance(100000.0)
    
    approved, order, reason = rm.pre_trade_checks(decision, market, balance)
    
    print(f"✅ 通过: {approved}")
    print(f"📋 原因: {reason}")
    
    if order:
        print(f"📊 订单: {{")
        print(f'  "symbol": "{order["symbol"]}",')
        print(f'  "side": "{order["side"]}",')
        print(f'  "order_type": "{order["order_type"]}",')
        print(f'  "size": {order["size"]},')
        print(f'  "limit_price": {order["limit_price"]}')
        print(f"}}")
        print(f"   - 交易对: {order['symbol']}")
        print(f"   - 方向: {order['side']}")
        print(f"   - 数量: {order['size']}")
    
    assert approved, "基础订单应该通过"
    assert order is not None, "应该返回订单"
    assert order["symbol"] == "BTC-USDT"
    assert order["side"] == "buy"
    
    print("✅ 测试 1 通过\n")

def test_2_daily_loss_limit(rm: RiskManager):
    """测试 2: 日内亏损限制 (Kill Switch)"""
    print("=" * 70)
    print("测试 2: 日内亏损限制 (Kill Switch)")
    print("=" * 70)
    
    # 模拟日初权益 100,000，当前权益 96,000（亏损 4%）
    rm.state.day_open_equity = 100000.0
    rm.equity_provider = lambda: 96000.0  # 触发 kill switch (> 3%)
    
    decision = create_test_decision("BTC-USDT", "buy")
    market = create_test_market("BTC-USDT", 50000.0)
    balance = create_test_balance(96000.0)
    
    approved, order, reason = rm.pre_trade_checks(decision, market, balance)
    
    print(f"❌ 拒绝: {not approved}")
    print(f"📋 原因: {reason}")
    
    assert not approved, "应该触发 kill switch"
    assert "kill-switch" in reason.lower(), "原因应该包含 kill-switch"
    
    print("✅ 测试 2 通过\n")

def test_3_consecutive_losses(rm: RiskManager):
    """测试 3: 连续亏损冷却"""
    print("=" * 70)
    print("测试 3: 连续亏损冷却")
    print("=" * 70)
    
    # 重置权益提供者
    rm.equity_provider = lambda: 100000.0
    rm.state.day_open_equity = 100000.0
    
    # 模拟 3 次连续亏损
    rm.state.consecutive_losses = 3
    rm.state.last_trade_ts = {"BTC-USDT": int(time.time())}
    
    decision = create_test_decision("BTC-USDT", "buy")
    market = create_test_market("BTC-USDT", 50000.0)
    balance = create_test_balance(100000.0)
    
    approved, order, reason = rm.pre_trade_checks(decision, market, balance)
    
    print(f"❌ 拒绝: {not approved}")
    print(f"📋 原因: {reason}")
    
    assert not approved, "应该触发连续亏损冷却"
    assert "cooldown" in reason.lower(), "原因应该包含 cooldown"
    
    # 等待冷却时间
    print(f"\n⏳ 等待 {rm.cfg.cooldown_global_sec}s 冷却...\n")
    time.sleep(rm.cfg.cooldown_global_sec + 1)
    
    # 重新检查
    approved2, order2, reason2 = rm.pre_trade_checks(decision, market, balance)
    
    print(f"✅ 冷却后通过: {approved2}")
    print(f"📋 原因: {reason2}")
    
    assert approved2, "冷却后应该通过"
    
    # 重置状态
    rm.state.consecutive_losses = 0
    
    print("✅ 测试 3 通过\n")

def test_4_symbol_cooldown(rm: RiskManager):
    """测试 4: 品种级冷却"""
    print("=" * 70)
    print("测试 4: 品种级冷却")
    print("=" * 70)
    
    # 模拟刚交易过 BTC
    rm.state.last_trade_ts = {"BTC-USDT": int(time.time())}
    
    decision = create_test_decision("BTC-USDT", "buy")
    market = create_test_market("BTC-USDT", 50000.0)
    balance = create_test_balance(100000.0)
    
    approved, order, reason = rm.pre_trade_checks(decision, market, balance)
    
    print(f"❌ 拒绝: {not approved}")
    print(f"📋 原因: {reason}")
    
    assert not approved, "应该触发品种冷却"
    assert "symbol cooldown" in reason.lower(), "原因应该是品种冷却"
    
    # 测试其他品种不受影响
    decision_eth = create_test_decision("ETH-USDT", "buy")
    market_eth = create_test_market("ETH-USDT", 3000.0)
    
    approved_eth, order_eth, reason_eth = rm.pre_trade_checks(decision_eth, market_eth, balance)
    
    print(f"\n✅ ETH 可以交易: {approved_eth}")
    
    assert approved_eth, "其他品种不应该受冷却影响"
    
    print("✅ 测试 4 通过\n")

def test_5_price_unavailable(rm: RiskManager):
    """测试 5: 价格不可用保护"""
    print("=" * 70)
    print("测试 5: 价格不可用保护")
    print("=" * 70)
    
    # 清除冷却状态，避免被拦截
    rm.state.last_trade_ts = {}
    rm.state.consecutive_losses = 0
    
    decision = create_test_decision("BTC-USDT", "buy")
    market = {"BTC-USDT": {"price": 0}}  # 价格异常
    balance = create_test_balance(100000.0)
    
    approved, order, reason = rm.pre_trade_checks(decision, market, balance)
    
    print(f"❌ 拒绝: {not approved}")
    print(f"📋 原因: {reason}")
    
    assert not approved, "应该拒绝价格异常的订单"
    assert "price unavailable" in reason.lower(), "原因应该是价格不可用"
    
    print("✅ 测试 5 通过\n")

def test_6_equity_unavailable(rm: RiskManager):
    """测试 6: 权益不可用保护"""
    print("=" * 70)
    print("测试 6: 权益不可用保护")
    print("=" * 70)
    
    # 模拟权益获取失败
    original_equity = rm.equity_provider
    rm.equity_provider = lambda: 0.0
    
    decision = create_test_decision("BTC-USDT", "buy")
    market = create_test_market("BTC-USDT", 50000.0)
    balance = create_test_balance(100000.0)
    
    approved, order, reason = rm.pre_trade_checks(decision, market, balance)
    
    print(f"❌ 拒绝: {not approved}")
    print(f"📋 原因: {reason}")
    
    assert not approved, "应该拒绝权益异常的订单"
    assert "equity unavailable" in reason.lower(), "原因应该是权益不可用"
    
    # 恢复
    rm.equity_provider = original_equity
    
    print("✅ 测试 6 通过\n")

def test_7_limit_price_deviation(rm: RiskManager):
    """测试 7: 限价单价格偏离保护"""
    print("=" * 70)
    print("测试 7: 限价单价格偏离保护")
    print("=" * 70)
    
    current_price = 50000.0
    
    # 限价偏离过大（超过 30bps）
    deviated_price = current_price * 1.005  # 偏离 50bps
    
    decision = create_test_decision(
        "BTC-USDT", 
        "buy", 
        order_type="limit", 
        limit_price=deviated_price
    )
    market = create_test_market("BTC-USDT", current_price)
    balance = create_test_balance(100000.0)
    
    approved, order, reason = rm.pre_trade_checks(decision, market, balance)
    
    print(f"❌ 拒绝: {not approved}")
    print(f"📋 原因: {reason}")
    print(f"📈 当前价: {current_price}")
    print(f"📉 限价: {deviated_price}")
    print(f"📊 偏离: {(deviated_price - current_price) / current_price * 10000:.1f} bps")
    
    assert not approved, "应该拒绝偏离过大的限价单"
    assert "deviates" in reason.lower(), "原因应该包含 deviates"
    
    print("✅ 测试 7 通过\n")

def test_8_size_below_minimum(rm: RiskManager):
    """测试 8: 数量低于最小值"""
    print("=" * 70)
    print("测试 8: 数量低于最小值")
    print("=" * 70)
    
    # 临时降低 equity，确保所有 cap 都很小
    original_equity = rm.equity_provider
    rm.equity_provider = lambda: 20.0  # 极小权益
    rm.state.day_open_equity = 20.0
    
    decision = create_test_decision("BTC-USDT", "buy")
    market = create_test_market("BTC-USDT", 50000.0)
    balance = create_test_balance(3.0)  # 极小余额
    
    approved, order, reason = rm.pre_trade_checks(decision, market, balance)
    
    print(f"❌ 拒绝: {not approved}")
    print(f"📋 原因: {reason}")
    
    if order:
        print(f"⚠️  意外生成的订单: size={order.get('size')}")
    
    # 恢复
    rm.equity_provider = original_equity
    rm.state.day_open_equity = 100000.0
    
    assert not approved, "应该拒绝数量过小的订单"
    assert "below" in reason.lower() and "min" in reason.lower(), "原因应该是数量过小"
    
    print("✅ 测试 8 通过\n")

def test_9_post_trade_update(rm: RiskManager):
    """测试 9: 交易后状态更新"""
    print("=" * 70)
    print("测试 9: 交易后状态更新")
    print("=" * 70)
    
    # 模拟成功交易
    symbol = "BTC-USDT"
    filled_size = 0.1
    fill_price = 50000.0
    realized_pnl = 100.0  # 盈利
    
    initial_pnl = rm.state.realized_pnl_today
    initial_losses = rm.state.consecutive_losses
    
    rm.post_trade_update(symbol, filled_size, fill_price, realized_pnl, "buy")
    
    print(f"✅ 更新时间戳: {symbol in rm.state.last_trade_ts}")
    print(f"✅ PnL 更新: {initial_pnl} → {rm.state.realized_pnl_today}")
    print(f"✅ 连亏重置: {initial_losses} → {rm.state.consecutive_losses}")
    print(f"✅ 仓位记录: {symbol in rm.state.open_positions}")
    
    assert symbol in rm.state.last_trade_ts, "应该记录交易时间"
    assert rm.state.realized_pnl_today == initial_pnl + realized_pnl, "PnL 应该更新"
    assert rm.state.consecutive_losses == 0, "盈利应该重置连亏"
    assert symbol in rm.state.open_positions, "应该记录仓位"
    
    # 测试亏损情况
    rm.post_trade_update(symbol, filled_size, fill_price, -50.0, "sell")
    
    print(f"✅ 连亏计数: {rm.state.consecutive_losses}")
    assert rm.state.consecutive_losses == 1, "亏损应该增加连亏计数"
    
    print("✅ 测试 9 通过\n")

def test_10_atr_calculation(rm: RiskManager):
    """测试 10: ATR 计算"""
    print("=" * 70)
    print("测试 10: ATR 波动率计算")
    print("=" * 70)
    
    symbol = "BTC-USDT"
    
    # 推送价格历史
    prices = [50000, 50100, 49900, 50200, 50000, 49800, 50300, 50100]
    
    for price in prices:
        rm.push_price(symbol, price)
    
    atr_bps = rm._atr_proxy_bps(symbol)
    
    print(f"📊 价格序列: {prices}")
    print(f"📈 ATR (bps): {atr_bps:.2f}")
    print(f"📉 ATR 下限: {rm.cfg.atr_floor_bps}")
    
    assert atr_bps >= rm.cfg.atr_floor_bps, "ATR 不应低于下限"
    assert atr_bps > 0, "ATR 应该大于 0"
    
    print("✅ 测试 10 通过\n")

# ============================================
# 运行所有测试
# ============================================

def run_all_tests():
    """运行所有测试"""
    print("\n")
    print("🧪" * 35)
    print("  RiskManager 完整测试套件")
    print("🧪" * 35)
    print("\n")
    
    # 创建 RiskManager 实例
    cfg = create_test_config()
    rm = RiskManager(cfg, state_path="test_risk_state.json")
    
    # 设置权益提供者
    rm.equity_provider = lambda: 100000.0
    
    # 初始化状态
    rm.state.day_open_equity = 100000.0
    rm.state.consecutive_losses = 0
    rm.state.last_trade_ts = {}
    
    try:
        # 运行测试
        test_1_basic_order(rm)
        test_2_daily_loss_limit(rm)
        test_3_consecutive_losses(rm)
        test_4_symbol_cooldown(rm)
        test_5_price_unavailable(rm)
        test_6_equity_unavailable(rm)
        test_7_limit_price_deviation(rm)
        test_8_size_below_minimum(rm)
        test_9_post_trade_update(rm)
        test_10_atr_calculation(rm)
        
        print("=" * 70)
        print("🎉 所有测试通过！")
        print("=" * 70)
        
    except AssertionError as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    
    except Exception as e:
        print(f"\n💥 测试异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    run_all_tests()