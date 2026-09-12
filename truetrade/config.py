"""No credentials in repr, logs, defaults or repository files."""
from dataclasses import dataclass, field
from decimal import Decimal
import os
import math


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)
    supabase_url: str = ""
    supabase_key: str = field(default="", repr=False)
    mode: str = "research"
    request_interval: float = 1.0
    timeout: float = 15.0
    max_retries: int = 3
    state_dir: str = "data"

    def __post_init__(self):
        if self.mode not in {"research", "collect", "demo"}:
            raise ValueError("Only research, collect and demo modes exist; live is unsupported")
        if not math.isfinite(self.request_interval) or not math.isfinite(self.timeout) or self.request_interval < .25 or self.timeout <= 0 or not 0 <= self.max_retries <= 8:
            raise ValueError("Invalid network limits")

    @classmethod
    def from_env(cls):
        return cls(api_key=os.getenv("TRUETRADE_API_KEY", ""),
                   api_secret=os.getenv("TRUETRADE_API_SECRET", ""),
                   supabase_url=os.getenv("SUPABASE_URL", ""),
                   supabase_key=os.getenv("SUPABASE_SERVICE_KEY", ""),
                   mode=os.getenv("BOT_MODE", "research"),
                   request_interval=float(os.getenv("REQUEST_INTERVAL_SECONDS", "1")),
                   timeout=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "15")),
                   state_dir=os.getenv("STATE_DIR", "data"))


@dataclass(frozen=True)
class RiskLimits:
    max_trade_risk: Decimal = Decimal("0.05")
    max_portfolio_risk: Decimal = Decimal("0.10")
    margin_utilization: Decimal = Decimal("0.50")
    circuit_drawdown: Decimal = Decimal("0.15")
    circuit_enabled: bool = True
    max_snapshot_age: float = 10.0

    def __post_init__(self):
        for name in ("max_trade_risk", "max_portfolio_risk", "margin_utilization", "circuit_drawdown"):
            v = getattr(self, name)
            if not v.is_finite() or not 0 < v <= 1:
                raise ValueError(f"Invalid {name}")
        if self.max_trade_risk > Decimal("0.05"):
            raise ValueError("Per-trade risk cannot exceed the requested 5% ceiling")
        if self.max_snapshot_age <= 0:
            raise ValueError("Invalid snapshot age")

    @classmethod
    def from_env(cls):
        return cls(max_trade_risk=Decimal(os.getenv("MAX_TRADE_RISK", "0.05")),
                   max_portfolio_risk=Decimal(os.getenv("MAX_PORTFOLIO_RISK", "0.10")),
                   margin_utilization=Decimal(os.getenv("MAX_MARGIN_UTILIZATION", "0.50")),
                   circuit_drawdown=Decimal(os.getenv("CIRCUIT_DRAWDOWN", "0.15")),
                   circuit_enabled=os.getenv("CIRCUIT_ENABLED", "true").lower() != "false")
