import time
import unittest
from pathlib import Path
import tempfile

from fake_mt5 import FakeMT5
from truetrade.brokers.mt5_config import MT5Settings
from truetrade.brokers.mt5_server_time import ServerTimeNormalizedMT5Broker
from truetrade.persistence.store import Journal
from truetrade.risk.manager import decimal as D


class ShiftedMT5(FakeMT5):
    OFFSET = 10800

    def symbol_info_tick(self, name):
        tick = super().symbol_info_tick(name)
        tick.time_msc += self.OFFSET * 1000
        return tick

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        rows = super().copy_rates_from_pos(symbol, timeframe, start, count)
        return [{**row, "time": row["time"] + self.OFFSET} for row in rows]


class ServerTimeNormalizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self.tmp.name) / "journal.sqlite")
        self.api = ShiftedMT5()
        self.settings = MT5Settings(
            login=12345,
            password="fixture-secret",
            server="Fixture-Demo",
            terminal_path="fixture-terminal",
            mode="demo",
            commission_per_lot=D("7"),
            server_utc_offset_seconds=10800,
        )
        self.broker = ServerTimeNormalizedMT5Broker(self.settings, self.journal, api=self.api)
        await self.broker.connect()

    async def asyncTearDown(self):
        await self.broker.shutdown()
        self.journal.close()
        self.tmp.cleanup()

    async def test_future_server_tick_is_normalized_to_fresh_utc(self):
        quote = await self.broker.quote("XAUUSD")
        self.assertLess(abs(time.time() - quote.timestamp), 2)

    async def test_server_shifted_bars_are_normalized_to_utc(self):
        rows = await self.broker.candles("XAUUSD", "M1", 5)
        self.assertLess(rows[-1]["time"], time.time())
        self.assertLess(time.time() - rows[-1]["time"], 180)

    def test_offset_validation_rejects_unreasonable_values(self):
        with self.assertRaises(ValueError):
            MT5Settings(server_utc_offset_seconds=90000)


if __name__ == "__main__":
    unittest.main()
