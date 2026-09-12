"""Bar-based linear perpetual simulator; not an exchange matching-engine replica."""
from dataclasses import dataclass
import numpy as np
from truetrade.features.technical import FEATURE_NAMES, WARMUP, compute, Normalizer
from truetrade.risk.manager import RiskManager, Account, Plan, RiskRejected, decimal

ACCOUNT_FEATURES = ("equity_return", "margin_fraction", "portfolio_risk_fraction", "net_exposure",
                    "gross_exposure", "unrealized_fraction", "drawdown")
# Parameterized categorical actions, each is a learned choice, not a TA rule.
ACTIONS = [("hold", 0., 20), ("close", 0., 20), ("tighten", 0., 20)] + [
    (side, tier, leverage) for side in ("LONG", "SHORT") for tier in (.25, .5, 1.) for leverage in (20, 25)]


@dataclass
class Position:
    plan: Plan
    entry_fee: float
    funding_paid: float = 0.

    @property
    def sign(self): return 1 if self.plan.side == "LONG" else -1


class TradingEnv:
    def __init__(self, candles, market, funding_rates, normalizer=None, risk=None,
                 initial_equity=10000., episode_length=256, random_start=True):
        if len(candles.close) < WARMUP + 3 or initial_equity <= 0 or episode_length < 2:
            raise ValueError("Insufficient data or invalid episode settings")
        self.candles, self.market = candles, market
        self.features, self.atr = compute(candles)
        self.funding = np.asarray(funding_rates, dtype=float)
        if self.funding.shape != candles.close.shape or not np.isfinite(self.funding).all():
            raise ValueError("Explicit per-bar funding settlement rates required")
        self.normalizer = normalizer or Normalizer.fit(self.features[WARMUP:])
        self.risk = risk or RiskManager()
        self.initial_equity, self.episode_length, self.random_start = initial_equity, episode_length, random_start
        self.rng = np.random.default_rng(0)
        self.n_features = len(FEATURE_NAMES) + len(ACCOUNT_FEATURES)
        self.reset()

    def reset(self, seed=None, options=None):
        if seed is not None:
            self.rng = np.random.default_rng(seed)
        max_start = max(WARMUP, len(self.candles.close) - self.episode_length - 1)
        self.i = int(self.rng.integers(WARMUP, max_start + 1)) if self.random_start else WARMUP
        self.end = min(self.i + self.episode_length, len(self.candles.close) - 1)
        self.balance = self.peak = self.initial_equity
        self.positions, self.closed, self.equity_curve = [], [], [self.initial_equity]
        self.done = False
        return self.observation(), {}

    def equity(self, mark):
        return self.balance + sum(p.sign * float(p.plan.size) * (mark - float(p.plan.entry)) for p in self.positions)

    def exposure(self, mark):
        used = sum(float(p.plan.margin) for p in self.positions)
        # Estimate risk from current marked equity to stop, including exit costs.
        risk = sum(max(0., p.sign * (mark - float(p.plan.stop))) * float(p.plan.size)
                   + float(p.plan.size) * (mark + float(p.plan.stop)) * float(self.market.fee_rate + self.market.slippage_rate)
                   for p in self.positions)
        gross = sum(float(p.plan.size) * mark for p in self.positions)
        net = sum(p.sign * float(p.plan.size) * mark for p in self.positions)
        return used, risk, gross, net

    def observation(self):
        mark = self.candles.close[self.i]
        eq = self.equity(mark)
        used, risk, gross, net = self.exposure(mark)
        denom = max(eq, 1.)
        account = [eq / self.initial_equity - 1, used / denom, risk / denom, net / denom,
                   gross / denom, (eq - self.balance) / denom, 1 - eq / max(self.peak, 1.)]
        return np.r_[self.normalizer.transform(self.features[self.i]), np.clip(account, -25, 25)].astype(np.float32)

    def _close(self, p, mark, reason):
        exit_price = mark * (1 - p.sign * float(self.market.slippage_rate))
        gross = p.sign * float(p.plan.size) * (exit_price - float(p.plan.entry))
        exit_fee = abs(exit_price * float(p.plan.size)) * float(self.market.fee_rate)
        self.balance += gross - exit_fee
        net = gross - exit_fee - p.entry_fee - p.funding_paid
        self.closed.append({"entry": float(p.plan.entry), "exit": exit_price, "side": p.plan.side,
                            "quantity": float(p.plan.size), "net_pnl": net, "reason": reason,
                            "funding": p.funding_paid, "timestamp": float(self.candles.timestamp[self.i])})
        self.positions.remove(p)

    def step(self, action, confidence=1.):
        if self.done:
            raise RuntimeError("reset is required after termination")
        if not isinstance(action, (int, np.integer)) or not 0 <= action < len(ACTIONS):
            raise ValueError("Unknown action")
        previous_equity = self.equity(self.candles.close[self.i])
        # The actor has only bar i; execution uses bar i+1.
        previous_atr = self.atr[self.i]
        self.i += 1
        o, h, low, close = (getattr(self.candles, k)[self.i] for k in ("open", "high", "low", "close"))
        # Overnight/bar-open gaps hit resting protection before a new decision executes.
        for p in list(self.positions):
            stop, target = float(p.plan.stop), float(p.plan.take_profit)
            if p.sign * (o - stop) <= 0:
                self._close(p, o, "gap_stop")
            elif p.sign * (o - target) >= 0:
                self._close(p, target, "gap_target_conservative")
        name, tier, leverage = ACTIONS[int(action)]
        info = {"action": name, "risk_result": "not_applicable"}
        if name == "close":
            for p in list(self.positions): self._close(p, o, "policy_close")
        elif name == "tighten":
            from dataclasses import replace
            from truetrade.risk.manager import quantize
            for p in self.positions:
                old = float(p.plan.stop)
                new = max(old, o - previous_atr) if p.sign == 1 else min(old, o + previous_atr)
                new = quantize(decimal(new), self.market.tick)
                try:
                    self.risk.validate_tightening(p.plan.side, p.plan.stop, new, o, p.plan.take_profit)
                    p.plan = replace(p.plan, stop=new)
                except RiskRejected:
                    info["risk_result"] = "tightening_rejected"
        elif name in {"LONG", "SHORT"}:
            entry = o * (1 + (1 if name == "LONG" else -1) * float(self.market.slippage_rate))
            eq = max(0., self.equity(o))
            used, open_risk, _, _ = self.exposure(o)
            account = Account(decimal(eq), decimal(max(0, eq-used)), decimal(used), decimal(open_risk),
                              decimal(self.peak), float(self.candles.timestamp[self.i]))
            try:
                plan = self.risk.size(self.market, account, name, entry, previous_atr, confidence,
                                      tier, leverage, now=account.timestamp)
                fee = float(plan.size * plan.entry * self.market.fee_rate)
                self.balance -= fee
                self.positions.append(Position(plan, fee))
                info.update(risk_result="accepted", size=float(plan.size), risk=float(plan.risk),
                            stop=float(plan.stop), take_profit=float(plan.take_profit), leverage=plan.leverage)
            except RiskRejected as e:
                info["risk_result"] = str(e)
        for p in list(self.positions):
            stop, target = float(p.plan.stop), float(p.plan.take_profit)
            # Both touched: stop wins. Intrabar liquidation model is conservative approximation.
            liquidation = float(p.plan.entry) * (1 - p.sign * (1 / p.plan.leverage - float(self.market.maintenance_rate)))
            stop_hit = low <= stop if p.sign == 1 else h >= stop
            target_hit = h >= target if p.sign == 1 else low <= target
            liq_hit = low <= liquidation if p.sign == 1 else h >= liquidation
            if stop_hit:
                self._close(p, min(o, stop) if p.sign == 1 else max(o, stop), "stop")
            elif liq_hit:
                self._close(p, liquidation, "liquidation_proxy")
            elif target_hit:
                self._close(p, target, "target")
            else:
                # Data convention: rate is a settlement event at candle close, not a forward-filled rate.
                funding = p.sign * float(p.plan.size) * close * self.funding[self.i]
                p.funding_paid += funding
                self.balance -= funding
        equity = self.equity(close)
        terminated = equity <= self.initial_equity * .05
        truncated = self.i >= self.end
        if terminated or truncated:
            for p in list(self.positions): self._close(p, close, "episode_end")
            equity = self.equity(close)
            self.done = True
        self.peak = max(self.peak, equity)
        self.equity_curve.append(equity)
        ret = (equity - previous_equity) / max(previous_equity, 1.)
        # Mark-to-market net return prevents hiding losses until after an episode.
        # Penalize downside and increases in drawdown. No annualized Sharpe claim.
        old_dd = 1 - previous_equity / max(self.peak, 1.)
        dd = 1 - equity / max(self.peak, 1.)
        reward = 100 * (ret - 2 * min(ret, 0.) ** 2 - .1 * max(0, dd-old_dd))
        info.update(equity=equity, drawdown=dd, realized_trades=len(self.closed))
        return self.observation(), float(reward), terminated, truncated, info


def gymnasium_env(*args, **kwargs):
    """Actual Gymnasium Env when optional dependency is installed."""
    import gymnasium as gym
    from gymnasium import spaces

    class Adapter(gym.Env):
        metadata = {"render_modes": []}
        def __init__(self):
            self.core = TradingEnv(*args, **kwargs)
            self.action_space = spaces.Discrete(len(ACTIONS))
            self.observation_space = spaces.Box(-25, 25, (self.core.n_features,), dtype=np.float32)
        def reset(self, *, seed=None, options=None):
            super().reset(seed=seed)
            return self.core.reset(seed, options)
        def step(self, action): return self.core.step(action)
    return Adapter()
