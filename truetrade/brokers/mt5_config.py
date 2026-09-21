"""Environment-only terminal identity and fail-closed execution settings."""
from dataclasses import dataclass, field
from decimal import Decimal
import json
import os
from truetrade.risk.manager import decimal as D


@dataclass(frozen=True)
class MT5Settings:
    login: int = 0
    password: str = field(default="", repr=False)
    server: str = ""
    terminal_path: str = ""
    mode: str = "paper"
    allow_live: bool = False
    max_spread_points: Decimal = Decimal("50")
    deviation_points: int = 20
    exit_slippage_points: int = 20
    commission_per_lot: Decimal | None = None
    magic: int = 730021
    symbol_map: dict = field(default_factory=dict)
    server_utc_offset_seconds: int = 0

    def __post_init__(self):
        if self.mode not in {"paper", "demo", "live"}:
            raise ValueError("MT5_MODE must be paper, demo or live")
        if type(self.allow_live) is not bool:
            raise ValueError("allow_live must be boolean")
        if self.login < 0 or not 1 <= self.magic <= 2147483647:
            raise ValueError("Invalid terminal identity")
        if type(self.server_utc_offset_seconds) is not int or not -50400 <= self.server_utc_offset_seconds <= 50400:
            raise ValueError("Invalid MT5 server UTC offset")
        object.__setattr__(self, "max_spread_points", D(self.max_spread_points))
        if self.commission_per_lot is not None:
            object.__setattr__(self, "commission_per_lot", D(self.commission_per_lot))
        if self.max_spread_points <= 0 or min(self.deviation_points, self.exit_slippage_points) < 0:
            raise ValueError("Invalid execution limits")
        if self.commission_per_lot is not None and self.commission_per_lot < 0:
            raise ValueError("Invalid commission")
        if not isinstance(self.symbol_map, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in self.symbol_map.items()):
            raise ValueError("Invalid symbol mapping")

    @classmethod
    def from_env(cls):
        fee = os.getenv("MT5_COMMISSION_PER_LOT")
        return cls(login=int(os.getenv("MT5_LOGIN", "0")), password=os.getenv("MT5_PASSWORD", ""),
                   server=os.getenv("MT5_SERVER", ""), terminal_path=os.getenv("MT5_TERMINAL_PATH", ""),
                   mode=os.getenv("MT5_MODE", "paper"),
                   allow_live=os.getenv("ALLOW_LIVE_TRADING", "false").lower() == "true",
                   max_spread_points=D(os.getenv("MT5_MAX_SPREAD_POINTS", "50")),
                   deviation_points=int(os.getenv("MT5_DEVIATION_POINTS", "20")),
                   exit_slippage_points=int(os.getenv("MT5_EXIT_SLIPPAGE_POINTS", "20")),
                   commission_per_lot=None if fee is None else D(fee),
                   magic=int(os.getenv("MT5_MAGIC", "730021")),
                   symbol_map=json.loads(os.getenv("MT5_SYMBOL_MAP", "{}")),
                   server_utc_offset_seconds=int(os.getenv("MT5_SERVER_UTC_OFFSET_SECONDS", "0")))
