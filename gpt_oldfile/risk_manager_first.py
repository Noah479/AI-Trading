# -*- coding: utf-8 -*-
# 专业量化 RiskManager（账户/品种/单笔/执行层级 & RPT sizing）
from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, date
import json, math, os, time

# ---------- 配置 ----------
@dataclass
class SymbolRule:
    price_tick: float = 0.1
    lot_size_min: float = 0.0001
    lot_size_step: float = 0.0001

@dataclass
class RiskConfig:
    # 账户级
    daily_loss_limit_pct: float = 0.03           # 当日最大允许回撤（3%）
    max_open_risk_pct: float = 0.03              # 未平仓合计风险上限（基于止损）
    max_gross_exposure_pct: float = 1.0          # 总名义敞口 / equity
    balance_reserve_pct: float = 0.10            # 账户保留余额比例（不参与交易）
    max_consecutive_losses: int = 3
    cooldown_global_sec: int = 900

    # 品种级
    max_symbol_exposure_pct: float = 0.30        # 单品种最大名义敞口
    symbol_cooldown_sec: int = 180

    # 单笔级
    risk_per_trade_pct: float = 0.005            # 单笔风险定额（0.5%）
    max_trade_ratio: float = 0.30                 # 单笔不超过权益的 30%
    max_slippage_bps: int = 20                    # 允许滑点
    deviation_guard_bps: int = 30                 # 报价偏离保护

    # 波动率 & 止损
    atr_lookback: int = 14
    atr_floor_bps: int = 25                       # 最小止损基点（0.25%）
    atr_mult_stop: float = 2.0                    # 止损=2×ATR
    atr_mult_tp: float = 3.0                      # 止盈=3×ATR

    # 费用/缓冲
    fee_rate_bps: float = 8.0                     # 预估手续费（万分之8=0.08%）
    symbol_rules: dict | None = None              # {"BTC-USDT": SymbolRule(...)}

@dataclass
class RiskState:
    date: str
    day_open_equity: float
    realized_pnl_today: float = 0.0
    consecutive_losses: int = 0
    last_trade_ts: dict | None = None
    symbol_exposure: dict | None = None           # 存储名义敞口（以权益占比或绝对额）
    open_positions: dict | None = None           # {symbol: {side, qty, avg_price}}

    def ensure(self):
        self.last_trade_ts = self.last_trade_ts or {}
        self.symbol_exposure = self.symbol_exposure or {}
        self.open_positions = self.open_positions or {}

# ---------- 工具 ----------
def _now_ts() -> int:
    return int(time.time())

def _floor_to_step(x: float, step: float) -> float:
    if step <= 0: return x
    return math.floor(x / step) * step

def _align_size(size: float, rule: SymbolRule) -> float:
    return max(_floor_to_step(float(size), rule.lot_size_step), rule.lot_size_min)

def _align_price(price: float, rule: SymbolRule) -> float:
    return _floor_to_step(float(price), rule.price_tick)

def _pct(a: float, b: float) -> float:
    if b <= 0: return 0.0
    return a / b

def _is_new_day(stored_date: str) -> bool:
    today = date.today().isoformat()
    return stored_date != today

