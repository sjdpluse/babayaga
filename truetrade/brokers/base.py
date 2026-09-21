"""Broker-neutral trading contracts. No vendor imports."""
from dataclasses import dataclass
from decimal import Decimal
import math
import re
import time
from typing import Protocol, runtime_checkable
from truetrade.risk.manager import decimal as D, RiskRejected


class BrokerError(RuntimeError):
    """Sanitized failure; never include secrets or raw vendor responses."""

    def __init__(self, message, *, diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic or {}


class OrderRejected(BrokerError):
    """Known unsent request or definite broker rejection without execution."""


class OrderNotSubmitted(OrderRejected):
    """The adapter proves order_send was never attempted. Do not replay the intent."""


class OrderUncertain(BrokerError):
    def __init__(self, message="Execution outcome uncertain", position_id=None, receipt=None, *, diagnostic=None):
        super().__init__(message, diagnostic=diagnostic)
        self.position_id = position_id
        self.receipt = receipt or {}


@dataclass(frozen=True)
class Signal:
    decision_id: str
    symbol: str
    side: str
    stop: Decimal
    take_profit: Decimal
    risk_fraction: Decimal
    created_at: float
    expires_at: float
    require_flat: bool = False
    expected_mode: str | None = None
    expected_identity: str | None = None
    expected_state_id: str | None = None
    model_sha256: str | None = None

    def __post_init__(self):
        if self.model_sha256 is not None and not re.fullmatch(r"[a-f0-9]{64}",self.model_sha256):
            raise ValueError("Invalid model hash")
        if type(self.require_flat) is not bool or self.expected_mode not in {None, "paper", "demo", "live"}:
            raise ValueError("Invalid execution constraints")
        for value in (self.expected_identity, self.expected_state_id):
            if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 100):
                raise ValueError("Invalid execution identity")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", self.decision_id):
            raise ValueError("Invalid decision ID")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", self.symbol):
            raise ValueError("Invalid symbol")
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("Invalid side")
        for name in ("stop", "take_profit", "risk_fraction"):
            object.__setattr__(self, name, D(getattr(self, name)))
        if min(self.stop, self.take_profit) <= 0 or not 0 < self.risk_fraction <= D(".05"):
            raise ValueError("Invalid protection or risk fraction")
        if not all(math.isfinite(x) for x in (self.created_at, self.expires_at)):
            raise ValueError("Invalid signal time")
        if not 0 < self.expires_at - self.created_at <= 300:
            raise ValueError("Signal lifetime must be at most 300 seconds")

    def validate_time(self):
        now = time.time()
        if self.created_at > now + 2 or self.expires_at <= now:
            raise OrderRejected("Signal expired or future-dated")


@dataclass(frozen=True)
class Symbol:
    symbol: str
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    trade_tick_size: Decimal
    trade_tick_value: Decimal
    trade_contract_size: Decimal
    point: Decimal
    digits: int
    trade_stops_level: int
    trade_freeze_level: int = 0

    def __post_init__(self):
        for key in ("volume_min", "volume_max", "volume_step", "trade_tick_size",
                    "trade_tick_value", "trade_contract_size", "point"):
            object.__setattr__(self, key, D(getattr(self, key)))
            if getattr(self, key) <= 0:
                raise RiskRejected("Invalid symbol metadata: " + key)
        if self.volume_max < self.volume_min or self.volume_min % self.volume_step:
            raise RiskRejected("Invalid volume grid")
        if not 0 <= self.digits <= 12 or min(self.trade_stops_level, self.trade_freeze_level) < 0:
            raise RiskRejected("Invalid precision/stops")
        if self.trade_tick_size % (D(10) ** -self.digits):
            raise RiskRejected("Tick size incompatible with digits")


@dataclass(frozen=True)
class Quote:
    bid: Decimal
    ask: Decimal
    timestamp: float

    def __post_init__(self):
        object.__setattr__(self, "bid", D(self.bid))
        object.__setattr__(self, "ask", D(self.ask))
        if not 0 < self.bid <= self.ask or not math.isfinite(self.timestamp):
            raise OrderRejected("Invalid Bid/Ask")

    def validate(self, max_age=5):
        age = time.time() - self.timestamp
        if age > max_age or age < -2:
            raise OrderRejected("Stale or future tick")


@dataclass(frozen=True)
class CFDPlan:
    symbol: str
    side: str
    entry: Decimal
    stop: Decimal
    take_profit: Decimal
    size: Decimal
    risk: Decimal
    margin: Decimal
    budget: Decimal
    timestamp: float
    decision_id: str
    risk_fraction: Decimal
    expires_at: float
    equity: Decimal = Decimal(0)


@runtime_checkable
class Broker(Protocol):
    """Gate every write, never retry uncertain writes; failed reads are not empty."""
    identity: str
    async def assert_execution_allowed(self): ...
    async def account(self): ...
    async def open(self, plan) -> dict: ...
    async def set_protection(self, position_id, stop, target): ...
    async def position(self, position_id) -> dict | None: ...
    async def close(self, position_id): ...
    async def verify(self, plan, observed): ...
    async def confirm_closed(self, position_id) -> bool: ...
    async def health(self) -> dict: ...


@runtime_checkable
class MarketBroker(Broker, Protocol):
    async def resolve_symbol(self, name: str) -> str: ...
    async def symbol_info(self, name: str) -> Symbol: ...
    async def quote(self, name: str) -> Quote: ...
    async def candles(self, name: str, timeframe: str, count: int) -> list[dict]: ...
    async def open_positions(self) -> list[dict]: ...
    async def prepare(self, signal: Signal, limits) -> CFDPlan: ...
