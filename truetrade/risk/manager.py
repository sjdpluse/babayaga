from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import time
import math
from truetrade.config import RiskLimits

D = Decimal


def decimal(value):
    result = D(str(value))
    if not result.is_finite():
        raise ValueError("Non-finite financial input")
    return result


def quantize(value, step, rounding=ROUND_FLOOR):
    if step <= 0:
        raise ValueError("Step must be positive")
    return (value / step).to_integral_value(rounding=rounding) * step


@dataclass(frozen=True)
class Market:
    symbol: str
    tick: D
    size_step: D
    min_size: D
    min_notional: D
    max_leverage: int
    fee_rate: D
    slippage_rate: D
    maintenance_rate: D

    def __post_init__(self):
        if not self.symbol or not 20 <= self.max_leverage:
            raise ValueError("Market does not support minimum 20x leverage")
        for k in ("tick", "size_step", "min_size", "min_notional", "fee_rate", "slippage_rate", "maintenance_rate"):
            v = getattr(self, k)
            if not isinstance(v, D) or not v.is_finite() or v < 0:
                raise ValueError(f"Invalid market {k}")
        if min(self.tick, self.size_step, self.min_size) <= 0:
            raise ValueError("Precision and minimum size must be positive")


@dataclass(frozen=True)
class Account:
    equity: D
    available_margin: D
    used_margin: D
    open_risk: D
    peak_equity: D
    timestamp: float
    all_protected: bool = True
    pending_uncertain: bool = False


@dataclass(frozen=True)
class Plan:
    symbol: str
    side: str
    entry: D
    stop: D
    take_profit: D
    size: D
    leverage: int
    risk: D
    margin: D
    budget: D
    explanation: str


class RiskRejected(RuntimeError):
    pass


class RiskManager:
    def __init__(self, limits=None):
        self.limits = limits or RiskLimits()

    def size(self, market: Market, account: Account, side, entry, atr, confidence,
             risk_tier=1., leverage=20, now=None):
        v = self.limits
        now = time.time() if now is None else now
        for x in (account.equity, account.available_margin, account.used_margin, account.open_risk, account.peak_equity):
            if not x.is_finite() or x < 0:
                raise RiskRejected("invalid_account")
        if not math.isfinite(account.timestamp) or not math.isfinite(now) or now - account.timestamp > v.max_snapshot_age or account.timestamp - now > 2:
            raise RiskRejected("stale_account")
        if account.equity <= 0 or not account.all_protected or account.pending_uncertain:
            raise RiskRejected("account_not_safe")
        if v.circuit_enabled and account.peak_equity > 0 and 1 - account.equity / account.peak_equity >= v.circuit_drawdown:
            raise RiskRejected("drawdown_circuit")
        if side not in {"LONG", "SHORT"} or not 20 <= leverage <= min(25, market.max_leverage):
            raise RiskRejected("invalid_side_or_leverage")
        entry, atr, confidence, risk_tier = map(decimal, (entry, atr, confidence, risk_tier))
        if entry <= 0 or atr <= 0 or not 0 < confidence <= 1 or not 0 < risk_tier <= 1:
            raise RiskRejected("invalid_model_input")
        sign = 1 if side == "LONG" else -1
        # Stop is rounded OUTWARD, then risk recomputed on the actual rounded price.
        stop = quantize(entry - sign * atr * 2, market.tick,
                        ROUND_FLOOR if sign == 1 else ROUND_CEILING)
        target = quantize(entry + sign * atr * 3, market.tick,
                          ROUND_FLOOR if sign == 1 else ROUND_CEILING)
        if min(stop, target) <= 0 or sign * (entry - stop) <= 0 or sign * (target - entry) <= 0:
            raise RiskRejected("invalid_protection")
        cost_per_unit = (entry + stop) * (market.fee_rate + market.slippage_rate)
        loss_per_unit = abs(entry - stop) + cost_per_unit
        # Conservative linear isolated-margin proxy; exact exchange liquidation must be verified.
        liquidation_buffer = entry * (D(1) / leverage - market.maintenance_rate)
        if loss_per_unit >= liquidation_buffer * D("0.8"):
            raise RiskRejected("stop_too_close_to_liquidation")
        budget = min(account.equity * v.max_trade_risk * risk_tier * confidence,
                     account.equity * v.max_portfolio_risk - account.open_risk)
        margin_budget = min(account.available_margin,
                            account.equity * v.margin_utilization - account.used_margin)
        if budget <= 0 or margin_budget <= 0:
            raise RiskRejected("portfolio_budget_exhausted")
        size = quantize(min(budget / loss_per_unit,
                       margin_budget / (entry / leverage + entry * market.fee_rate)), market.size_step)
        if size < market.min_size or size * entry < market.min_notional:
            raise RiskRejected("below_market_minimum")
        risk = size * loss_per_unit
        margin = size * entry / leverage
        if risk > budget or risk + account.open_risk > account.equity * v.max_portfolio_risk:
            raise RiskRejected("post_rounding_limit")
        return Plan(market.symbol, side, entry, stop, target, size, leverage, risk, margin, budget,
                    "Stop distance + estimated entry/exit fees and adverse slippage; capped by aggregate risk and available margin. "
                    "Confidence is policy probability, not calibrated win probability.")

    def validate_tightening(self, side, old_stop, new_stop, mark, target):
        old_stop, new_stop, mark, target = map(decimal, (old_stop, new_stop, mark, target))
        if side == "LONG" and not old_stop <= new_stop < mark < target:
            raise RiskRejected("protection_would_widen_or_cross")
        if side == "SHORT" and not target < mark < new_stop <= old_stop:
            raise RiskRejected("protection_would_widen_or_cross")
        if side not in {"LONG", "SHORT"}:
            raise RiskRejected("invalid_side")
