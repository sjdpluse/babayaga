"""Read-only terminal data plus the existing paper executor; never order_send."""
import time
from truetrade.brokers.paper import PaperBroker
from truetrade.risk.manager import Account, decimal as D
from truetrade.risk.cfd import size_signal


class MT5PaperBroker(PaperBroker):
    def __init__(self, market_data, equity=D("10000")):
        super().__init__(Account(equity, equity, D(0), D(0), equity, time.time()))
        self.market_data = market_data
        self.identity = market_data.identity

    async def prepare(self, signal, limits):
        source = self.market_data
        info = await source.symbol_info(signal.symbol)
        return size_signal(signal, info, await source.quote(info.symbol), await self.account(), limits,
                           source.loss, source.margin, source.settings.max_spread_points,
                           source.settings.deviation_points, source.settings.exit_slippage_points,
                           source.settings.commission_per_lot)

    async def health(self):
        return {**await self.market_data.health(), "execution_allowed": True, "mode": "paper"}

    async def resolve_symbol(self, name):
        return await self.market_data.resolve_symbol(name)

    async def symbol_info(self, name):
        return await self.market_data.symbol_info(name)

    async def quote(self, name):
        return await self.market_data.quote(name)

    async def candles(self, name, timeframe="M1", count=200, start=1):
        return await self.market_data.candles(name, timeframe, count, start)

    async def open_positions(self):
        return list(self.positions.values())

    async def research_contract(self, name):
        return await self.market_data.research_contract(name)