# ---------- RiskManager ----------
class RiskManager:
    def __init__(self, cfg: RiskConfig, state_path: str = "risk_state.json"):
        self.cfg = cfg
        self.state_path = state_path
        self.state = self._load_state()

    def _load_state(self) -> RiskState:
        if os.path.exists(self.state_path):
            try:
                d = json.load(open(self.state_path, "r", encoding="utf-8"))
                st = RiskState(**d)
                st.ensure()
            except Exception:
                st = self._new_state()
        else:
            st = self._new_state()
        # 新一天重置
        if _is_new_day(st.date):
            st.date = date.today().isoformat()
            st.day_open_equity = self._estimate_equity()
            st.realized_pnl_today = 0.0
            st.consecutive_losses = 0
        return st

    def _new_state(self) -> RiskState:
        return RiskState(date=date.today().isoformat(), day_open_equity=self._estimate_equity(),
                         realized_pnl_today=0.0, consecutive_losses=0,
                         last_trade_ts={}, symbol_exposure={}, open_positions={})

    def _save_state(self):
        json.dump(asdict(self.state), open(self.state_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)

    # ---- 需由外部注入的实时数据（可在构造后设置）----
    equity_provider = None   # callable() -> float  (余额 + 持仓市值)
    price_provider = None    # callable(symbol) -> float
    exposure_provider = None # callable(symbol) -> float (名义=|qty*price|)

    def _estimate_equity(self) -> float:
        if callable(self.equity_provider):
            try:
                return float(self.equity_provider())
            except Exception:
                pass
        return 0.0

    # ---- 核心 API：下单前检查 + sizing ----
    def pre_trade_checks(self, decision: dict, market: dict, balance: dict) -> tuple[bool, dict | None, str]:
        """
        输入:
          decision: DeepSeek JSON 决策 (已校验字段)
          market: 价格快照，例如 {"BTC-USDT":{"price":...}}
          balance: 账户余额结构，需包含可用USDT等
        返回:
          (approved: bool, sized_order: dict | None, reason: str)
        """
        sym = decision["decision"]["symbol"]
        side = decision["decision"]["side"]
        if side not in ("buy", "sell", "hold"):
            return False, None, "invalid side"

        if side == "hold":
            return False, None, "hold (no order)"

        # 1) Kill Switch
        equity = self._estimate_equity()
        if equity <= 0:
            return False, None, "equity unavailable"
        dd = (equity - self.state.day_open_equity) / max(self.state.day_open_equity, 1e-9)
        if dd <= -self.cfg.daily_loss_limit_pct:
            return False, None, f"kill-switch: daily loss {dd:.2%}"

        if self.state.consecutive_losses >= self.cfg.max_consecutive_losses:
            last_ts = max(self.state.last_trade_ts.values(), default=0)
            if _now_ts() - last_ts < self.cfg.cooldown_global_sec:
                return False, None, "global cooldown (loss streak)"

        # 2) 频率限制（品种级）
        last_ts = self.state.last_trade_ts.get(sym, 0)
        if _now_ts() - last_ts < self.cfg.symbol_cooldown_sec:
            return False, None, f"symbol cooldown {sym}"

        # 3) 价格守门 & 滑点
        px = market.get(sym, {}).get("price")
        if not px or px <= 0:
            return False, None, "price unavailable"
        # 若模型有 limit_price，校验tick & 偏离
        limit_px = decision["decision"].get("limit_price")
        order_type = decision["decision"].get("order_type", "market")
        max_dev_bps = decision["decision"].get("max_slippage_bps", self.cfg.max_slippage_bps)
        rule = self._get_rule(sym)

        if order_type == "limit" and limit_px:
            limit_px = _align_price(limit_px, rule)
            dev_bps = abs(limit_px - px) / px * 1e4
            if dev_bps > self.cfg.deviation_guard_bps:
                return False, None, f"limit price deviates {dev_bps:.1f}bps > guard"
        else:
            # 市价单：用 max_slippage_bps 作为保护
            pass

        # 4) 敞口与余额约束
        symbol_expo_cap = self.cfg.max_symbol_exposure_pct * equity
        gross_cap = self.cfg.max_gross_exposure_pct * equity
        avail_usdt = float(balance.get("USDT", {}).get("available", 0.0))
        # 保留余额保护
        reserve_usdt = self.cfg.balance_reserve_pct * equity
        spendable_usdt = max(0.0, avail_usdt - reserve_usdt)

        # 5) 止损距离与 RPT sizing
        stop_pct = None
        risk = decision["decision"].get("risk") or {}
        if isinstance(risk.get("stop_loss_pct"), (int, float)) and risk["stop_loss_pct"] > 0:
            stop_pct = float(risk["stop_loss_pct"])
        else:
            # 用 ATR 代理：若无历史，用 floor
            atr_bps = max(self.cfg.atr_floor_bps, self._atr_proxy_bps(sym))
            stop_pct = self.cfg.atr_mult_stop * atr_bps * 1e-4

        R = self.cfg.risk_per_trade_pct * equity
        stop_distance = max(stop_pct * px, 1e-9)
        # size_raw 基于风险定额（假设直线止损）
        size_raw = R / stop_distance

        # 暴露上限（名义）
        cap1 = symbol_expo_cap / px
        cap2 = self.cfg.max_trade_ratio * equity / px
        cap3 = spendable_usdt / (px * (1 + self.cfg.fee_rate_bps * 1e-4))

        # 模型若提供 size，用 min 约束
        model_size = decision["decision"].get("size")
        if isinstance(model_size, (int, float)) and model_size > 0:
            size_raw = min(size_raw, float(model_size))

        size = max(0.0, min(size_raw, cap1, cap2, cap3))
        size = _align_size(size, rule)
        if size < rule.lot_size_min:
            return False, None, "size below lot_size_min"

        # 6) 预估下单后风险合规（未平仓合计风险校验）
        open_risk_after = self._estimate_open_risk_after(sym, side, size, px, stop_pct)
        if open_risk_after > self.cfg.max_open_risk_pct * equity:
            return False, None, "open risk exceed cap"

        # 通过 → 生成订单
        order = {
            "symbol": sym,
            "side": side,
            "order_type": order_type,
            "size": size,
            "limit_price": limit_px if order_type == "limit" else None,
        }
        return True, order, "ok"

    def post_trade_update(self, symbol: str, filled_size: float, fill_price: float, realized_pnl: float):
        # 更新状态：上次交易时间、连败计数、当日盈亏等
        self.state.last_trade_ts[symbol] = _now_ts()
        self.state.realized_pnl_today += realized_pnl
        if realized_pnl < 0:
            self.state.consecutive_losses += 1
        elif realized_pnl > 0:
            self.state.consecutive_losses = 0
        self._save_state()

    # --- 内部：ATR 代理（用本地缓存价格序列；外部可注入更专业的序列） ---
    price_history = {}  # {symbol: [p1, p2, ..., pN]}

    def push_price(self, symbol: str, price: float, maxlen: int = 256):
        buf = self.price_history.get(symbol, [])
        buf.append(float(price))
        if len(buf) > maxlen:
            buf = buf[-maxlen:]
        self.price_history[symbol] = buf

    def _atr_proxy_bps(self, symbol: str) -> float:
        # 用简单的近似：最近 N 根的平均真实波幅/价格 * 1e4
        # 由于没有K线高低价，用 |p_t - p_{t-1}| 近似 TR
        N = max(3, self.cfg.atr_lookback)
        seq = self.price_history.get(symbol, [])
        if len(seq) < 2:
            return float(self.cfg.atr_floor_bps)
        diffs = [abs(seq[i] - seq[i-1]) for i in range(1, len(seq))]
        if not diffs:
            return float(self.cfg.atr_floor_bps)
        avg = sum(diffs[-N:]) / min(N, len(diffs))
        px = seq[-1]
        bps = avg / max(px, 1e-9) * 1e4
        return max(float(self.cfg.atr_floor_bps), bps)

    def _get_rule(self, symbol: str) -> SymbolRule:
        rules = self.cfg.symbol_rules or {}
        r = rules.get(symbol)
        if isinstance(r, dict):  # 兼容 dict 配置
            return SymbolRule(**r)
        return r or SymbolRule()

    def _estimate_open_risk_after(self, symbol: str, side: str, size: float, px: float, stop_pct: float) -> float:
        # 简化：新增头寸的风险 ≈ size * px * stop_pct
        return size * px * stop_pct
