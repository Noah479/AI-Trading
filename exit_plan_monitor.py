# -*- coding: utf-8 -*-
"""
退出计划监控器 - 持续检查 TP/SL/Invalidation 条件
Author: AI Assistant
Version: 2.0 (集成持仓时长追踪)
"""

import json
import time
import os
from pathlib import Path
from datetime import datetime, timezone

LOG_DIR = Path("logs")
POSITIONS_FILE = LOG_DIR / "active_positions.json"

def load_positions():
    """加载活跃持仓"""
    if not POSITIONS_FILE.exists():
        return []
    try:
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # ✅ 兼容两种格式
            if isinstance(data, dict) and "positions" in data:
                return data["positions"]
            elif isinstance(data, list):
                return data
            else:
                return []
    except Exception as e:
        print(f"⚠️ 加载持仓失败: {e}")
        return []

def save_positions(positions):
    """保存持仓（标准格式）"""
    try:
        with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
            # ✅ 使用标准格式（与 ai_trader.py 保持一致）
            json.dump({
                "positions": positions,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ 保存持仓失败: {e}")

def check_exit_conditions(positions, market_data):
    """检查退出条件"""
    to_close = []
    
    for pos in positions:
        sym = pos["symbol"]
        entry_price = pos["entry_price"]
        side = pos["side"]
        
        # 获取当前价格
        current_price = market_data.get(sym, {}).get("price")
        if not current_price:
            continue
        
        # ✅ 计算当前盈亏比例（用于日志）
        if side == "buy":
            pnl_pct = ((current_price - entry_price) / entry_price) * 100
        else:  # sell/short
            pnl_pct = ((entry_price - current_price) / entry_price) * 100
        
        # 考虑杠杆
        leverage = pos.get("leverage") or 1.0
        pnl_pct *= float(leverage)
        
        exit_plan = pos.get("exit_plan", {})
        sl_pct = exit_plan.get("stop_loss_pct")
        tp_pct = exit_plan.get("take_profit_pct")
        invalidation = exit_plan.get("invalidation_condition", "")
        
        # ✅ 添加条件触发标志
        should_exit = False
        exit_reason = ""
        
        # 1. 检查止盈
        if tp_pct:
            tp_threshold = tp_pct * 100  # 转换为百分比
            if side == "buy" and current_price >= entry_price * (1 + tp_pct):
                should_exit = True
                exit_reason = f"止盈触发 ({pnl_pct:+.2f}% >= {tp_threshold:.2f}%)"
            elif side == "sell" and current_price <= entry_price * (1 - tp_pct):
                should_exit = True
                exit_reason = f"止盈触发 ({pnl_pct:+.2f}% >= {tp_threshold:.2f}%)"
        
        # 2. 检查止损（优先级高于止盈）
        if sl_pct and not should_exit:
            sl_threshold = sl_pct * 100
            if side == "buy" and current_price <= entry_price * (1 - sl_pct):
                should_exit = True
                exit_reason = f"止损触发 ({pnl_pct:+.2f}% <= -{sl_threshold:.2f}%)"
            elif side == "sell" and current_price >= entry_price * (1 + sl_pct):
                should_exit = True
                exit_reason = f"止损触发 ({pnl_pct:+.2f}% <= -{sl_threshold:.2f}%)"
        
        # 3. 检查失效条件
        if invalidation and not should_exit:
            try:
                from risk_manager import parse_invalidation_condition
                if parse_invalidation_condition(invalidation, sym, market_data):
                    should_exit = True
                    exit_reason = f"失效条件触发: {invalidation}"
            except Exception as e:
                print(f"⚠️ 失效条件解析失败: {e}")
        
        if should_exit:
            to_close.append({
                "position": pos,
                "reason": exit_reason,
                "exit_price": current_price,
                "pnl_pct": pnl_pct  # ✅ 传递盈亏比例
            })
    
    return to_close

def execute_close(position, reason, exit_price, pnl_pct):
    """
    执行平仓（增强版）
    
    Args:
        position: 持仓信息
        reason: 平仓原因
        exit_price: 平仓价格
        pnl_pct: 盈亏比例（%）
    """
    from bridge_to_flask import _http_post
    
    order = {
        "symbol": position["symbol"],
        "side": "sell" if position["side"] == "buy" else "buy",
        "size": position["size"],
        "order_type": "market"
    }
    
    try:
        resp = _http_post("/order", order)
        
        # ✅ 计算绝对盈亏（USDT）
        if position["side"] == "buy":
            pnl = (exit_price - position["entry_price"]) * position["size"]
        else:
            pnl = (position["entry_price"] - exit_price) * position["size"]
        
        # 考虑杠杆
        leverage = position.get("leverage") or 1.0
        pnl *= float(leverage)
        
        # ===== ✅ 新增：调用持仓时长记录 =====
        try:
            # 导入 position_analytics（如果存在）
            import sys
            if 'position_analytics' not in sys.modules:
                import position_analytics
            
            position_analytics.close_position(
                symbol=position["symbol"],
                exit_time=datetime.now(timezone.utc).isoformat(),
                exit_price=exit_price,
                profit_pct=pnl_pct,  # 使用计算好的盈亏比例
                exit_reason=reason
            )
        except ImportError:
            print("⚠️ position_analytics 模块未找到，跳过持仓时长记录")
        except Exception as e:
            print(f"⚠️ 记录持仓时长失败: {e}")
        
        # 记录平仓日志（保留原有逻辑）
        log_entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "symbol": position["symbol"],
            "side": position["side"],
            "entry_price": position["entry_price"],
            "exit_price": exit_price,
            "size": position["size"],
            "leverage": position.get("leverage"),
            "exit_reason": reason,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "response": resp
        }
        
        log_file = LOG_DIR / "exit_log.jsonl"
        with log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        
        print(f"✅ 平仓成功: {position['symbol']} | {reason} | 盈亏: {pnl_pct:+.2f}% ({pnl:+.2f} USDT)")
        return True
    
    except Exception as e:
        print(f"❌ 平仓失败: {e}")
        return False

def monitor_loop():
    """主监控循环"""
    print("🔍 退出计划监控器启动...")
    print(f"📂 持仓文件: {POSITIONS_FILE}")
    
    consecutive_errors = 0
    max_errors = 5
    
    while True:
        try:
            positions = load_positions()
            
            if not positions:
                # ✅ 优化：没有持仓时降低检查频率
                print(f"⏳ [{datetime.now().strftime('%H:%M:%S')}] 暂无活跃持仓，等待 30 秒...")
                time.sleep(30)
                consecutive_errors = 0  # 重置错误计数
                continue
            
            print(f"\n{'='*70}")
            print(f"🔍 [{datetime.now().strftime('%H:%M:%S')}] 检查 {len(positions)} 个持仓...")
            print(f"{'='*70}")
            
            # 获取市场数据
            from ai_trader import fetch_market
            market = fetch_market()
            
            # 检查退出条件
            to_close = check_exit_conditions(positions, market)
            
            if to_close:
                print(f"⚠️ 发现 {len(to_close)} 个触发退出条件的持仓")
            
            # 执行平仓
            for item in to_close:
                if execute_close(
                    item["position"], 
                    item["reason"], 
                    item["exit_price"],
                    item["pnl_pct"]
                ):
                    # ✅ 从列表中移除已平仓
                    positions = [p for p in positions if p != item["position"]]
            
            # 更新持仓文件
            if to_close:
                save_positions(positions)
            
            # ✅ 打印剩余持仓状态
            if positions:
                print(f"\n📊 剩余持仓: {len(positions)}")
                for pos in positions:
                    current_price = market.get(pos["symbol"], {}).get("price", 0)
                    entry_price = pos["entry_price"]
                    if pos["side"] == "buy":
                        unrealized = ((current_price - entry_price) / entry_price) * 100
                    else:
                        unrealized = ((entry_price - current_price) / entry_price) * 100
                    
                    leverage = pos.get("leverage") or 1.0
                    unrealized *= float(leverage)
                    
                    print(f"  • {pos['symbol']}: {unrealized:+.2f}% (入场 {entry_price:.2f}, 当前 {current_price:.2f})")
            
            consecutive_errors = 0  # 重置错误计数
            
        except KeyboardInterrupt:
            print("\n⏹️ 用户中断，退出监控...")
            break
        except Exception as e:
            consecutive_errors += 1
            print(f"❌ 监控循环异常 ({consecutive_errors}/{max_errors}): {e}")
            
            if consecutive_errors >= max_errors:
                print("⚠️ 连续错误过多，暂停 60 秒...")
                time.sleep(60)
                consecutive_errors = 0
        
        # ✅ 动态检查间隔（有持仓时更频繁）
        check_interval = 15 if positions else 30
        time.sleep(check_interval)

if __name__ == "__main__":
    monitor_loop()