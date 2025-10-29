# -*- coding: utf-8 -*-
"""
专业量化交易风控管理器 (Production Ready)
Author: AI Assistant
Version: 2.0
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, date
import json, math, os, time

@dataclass
class SymbolRule:
    """交易品种规则"""
    price_tick: float = 0.1
    lot_size_min: float = 0.0001
    lot_size_step: float = 0.0001

@dataclass
class RiskConfig:
    """风控配置参数"""
    # 账户级限制
    daily_loss_limit_pct: float = 0.03        # 日内最大亏损 3%
    max_open_risk_pct: float = 0.03           # 最大开仓风险 3%
    max_gross_exposure_pct: float = 1.0       # 最大总敞口 100%
    balance_reserve_pct: float = 0.10         # 保留余额比例 10%
    
    # 连续亏损保护
    max_consecutive_losses: int = 3           # 最多连亏次数
    cooldown_global_sec: int = 900            # 全局冷却时间 15分钟
    
    # 品种级限制
    max_symbol_exposure_pct: float = 0.30     # 单品种最大敞口 30%
    symbol_cooldown_sec: int = 180            # 品种冷却时间 3分钟
    
    # 🆕 动态仓位管理参数
    risk_per_trade_pct: float = 0.005         # 基础风险 0.5%（会被动态调整）
    risk_min_pct: float = 0.002               # 最小风险 0.2%
    risk_max_pct: float = 0.015               # 最大风险 1.5%
    max_trade_ratio: float = 0.30             # 单笔最大交易占比 30%

    # 🆕 波动率阈值
    volatility_low_bps: int = 30              # 低波动阈值 30bps
    volatility_high_bps: int = 100            # 高波动阈值 100bps
    
    # 价格保护
    max_slippage_bps: int = 20                # 最大滑点 20bps
    deviation_guard_bps: int = 30             # 限价偏离保护 30bps
    
    # ATR参数
    atr_lookback: int = 14                    # ATR回看周期
    atr_floor_bps: int = 25                   # ATR下限 25bps
    atr_mult_stop: float = 2.0                # 止损倍数
    atr_mult_tp: float = 3.0                  # 止盈倍数
    
    # 费用
    fee_rate_bps: float = 8.0                 # 费率 8bps
    
    # 品种规则
    symbol_rules: dict | None = None

@dataclass
class RiskState:
    """风控状态"""
    date: str                                 # 当前日期
    day_open_equity: float                    # 日初权益
    realized_pnl_today: float = 0.0           # 今日已实现盈亏
    consecutive_losses: int = 0               # 连续亏损次数
    consecutive_wins: int = 0                 # 🆕 连续盈利次数
    last_trade_ts: dict | None = None         # 最后交易时间戳
    symbol_exposure: dict | None = None       # 品种敞口
    open_positions: dict | None = None        # 持仓信息

    def ensure(self):
        """确保字典字段初始化"""
        self.last_trade_ts = self.last_trade_ts or {}
        self.symbol_exposure = self.symbol_exposure or {}
        self.open_positions = self.open_positions or {}

def _now_ts() -> int:
    """获取当前时间戳"""
    return int(time.time())

def _floor_to_step(x: float, step: float) -> float:
    """向下取整到指定步长"""
    if step <= 0:
        return x
    return math.floor(x / step) * step

def _align_size(size: float, rule: SymbolRule) -> float:
    """
    对齐数量到步长
    注意：只做对齐，不强制提升到最小值
    """
    if size <= 0:
        return 0.0
    return _floor_to_step(float(size), rule.lot_size_step)

def _align_price(price: float, rule: SymbolRule) -> float:
    """对齐价格到最小变动单位"""
    return _floor_to_step(float(price), rule.price_tick)

def _pct(a: float, b: float) -> float:
    """计算百分比"""
    if b <= 0:
        return 0.0
    return a / b

def _is_new_day(stored_date: str) -> bool:
    """检查是否新的一天"""
    today = date.today().isoformat()
    return stored_date != today


class RiskManager:
    """专业量化交易风控管理器"""
    
    def __init__(self, cfg: RiskConfig, state_path: str = "risk_state.json"):
        """
        初始化风控管理器
        
        Args:
            cfg: 风控配置
            state_path: 状态文件路径
        """
        self.cfg = cfg
        self.state_path = state_path
        self.price_history = {}
        self.state = self._load_state()
        # 🧠 当前冷却时间（Adaptive Unlock 使用）
        self.current_cooldown = 0

    def _load_state(self) -> RiskState:
        """加载风控状态"""
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                st = RiskState(**d)
                st.ensure()
            except Exception:
                st = self._new_state()
        else:
            st = self._new_state()
        
        # 检查是否新的一天
        if _is_new_day(st.date):
            old_equity = self._estimate_equity()
            if old_equity > 0:
                st.day_open_equity = old_equity
            st.date = date.today().isoformat()
            st.realized_pnl_today = 0.0
            st.consecutive_losses = 0
        
        return st

    def _new_state(self) -> RiskState:
        """创建新状态"""
        equity = self._estimate_equity()
        if equity <= 0:
            equity = 100000.0  # 默认值
        
        return RiskState(
            date=date.today().isoformat(),
            day_open_equity=equity,
            realized_pnl_today=0.0,
            consecutive_losses=0,
            last_trade_ts={},
            symbol_exposure={},
            open_positions={}
        )

    def _save_state(self):
        """保存风控状态"""
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(asdict(self.state), f, ensure_ascii=False, indent=2)

    # 外部数据提供者（可选）
    equity_provider = None
    price_provider = None
    exposure_provider = None

    def _estimate_equity(self) -> float:
        """估算账户权益"""
        if callable(self.equity_provider):
            try:
                return float(self.equity_provider())
            except Exception:
                pass
        return 0.0

    # ===============================================================
    # 🧠 自适应冷却系统 (Adaptive Cooldown System)
    # ===============================================================
    @staticmethod
    def adaptive_cooldown(consecutive_losses: int,
                        avg_drawdown: float,
                        volatility: float,
                        ai_confidence: float) -> int:
        """
        自适应冷却时间计算器

        Args:
            consecutive_losses: 连续亏损次数
            avg_drawdown: 平均回撤比例 (0.05 = 5%)
            volatility: 市场波动率（基于 ATR 或标准差, 例如 0.02 = 2%）
            ai_confidence: 当前 AI 信号置信度 (0~1)

        Returns:
            冷却时间（秒）
        """

        # 🧩 基础冷却时间：5分钟
        base_time = 300

        # 📉 连亏倍数（每多亏一次增加 30%）
        loss_factor = 1 + consecutive_losses * 0.3

        # 📉 回撤影响（平均回撤每增加5%，冷却延长 50%）
        dd_factor = 1 + (avg_drawdown / 0.05) * 0.5

        # 📈 波动率影响（波动率高 → 冷却更久）
        vola_factor = 1 + (volatility / 0.02) * 0.5

        # 🧠 AI 置信度影响（信心低于 0.5 → 冷却时间翻倍）
        conf_factor = 2 if ai_confidence < 0.5 else 1

        # 🧮 最终冷却时间计算
        cooldown_time = base_time * loss_factor * dd_factor * vola_factor * conf_factor

        # ⚙️ 限制范围：最短 3分钟，最长 1小时
        cooldown_time = int(max(180, min(cooldown_time, 3600)))

        return cooldown_time

    def calculate_dynamic_position_size(
        self,
        base_risk_pct: float,
        ai_confidence: float,
        volatility_bps: float,
        consecutive_losses: int,
        consecutive_wins: int,
        equity: float
    ) -> float:
        """
        🧠 动态仓位计算器
        
        Args:
            base_risk_pct: 基础风险比例（例如 0.005 = 0.5%）
            ai_confidence: AI 置信度 (0~1)
            volatility_bps: 市场波动率（bps）
            consecutive_losses: 连续亏损次数
            consecutive_wins: 连续盈利次数
            equity: 当前权益
        
        Returns:
            调整后的风险金额
        """
        
        # 📊 第一层：AI 置信度调整（0.5x ~ 1.5x）
        conf_scale = 0.5 + ai_confidence  # 置信度 0 → 0.5x, 1 → 1.5x
        
        # 📈 第二层：波动率调整
        if volatility_bps < self.cfg.volatility_low_bps:
            # 低波动 → 提高仓位 20%
            vola_scale = 1.2
        elif volatility_bps > self.cfg.volatility_high_bps:
            # 高波动 → 降低仓位 40%
            vola_scale = 0.6
        else:
            # 正常波动 → 线性插值
            ratio = (volatility_bps - self.cfg.volatility_low_bps) / max(1, self.cfg.volatility_high_bps - self.cfg.volatility_low_bps)
            vola_scale = 1.2 - 0.6 * ratio  # 从 1.2 线性下降到 0.6
        
        # 🎯 第三层：账户状态调整
        if consecutive_losses >= 2:
            # 连亏 2 次以上 → 减半仓位
            state_scale = 0.5
        elif consecutive_wins >= 3:
            # 连赢 3 次以上 → 提高仓位 30%
            state_scale = 1.3
        else:
            state_scale = 1.0
        
        # 🧮 综合计算
        adjusted_risk_pct = base_risk_pct * conf_scale * vola_scale * state_scale
        
        # ⚠️ 限制范围
        adjusted_risk_pct = max(self.cfg.risk_min_pct, min(adjusted_risk_pct, self.cfg.risk_max_pct))
        
        # 💰 转换为风险金额
        risk_amount = adjusted_risk_pct * equity
        
        # 📝 日志输出（调试用）
        print(f"[Dynamic Sizing] base={base_risk_pct:.3%}, conf={conf_scale:.2f}x, "
            f"vola={vola_scale:.2f}x, state={state_scale:.2f}x → final={adjusted_risk_pct:.3%}")
        
        return risk_amount


    def pre_trade_checks(
        self, 
        decision: dict, 
        market: dict, 
        balance: dict
    ) -> tuple[bool, dict | None, str]:
        """
        交易前风控检查
        
        优化后的检查顺序：
        1. 快速失败（数据完整性）
        2. 硬限制（kill switch）
        3. 频率限制
        4. 风险计算
        
        Args:
            decision: 交易决策 {"decision": {"symbol", "side", ...}}
            market: 市场数据 {symbol: {"price": ...}}
            balance: 账户余额
            
        Returns:
            (approved, order, reason)
            - approved: 是否通过
            - order: 订单信息（通过时）
            - reason: 原因说明
        """

# ========== 阶段 1: 快速失败检查 ==========
        # ✅ 第一步：提取并验证决策结构
        try:
            d = decision.get("decision", {})
            sym = d.get("symbol")
            side = d.get("side")
        except Exception:
            return False, None, "invalid decision structure"

        # ✅ 第二步：基础验证
        if not sym:
            return False, None, "missing symbol"

        if side not in ("buy", "sell", "hold"):
            return False, None, f"invalid side: {side}"

        if side == "hold":
            return False, None, "hold (no order)"

        # ✅ 第三步：价格验证
        px = market.get(sym, {}).get("price")
        if not px or px <= 0:
            return False, None, f"price unavailable for {sym}"

        # ✅ 第四步：权益验证
        equity = self._estimate_equity()
        if equity <= 0:
            return False, None, "equity unavailable"
        
        # ========== ✅ 新增：阶段 1.5 - 3m 极端信号检测（最高优先级）==========
        tf_data = market.get(sym, {}).get("tf", {})
        ctx3m = tf_data.get("3m", {})
        
        if ctx3m:
            rsi3m = float(ctx3m.get("rsi14") or 50.0)
            adx3m = float(ctx3m.get("adx14") or 0.0)
            
            # 规则 1: 极端超买（RSI > 90）
            if rsi3m > 90:
                return False, None, f"3m RSI extreme overbought ({rsi3m:.1f})"
            
            # 规则 2: 极端超卖（RSI < 10）
            if rsi3m < 10:
                return False, None, f"3m RSI extreme oversold ({rsi3m:.1f})"
            
            # 规则 3: 极端趋势末期（ADX > 80）
            if adx3m > 80:
                return False, None, f"3m ADX extreme ({adx3m:.1f})"


        # ========== 阶段 2: Kill Switch（日亏损限制） ==========
        dd = (equity - self.state.day_open_equity) / max(self.state.day_open_equity, 1e-9)
        if dd <= -self.cfg.daily_loss_limit_pct:
            return False, None, f"kill-switch: daily loss {dd:.2%}"

        # ========== 阶段 2.3: Invalidation Condition 检查 ==========
        inv_cond = decision["decision"].get("exit_plan", {}).get("invalidation_condition")
        if inv_cond:
            is_invalid, reason = self._check_invalidation(inv_cond, sym, market)
            if is_invalid:
                return False, None, f"invalidation: {reason}"

# ========== 阶段 2.5: 自适应冷却系统 ==========
        # 第一步：计算动态冷却时间
        avg_drawdown = abs((equity - self.state.day_open_equity) / max(self.state.day_open_equity, 1e-9))
        volatility = self._atr_proxy_bps(sym) / 1e4  # 转为百分比（例如 0.02 = 2%）
        ai_confidence = decision["decision"].get("confidence", 0.7)

        dynamic_cooldown = self.adaptive_cooldown(
            consecutive_losses=self.state.consecutive_losses,
            avg_drawdown=avg_drawdown,
            volatility=volatility,
            ai_confidence=ai_confidence
        )

        # 第二步：确定使用哪种冷却模式
        if self.cfg.cooldown_global_sec <= 60:
            # 测试模式：使用超短固定冷却（≤60秒）
            cooldown_time = self.cfg.cooldown_global_sec
            cooldown_mode = "fixed-test"
        else:
            # 生产模式：使用动态冷却
            cooldown_time = dynamic_cooldown
            cooldown_mode = "adaptive"

        # 第三步：保存当前冷却时间（供外部查询）
        self.current_cooldown = cooldown_time

        # 第四步：检查是否触发连亏冷却
        if self.state.consecutive_losses >= self.cfg.max_consecutive_losses:
            if not self.state.last_trade_ts:
                # 没有历史交易记录，跳过冷却检查
                pass
            else:
                last_ts = max(self.state.last_trade_ts.values())
                elapsed = _now_ts() - last_ts
                
                if elapsed < cooldown_time:
                    # ✅ 第五步：智能提前解锁检测
                    can_unlock = (
                        volatility < 0.01 and           # 波动率 < 1%
                        ai_confidence >= 0.80           # AI 置信度 >= 0.8
                    )
                    
                    if can_unlock:
                        print(f"🔓 [Adaptive Unlock] 波动={volatility:.2%}, 置信度={ai_confidence:.2f} → 提前解锁！")
                        self.state.consecutive_losses = 0  # 重置连亏计数
                        # 继续往下执行，不返回
                    else:
                        # 冷却中，拒绝交易
                        remaining = cooldown_time - elapsed
                        print(f"⏸️  [Cooldown] 模式={cooldown_mode}, 总时长={cooldown_time}s, 剩余={remaining:.0f}s")
                        return False, None, f"global cooldown ({cooldown_mode}, {remaining:.0f}s left)"

        # ========== 阶段 3: 频率限制 ==========
        last_ts = self.state.last_trade_ts.get(sym, 0)
        if _now_ts() - last_ts < self.cfg.symbol_cooldown_sec:
            return False, None, f"symbol cooldown {sym}"

        # ========== 阶段 4: 价格偏离检查 ==========
        limit_px = decision["decision"].get("limit_price")
        order_type = decision["decision"].get("order_type", "market")
        rule = self._get_rule(sym)

        if order_type == "limit" and limit_px:
            limit_px = _align_price(limit_px, rule)
            dev_bps = abs(limit_px - px) / px * 1e4
            if dev_bps > self.cfg.deviation_guard_bps:
                return False, None, f"limit price deviates {dev_bps:.1f}bps > guard"

        # ========== 阶段 5: 余额约束 ==========
        if isinstance(balance.get("USDT"), dict):
            avail_usdt = float(balance["USDT"].get("available", 0.0))
        else:
            avail_usdt = float(balance.get("available", 0.0))
        
        symbol_expo_cap = self.cfg.max_symbol_exposure_pct * equity
        reserve_usdt = self.cfg.balance_reserve_pct * equity
        spendable_usdt = max(0.0, avail_usdt - reserve_usdt)

        # ========== 阶段 6: 动态 Sizing ==========
        risk = decision["decision"].get("risk") or {}
        if isinstance(risk.get("stop_loss_pct"), (int, float)) and risk["stop_loss_pct"] > 0:
            stop_pct = float(risk["stop_loss_pct"])
        else:
            atr_bps = max(self.cfg.atr_floor_bps, self._atr_proxy_bps(sym))
            stop_pct = self.cfg.atr_mult_stop * atr_bps * 1e-4

        # 🆕 使用动态仓位计算器
        raw_conf = decision["decision"].get("confidence", 0.7)
        ai_confidence = max(0.0, min(1.0, float(raw_conf)))

        R = self.calculate_dynamic_position_size(
            base_risk_pct=self.cfg.risk_per_trade_pct,
            ai_confidence=ai_confidence,
            volatility_bps=self._atr_proxy_bps(sym),
            consecutive_losses=self.state.consecutive_losses,
            consecutive_wins=self.state.consecutive_wins,
            equity=equity
        )
        
        stop_distance = max(stop_pct * px, 1e-9)
        size_raw = R / stop_distance

        # 计算各种约束
        cap1 = symbol_expo_cap / px
        cap2 = self.cfg.max_trade_ratio * equity / px
        cap3 = spendable_usdt / (px * (1 + self.cfg.fee_rate_bps * 1e-4))

        # 模型建议的数量
        model_size = decision["decision"].get("size")
        if isinstance(model_size, (int, float)) and model_size > 0:
            size_raw = min(size_raw, float(model_size))

        # 应用所有约束
        size = max(0.0, min(size_raw, cap1, cap2, cap3))
        
        # ✅ 对齐到步长（不强制提升）
        size = _align_size(size, rule)
        
        # ✅ 检查是否低于最小值
        if size < rule.lot_size_min:
            return False, None, f"size {size:.6f} below min {rule.lot_size_min}"

        
# ========== 阶段 7: 期望收益比检查 ==========
        risk = decision["decision"].get("risk") or {}
        tp_pct = float(risk.get("take_profit_pct") or 0.0)
        sl_pct = float(risk.get("stop_loss_pct") or 0.0)

        if tp_pct > 0 and sl_pct > 0:
            # ✅ 第一步：计算原始风险回报比（不含手续费）
            raw_r = tp_pct / sl_pct
            
            # ✅ 第二步：计算有效风险回报比（考虑双边手续费）
            fee = self.cfg.fee_rate_bps * 1e-4  # 单边费率（例如 0.0008 = 0.08%）
            total_fee_impact = 2 * fee           # 开仓 + 平仓
            
            # 有效止盈 = 止盈 - 手续费
            # 有效止损 = 止损 + 手续费
            effective_tp = max(0.0, tp_pct - total_fee_impact)
            effective_sl = sl_pct + total_fee_impact
            effective_r = effective_tp / max(1e-9, effective_sl)
            
            # ✅ 第三步：分级拦截
            # 规则 1：原始 R < 1.5 直接拒绝（设计问题）
            if raw_r < 1.5:
                return False, None, f"raw R too low ({raw_r:.2f} < 1.5)"
            
            # 规则 2：有效 R < 1.0 拒绝（扣费后无利可图）
            if effective_r < 1.0:
                return False, None, f"effective R after fees ({effective_r:.2f} < 1.0)"
            
            # ✅ 第四步：记录日志（调试用）
            print(f"[R-Check] raw={raw_r:.2f}, effective={effective_r:.2f}, "
                  f"tp={tp_pct:.2%}, sl={sl_pct:.2%}, fee_impact={total_fee_impact:.2%}")

        # ========== 阶段 7: 风险合规检查 ==========
        open_risk_after = self._estimate_open_risk_after(sym, side, size, px, stop_pct)
        if open_risk_after > self.cfg.max_open_risk_pct * equity:
            return False, None, "open risk exceed cap"

        # ========== 通过所有检查 ==========
        order = {
            "symbol": sym,
            "side": side,
            "order_type": order_type,
            "size": size,
            "limit_price": limit_px if order_type == "limit" else None,
        }
        
        return True, order, "ok"

    def post_trade_update(
        self, 
        symbol: str, 
        filled_size: float, 
        fill_price: float, 
        realized_pnl: float, 
        side: str = "buy"
    ):
        """
        交易后状态更新
        
        Args:
            symbol: 交易品种
            filled_size: 成交数量
            fill_price: 成交价格
            realized_pnl: 已实现盈亏
            side: 交易方向
        """
        # 更新时间戳
        self.state.last_trade_ts[symbol] = _now_ts()
        
        # 更新今日盈亏
        self.state.realized_pnl_today += realized_pnl
        
        # 🆕 更新连亏/连赢计数
        if realized_pnl < 0:
            self.state.consecutive_losses += 1
            self.state.consecutive_wins = 0  # 重置连赢
        elif realized_pnl > 0:
            self.state.consecutive_losses = 0  # 重置连亏
            self.state.consecutive_wins += 1
        
        # 更新持仓
        if symbol not in self.state.open_positions:
            self.state.open_positions[symbol] = {
                "side": side,
                "qty": filled_size,
                "avg_price": fill_price
            }
        else:
            pos = self.state.open_positions[symbol]
            total_qty = pos["qty"] + filled_size
            if total_qty != 0:
                pos["avg_price"] = (pos["qty"] * pos["avg_price"] + filled_size * fill_price) / total_qty
                pos["qty"] = total_qty
            else:
                # 平仓
                del self.state.open_positions[symbol]
        
        # 更新敞口
        self.state.symbol_exposure[symbol] = abs(filled_size * fill_price)
        
        # 保存状态
        self._save_state()

    def push_price(self, symbol: str, price: float, maxlen: int = 256):
        """
        推送价格历史（用于ATR计算）
        
        Args:
            symbol: 交易品种
            price: 价格
            maxlen: 最大保留长度
        """
        buf = self.price_history.get(symbol, [])
        buf.append(float(price))
        if len(buf) > maxlen:
            buf = buf[-maxlen:]
        self.price_history[symbol] = buf

    def _atr_proxy_bps(self, symbol: str) -> float:
        """
        计算ATR代理（基于价格历史）
        
        Args:
            symbol: 交易品种
            
        Returns:
            ATR (bps)
        """
        N = max(3, self.cfg.atr_lookback)
        seq = self.price_history.get(symbol, [])
        
        if len(seq) < 2:
            return float(self.cfg.atr_floor_bps)
        
        # 计算价格变化
        diffs = [abs(seq[i] - seq[i-1]) for i in range(1, len(seq))]
        if not diffs:
            return float(self.cfg.atr_floor_bps)
        
        # 平均变化
        avg = sum(diffs[-N:]) / min(N, len(diffs))
        px = seq[-1]
        bps = avg / max(px, 1e-9) * 1e4
        
        return max(float(self.cfg.atr_floor_bps), bps)

    def _get_rule(self, symbol: str) -> SymbolRule:
        """
        获取品种规则
        
        Args:
            symbol: 交易品种
            
        Returns:
            SymbolRule
        """
        rules = self.cfg.symbol_rules or {}
        r = rules.get(symbol)
        if isinstance(r, dict):
            return SymbolRule(**r)
        return r or SymbolRule()

    def _estimate_open_risk_after(
        self, 
        symbol: str, 
        side: str, 
        size: float, 
        px: float, 
        stop_pct: float
    ) -> float:
        """
        估算开仓后的总风险
        
        Args:
            symbol: 新开仓品种
            side: 方向
            size: 数量
            px: 价格
            stop_pct: 止损百分比
            
        Returns:
            总风险金额
        """
        # 新仓位风险
        new_risk = size * px * stop_pct
        total_risk = new_risk
        
        # 加上现有仓位风险
        for sym, pos in self.state.open_positions.items():
            if not pos or sym == symbol:
                continue
            
            pos_qty = abs(float(pos.get('qty', 0)))
            pos_px = float(pos.get('avg_price', 0))
            
            if pos_qty <= 0 or pos_px <= 0:
                continue
            
            # 估算止损百分比
            pos_stop_pct = self._atr_proxy_bps(sym) * self.cfg.atr_mult_stop * 1e-4
            total_risk += pos_qty * pos_px * pos_stop_pct
        
        return total_risk

    def get_state_summary(self) -> dict:
        """获取状态摘要"""
        equity = self._estimate_equity()
        
        return {
            "date": self.state.date,
            "equity": equity,
            "day_open_equity": self.state.day_open_equity,
            "realized_pnl_today": self.state.realized_pnl_today,
            "drawdown_pct": (equity - self.state.day_open_equity) / max(self.state.day_open_equity, 1) * 100,
            "consecutive_losses": self.state.consecutive_losses,
            "open_positions": len(self.state.open_positions),
            "symbols_on_cooldown": [
                sym for sym, ts in self.state.last_trade_ts.items()
                if _now_ts() - ts < self.cfg.symbol_cooldown_sec
            ]
        }
    

