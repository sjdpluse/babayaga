"""Forex/CFD monetary sizing; never uses crypto leverage/liquidation formulas."""
import math
import time
from decimal import ROUND_FLOOR, ROUND_CEILING
from truetrade.brokers.base import CFDPlan, OrderRejected
from truetrade.risk.manager import decimal as D, quantize, RiskRejected


def normalize_volume(requested, symbol):
    volume = quantize(min(D(requested), symbol.volume_max), symbol.volume_step)
    if volume < symbol.volume_min:
        raise RiskRejected("Risk budget below minimum lot")
    return volume


def validate_volume(volume, symbol):
    volume = D(volume)
    if not symbol.volume_min <= volume <= symbol.volume_max or volume % symbol.volume_step:
        raise OrderRejected("Invalid lot volume")


def protection(symbol, quote, side, stop, target, *, modify=False):
    if side not in {"LONG", "SHORT"}:
        raise OrderRejected("Invalid side")
    stop = quantize(D(stop), symbol.trade_tick_size, ROUND_FLOOR if side == "LONG" else ROUND_CEILING)
    target = quantize(D(target), symbol.trade_tick_size, ROUND_FLOOR if side == "LONG" else ROUND_CEILING)
    level = max(symbol.trade_stops_level, symbol.trade_freeze_level if modify else 0)
    distance = max(symbol.trade_tick_size, D(level) * symbol.point)
    price = quote.bid if side == "LONG" else quote.ask
    if min(stop, target) <= 0:
        raise OrderRejected("Invalid SL/TP")
    if side == "LONG" and not (stop <= price-distance and target >= price+distance):
        raise OrderRejected("SL/TP violates stop distance")
    if side == "SHORT" and not (stop >= price+distance and target <= price-distance):
        raise OrderRejected("SL/TP violates stop distance")
    return stop, target


def validate_account(account, limits):
    for value in (account.equity, account.available_margin, account.used_margin,
                  account.open_risk, account.peak_equity):
        if not value.is_finite() or value < 0:
            raise RiskRejected("Invalid account")
    age = time.time() - account.timestamp
    if not math.isfinite(age) or age > limits.max_snapshot_age or age < -2:
        raise RiskRejected("Stale account")
    if account.equity <= 0 or not account.all_protected or account.pending_uncertain:
        raise RiskRejected("Account not safe")
    if limits.circuit_enabled and account.peak_equity > 0:
        if 1-account.equity/account.peak_equity >= limits.circuit_drawdown:
            raise RiskRejected("Drawdown circuit")


def size_signal(signal, symbol, quote, account, limits, loss, margin,
                max_spread_points, deviation_points, exit_slippage_points, commission_per_lot):
    """Callbacks return account-currency amounts. MT5 uses -order_calc_profit.

    Use a valid reference volume, round down, and re-evaluate actual volume.
    Commission must be explicitly reviewed; zero is valid only when verified.
    """
    signal.validate_time()
    quote.validate()
    validate_account(account, limits)
    if commission_per_lot is None:
        raise RiskRejected("Explicit round-trip commission estimate required")
    fee = D(commission_per_lot)
    if fee < 0 or D(max_spread_points) <= 0 or min(deviation_points, exit_slippage_points) < 0:
        raise RiskRejected("Invalid execution costs")
    if (quote.ask-quote.bid)/symbol.point > D(max_spread_points):
        raise RiskRejected("Spread limit exceeded")
    stop, target = protection(symbol, quote, signal.side, signal.stop, signal.take_profit)
    entry = quote.ask if signal.side == "LONG" else quote.bid
    sign = 1 if signal.side == "LONG" else -1
    worst_entry = entry + sign*D(deviation_points)*symbol.point
    worst_stop = stop - sign*D(exit_slippage_points)*symbol.point
    if min(worst_entry, worst_stop) <= 0:
        raise RiskRejected("Invalid adverse execution prices")
    reference = symbol.volume_min
    unit_loss = D(loss(signal.side, symbol.symbol, reference, worst_entry, worst_stop))/reference + fee
    if unit_loss <= 0:
        raise RiskRejected("Missing/nonpositive monetary stop loss")
    budget = min(account.equity*min(signal.risk_fraction, limits.max_trade_risk),
                 account.equity*limits.max_portfolio_risk-account.open_risk)
    if budget <= 0:
        raise RiskRejected("Portfolio risk budget exhausted")
    size = normalize_volume(budget/unit_loss, symbol)
    risk = D(loss(signal.side, symbol.symbol, size, worst_entry, worst_stop)) + size*fee
    required_margin = D(margin(signal.side, symbol.symbol, size, worst_entry))
    margin_budget = min(account.available_margin, account.equity*limits.margin_utilization-account.used_margin)
    if required_margin <= 0 or required_margin + size*fee > margin_budget:
        raise RiskRejected("Insufficient margin")
    if risk <= 0 or risk > budget:
        raise RiskRejected("Post-rounding risk limit")
    return CFDPlan(symbol.symbol, signal.side, entry, stop, target, size, risk,
                   required_margin, budget, time.time(), signal.decision_id,
                   signal.risk_fraction, signal.expires_at, account.equity)
