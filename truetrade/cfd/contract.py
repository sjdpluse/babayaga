"""Reviewed account-currency research assumptions, never inferred historical fees."""
from dataclasses import dataclass, asdict
from decimal import Decimal
import math
from truetrade.brokers.base import Symbol


@dataclass(frozen=True)
class Contract:
    symbol: dict
    currency: str
    value_per_price_lot: float
    margin_rate: float
    commission: float
    entry_slippage_points: float
    exit_slippage_points: float
    max_spread_points: float
    swap_long_cost: float
    swap_short_cost: float
    rollover_utc_hour: int
    triple_weekday: int
    source: str
    observed_at: float

    def __post_init__(self):
        Symbol(**self.symbol)
        if self.currency != 'USD' or not self.symbol['symbol'].startswith('XAUUSD'):
            raise ValueError('Initial research supports XAUUSD in USD accounts only')
        for key in ('value_per_price_lot','margin_rate','commission','entry_slippage_points',
                    'exit_slippage_points','max_spread_points','swap_long_cost','swap_short_cost','observed_at'):
            if not math.isfinite(getattr(self,key)) or getattr(self,key)<0:
                raise ValueError('Invalid research contract')
        if min(self.value_per_price_lot,self.margin_rate,self.max_spread_points,self.observed_at)<=0:
            raise ValueError('Missing contract economics')
        if not 0 <= self.rollover_utc_hour <= 23 or not 0 <= self.triple_weekday <= 4:
            raise ValueError('Reviewed UTC rollover calendar required')
        if self.source not in {'mt5_demo','mt5_live','synthetic_test'}:
            raise ValueError('Unknown data source')

    def json(self):
        return asdict(self)

    def compatible(self, current):
        # Same contract units; actual current execution costs must fit the trained envelope.
        old,new=Symbol(**self.symbol),Symbol(**current.symbol)
        keys=('trade_tick_size','trade_contract_size','point','volume_step','volume_min','digits')
        if self.currency != current.currency or any(getattr(old,k)!=getattr(new,k) for k in keys):
            return False
        return (math.isclose(self.value_per_price_lot,current.value_per_price_lot,rel_tol=1e-6)
                and all(getattr(current,k)<=getattr(self,k) for k in
                        ('commission','entry_slippage_points','exit_slippage_points','margin_rate',
                         'swap_long_cost','swap_short_cost','max_spread_points'))
                and current.rollover_utc_hour==self.rollover_utc_hour and current.triple_weekday==self.triple_weekday
                and new.trade_stops_level<=old.trade_stops_level)
