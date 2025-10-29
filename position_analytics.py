# -*- coding: utf-8 -*-
"""
持仓时长分析工具
- 统计平均持仓时长
- 分析盈亏持仓的时长差异
- 生成可视化报告
"""

import json
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List

LOG_DIR = Path("logs")
ACTIVE_POS_FILE = LOG_DIR / "active_positions.json"
HISTORY_FILE = LOG_DIR / "position_history.json"  # 新建历史记录文件


def load_active_positions() -> List[Dict]:
    """加载当前活跃持仓"""
    if not ACTIVE_POS_FILE.exists():
        return []
    try:
        data = json.loads(ACTIVE_POS_FILE.read_text(encoding="utf-8"))
        return data.get("positions", [])
    except Exception as e:
        print(f"⚠️ 加载活跃持仓失败: {e}")
        return []


def load_position_history() -> List[Dict]:
    """加载历史持仓记录"""
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️ 加载历史持仓失败: {e}")
        return []


def save_position_history(history: List[Dict]):
    """保存历史持仓记录"""
    try:
        HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"❌ 保存历史持仓失败: {e}")


def close_position(symbol: str, exit_time: str = None, exit_price: float = None, 
                   profit_pct: float = None, exit_reason: str = ""):
    """
    记录平仓事件（从 exit_plan_monitor.py 或 bridge_to_flask.py 调用）
    
    Args:
        symbol: 交易对（如 BTC-USDT）
        exit_time: 平仓时间（ISO 格式）
        exit_price: 平仓价格
        profit_pct: 收益率（%）
        exit_reason: 平仓原因（止盈/止损/手动）
    """
    active = load_active_positions()
    history = load_position_history()
    
    # 查找匹配的持仓
    matched = None
    remaining = []
    
    for pos in active:
        if pos.get("symbol") == symbol:
            matched = pos
        else:
            remaining.append(pos)
    
    if not matched:
        print(f"⚠️ 未找到 {symbol} 的活跃持仓")
        return
    
    # 计算持仓时长
    entry_time = datetime.fromisoformat(matched["entry_time"].replace("Z", "+00:00"))
    exit_time_dt = datetime.fromisoformat((exit_time or datetime.now(timezone.utc).isoformat()).replace("Z", "+00:00"))
    duration_hours = (exit_time_dt - entry_time).total_seconds() / 3600
    
    # 构造历史记录
    record = {
        **matched,  # 包含 entry_price, size, exit_plan 等
        "exit_time": exit_time or datetime.now(timezone.utc).isoformat(),
        "exit_price": exit_price,
        "profit_pct": profit_pct,
        "exit_reason": exit_reason,
        "duration_hours": round(duration_hours, 2),
        "duration_days": round(duration_hours / 24, 2)
    }
    
    # 更新文件
    history.append(record)
    save_position_history(history)
    
    # 更新活跃持仓（移除已平仓）
    from exit_plan_monitor import save_positions
    save_positions(remaining)
    
    print(f"✅ 已记录 {symbol} 平仓: 持仓 {duration_hours:.2f} 小时, 收益 {profit_pct or 0:.2f}%")


def analyze_positions():
    """
    生成持仓时长分析报告
    
    Returns:
        Dict: 包含统计数据的字典
    """
    history = load_position_history()
    
    if not history:
        return {
            "error": "暂无历史持仓数据",
            "suggestion": "请先运行交易系统积累数据"
        }
    
    df = pd.DataFrame(history)
    
    # === 基础统计 ===
    total_positions = len(df)
    avg_duration_hours = df["duration_hours"].mean()
    median_duration_hours = df["duration_hours"].median()
    max_duration = df["duration_hours"].max()
    min_duration = df["duration_hours"].min()
    
    # === 盈亏分组分析 ===
    profitable = df[df["profit_pct"] > 0]
    loss_making = df[df["profit_pct"] <= 0]
    
    profitable_avg = profitable["duration_hours"].mean() if len(profitable) > 0 else 0
    loss_avg = loss_making["duration_hours"].mean() if len(loss_making) > 0 else 0
    
    # === 按币种统计 ===
    by_symbol = df.groupby("symbol").agg({
        "duration_hours": ["mean", "count"],
        "profit_pct": "mean"
    }).round(2)
    
    # === 杠杆分析（如果有）===
    if "leverage" in df.columns:
        by_leverage = df.groupby("leverage")["duration_hours"].mean().round(2)
    else:
        by_leverage = None
    
    report = {
        "total_positions": total_positions,
        "average_duration_hours": round(avg_duration_hours, 2),
        "average_duration_days": round(avg_duration_hours / 24, 2),
        "median_duration_hours": round(median_duration_hours, 2),
        "max_duration_hours": round(max_duration, 2),
        "min_duration_hours": round(min_duration, 2),
        "profitable_count": len(profitable),
        "profitable_avg_hours": round(profitable_avg, 2),
        "loss_count": len(loss_making),
        "loss_avg_hours": round(loss_avg, 2),
        "by_symbol": by_symbol.to_dict(),
        "by_leverage": by_leverage.to_dict() if by_leverage is not None else None,
        "latest_10": df.tail(10)[["symbol", "entry_time", "exit_time", "duration_hours", "profit_pct", "exit_reason"]].to_dict("records")
    }
    
    return report


