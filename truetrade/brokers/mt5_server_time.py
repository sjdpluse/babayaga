"""MT5 adapter for terminals that expose broker-server epoch timestamps.

Some broker terminals return tick, bar and deal epochs shifted by the server UTC
offset even though the Python integration normally treats them as UTC.  The offset
is explicit operator configuration; it is never inferred or guessed.
"""
import math
import time

from truetrade.brokers.base import BrokerError, OrderRejected, Quote
from truetrade.brokers.mt5 import MT5Broker
from truetrade.risk.manager import decimal as D


class ServerTimeNormalizedMT5Broker(MT5Broker):
    def _server_to_utc(self, value):
        return float(value) - self.settings.server_utc_offset_seconds

    async def quote(self, name):
        self._account_info()
        tick = self.call("symbol_info_tick", name)
        result = Quote(D(tick.bid), D(tick.ask), self._server_to_utc(tick.time_msc / 1000.0))
        result.validate()
        return result

    async def candles(self, name, timeframe="M1", count=200, start=1):
        name = await self.resolve_symbol(name)
        self._symbol(name)
        if timeframe not in {"M1", "M5", "M15", "M30", "H1", "H4", "D1"} or type(count) is not int or not 1 <= count <= 10000:
            raise OrderRejected("Invalid candle request")
        if type(start) is not int or not 1 <= start <= 2000000:
            raise OrderRejected("Invalid history offset")
        bars = self.call("copy_rates_from_pos", name, getattr(self.api, "TIMEFRAME_" + timeframe), start, count)
        if len(bars) != count:
            raise BrokerError("Insufficient closed-bar history")
        output = []
        for bar in bars:
            item = {k: float(bar[k]) for k in ("open", "high", "low", "close")}
            item.update(time=int(self._server_to_utc(bar["time"])), volume=int(bar["tick_volume"]),
                        real_volume=int(bar["real_volume"]), spread=int(bar["spread"]))
            if not all(math.isfinite(v) for v in item.values()) or min(item[k] for k in ("open", "high", "low", "close")) <= 0:
                raise BrokerError("Invalid candle prices")
            if not item["low"] <= min(item["open"], item["close"]) <= max(item["open"], item["close"]) <= item["high"]:
                raise BrokerError("Invalid OHLC bounds")
            if min(item["volume"], item["real_volume"], item["spread"]) < 0 or item["time"] >= time.time():
                raise BrokerError("Invalid candle data")
            if output and item["time"] <= output[-1]["time"]:
                raise BrokerError("Unordered candle history")
            output.append(item)
        return output

    async def closed_outcome(self, pid):
        result = await super().closed_outcome(pid)
        result["opened_at"] = self._server_to_utc(result["opened_at"])
        result["closed_at"] = self._server_to_utc(result["closed_at"])
        return result
