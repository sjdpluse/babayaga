"""Worker configuration, shared ATR protection and an optional demo-only baseline.

A closed bar breaking the prior 20-bar range in the EMA8/21 direction enters
once, with 2 ATR stop distance and 2:1 target/risk distance. No profit claim.
"""
from dataclasses import dataclass
import os
import re
from decimal import Decimal
from truetrade.features.technical import Candles, compute, WARMUP
from truetrade.brokers.base import Quote, Signal

TIMEFRAMES = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}


@dataclass(frozen=True)
class WorkerSettings:
    mode: str = "paper"
    strategy: str = "ppo_cfd"
    symbol: str = "XAUUSD"
    timeframe: str = "M5"
    bars: int = 256
    poll_seconds: int = 10
    max_bar_age: int = 90
    risk: Decimal = Decimal(".005")
    allow_live: bool = False

    def __post_init__(self):
        if self.mode not in {"paper", "demo", "live"}:
            raise ValueError("Invalid execution mode")
        if self.strategy not in {"breakout_demo","ppo_cfd"}:
            raise ValueError("Unknown strategy")
        if self.mode=="live" and (not self.allow_live or self.strategy!="ppo_cfd"):
            raise ValueError("Live requires explicit authorization and a qualified CFD PPO model")
        if self.strategy=="ppo_cfd" and self.bars!=256:
            raise ValueError("CFD policy requires the fixed training window of 256")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", self.symbol):
            raise ValueError("Invalid worker symbol")
        if self.timeframe not in TIMEFRAMES or not 100 <= self.bars <= 2000:
            raise ValueError("Invalid timeframe/history window")
        if not 5 <= self.poll_seconds <= 60 or not 5 <= self.max_bar_age <= 300:
            raise ValueError("Invalid polling/freshness limits")
        if not self.risk.is_finite() or not 0 < self.risk <= Decimal(".005"):
            raise ValueError("Worker risk must be positive and at most 0.5 percent")

    @classmethod
    def from_env(cls):
        return cls(mode=os.getenv("MT5_MODE", "paper"), strategy=os.getenv("MT5_STRATEGY", "ppo_cfd"),
                   symbol=os.getenv("MT5_SYMBOL", "XAUUSD"), timeframe=os.getenv("MT5_TIMEFRAME", "M5"),
                   bars=int(os.getenv("MT5_HISTORY_BARS", "256")),
                   poll_seconds=int(os.getenv("MT5_POLL_SECONDS", "10")),
                   max_bar_age=int(os.getenv("MT5_MAX_BAR_AGE_SECONDS", "90")),
                   risk=Decimal(os.getenv("MT5_SIGNAL_RISK", ".005")),
                   allow_live=os.getenv("ALLOW_LIVE_TRADING","false").lower()=="true")


def closed_candles(rows, settings, now):
    if not isinstance(rows, list) or len(rows) != settings.bars:
        raise ValueError("Incomplete candle history")
    c = Candles(**{field: [r["time" if field == "timestamp" else field] for r in rows]
                   for field in Candles.__dataclass_fields__})
    seconds = TIMEFRAMES[settings.timeframe]
    if any(t != int(t) or t % seconds for t in c.timestamp):
        raise ValueError("Unaligned candle timestamps")
    if c.timestamp[-1] + seconds > now:
        raise ValueError("Incomplete latest candle")
    if len(c.close) < WARMUP + 21:
        raise ValueError("Insufficient feature warmup")
    return c


def choose(candles):
    features, atr = compute(candles)
    x = features[-1]
    side = "LONG" if x[16] and x[5] > 0 else "SHORT" if x[17] and x[5] < 0 else None
    return side, Decimal(str(atr[-1]))


def make_signal(decision_id, side, atr, market, settings, now):
    quote = Quote(**market["quote"])
    quote.validate()
    entry = quote.ask if side == "LONG" else quote.bid
    if atr <= 0:
        raise ValueError("ATR must be positive")
    direction = 1 if side == "LONG" else -1
    distance = atr * 2
    return Signal(decision_id, settings.symbol, side, entry-direction*distance,
                  entry+direction*distance*2, settings.risk, now, now+60,
                  require_flat=True, expected_mode=settings.mode)