def print_report():
    """打印美化的分析报告"""
    report = analyze_positions()
    
    if "error" in report:
        print(f"\n❌ {report['error']}")
        print(f"💡 {report['suggestion']}\n")
        return
    
    print("\n" + "="*70)
    print("📊 持仓时长分析报告")
    print("="*70)
    
    print(f"\n📈 总体统计:")
    print(f"  总持仓数: {report['total_positions']} 笔")
    print(f"  平均持仓: {report['average_duration_hours']:.2f} 小时 ({report['average_duration_days']:.2f} 天)")
    print(f"  中位数: {report['median_duration_hours']:.2f} 小时")
    print(f"  最长: {report['max_duration_hours']:.2f} 小时")
    print(f"  最短: {report['min_duration_hours']:.2f} 小时")
    
    print(f"\n💰 盈亏对比:")
    print(f"  盈利仓位: {report['profitable_count']} 笔, 平均持仓 {report['profitable_avg_hours']:.2f} 小时")
    print(f"  亏损仓位: {report['loss_count']} 笔, 平均持仓 {report['loss_avg_hours']:.2f} 小时")
    
    if report['profitable_avg_hours'] > 0 and report['loss_avg_hours'] > 0:
        ratio = report['profitable_avg_hours'] / report['loss_avg_hours']
        if ratio > 1.2:
            print(f"  ✅ 盈利仓持仓时长是亏损仓的 {ratio:.2f} 倍（良好趋势）")
        elif ratio < 0.8:
            print(f"  ⚠️ 盈利仓持仓时长短于亏损仓（可能过早止盈）")
        else:
            print(f"  ℹ️ 盈亏持仓时长比例: {ratio:.2f}")
    
    print(f"\n🪙 按币种统计:")
    for sym, data in report["by_symbol"].items():
        print(f"  {sym}: 平均 {data['duration_hours']['mean']:.2f} 小时 ({data['duration_hours']['count']} 笔), 平均收益 {data['profit_pct']['mean']:.2f}%")
    
    if report["by_leverage"]:
        print(f"\n⚡ 按杠杆统计:")
        for lev, avg_hours in report["by_leverage"].items():
            print(f"  {lev}x: 平均持仓 {avg_hours:.2f} 小时")
    
    print(f"\n📋 最近 10 笔平仓:")
    for i, pos in enumerate(report["latest_10"][-10:], 1):
        duration = pos.get("duration_hours", 0)
        profit = pos.get("profit_pct", 0)
        reason = pos.get("exit_reason", "未知")
        print(f"  {i}. {pos['symbol']} | {duration:.2f}h | {profit:+.2f}% | {reason}")
    
    print("="*70 + "\n")


if __name__ == "__main__":
    # 测试用例
    print("🧪 测试模式：生成模拟数据...\n")
    
    # 模拟一些历史数据（实际使用时删除此段）
    test_history = [
        {
            "symbol": "BTC-USDT",
            "entry_time": "2025-10-28T10:00:00Z",
            "exit_time": "2025-10-29T02:00:00Z",
            "entry_price": 68000,
            "exit_price": 69500,
            "size": 0.1,
            "leverage": 3,
            "profit_pct": 2.20,
            "duration_hours": 16.0,
            "exit_reason": "止盈"
        },
        {
            "symbol": "ETH-USDT",
            "entry_time": "2025-10-28T14:00:00Z",
            "exit_time": "2025-10-28T18:00:00Z",
            "entry_price": 2500,
            "exit_price": 2480,
            "size": 1.0,
            "leverage": 2,
            "profit_pct": -0.80,
            "duration_hours": 4.0,
            "exit_reason": "止损"
        },
        {
            "symbol": "BTC-USDT",
            "entry_time": "2025-10-27T08:00:00Z",
            "exit_time": "2025-10-28T08:00:00Z",
            "entry_price": 67500,
            "exit_price": 68200,
            "size": 0.15,
            "leverage": 4,
            "profit_pct": 1.04,
            "duration_hours": 24.0,
            "exit_reason": "手动平仓"
        }
    ]
    
    save_position_history(test_history)
    print_report()