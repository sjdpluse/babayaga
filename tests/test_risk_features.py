from dataclasses import replace
import unittest
import numpy as np
from truetrade.config import RiskLimits
from truetrade.risk.manager import RiskManager, RiskRejected, decimal as d
from truetrade.features.technical import compute, Normalizer
from truetrade.exchange.scanner import rank
from helpers import candles, market, account


class RiskTests(unittest.TestCase):
    def size(self, a=None, m=None, **kwargs):
        return RiskManager().size(m or market(), a or account(), "LONG", 100, .3, .9, now=1000, **kwargs)

    def test_five_percent_stop_risk_with_costs(self):
        p = self.size()
        self.assertLessEqual(p.risk, d("500"))
        self.assertEqual(p.size % market().size_step, 0)
        self.assertGreater(p.risk, p.size * abs(p.entry-p.stop))
        self.assertLess(p.stop, p.entry)
        self.assertGreater(p.take_profit, p.entry)

    def test_portfolio_budget_not_position_count(self):
        p = self.size(replace(account(), open_risk=d("990")))
        self.assertLessEqual(p.risk, d("10"))
        with self.assertRaises(RiskRejected): self.size(replace(account(), open_risk=d("1000")))

    def test_stale_unprotected_unknown_accounts_blocked(self):
        for a in (replace(account(), timestamp=900), replace(account(), all_protected=False), replace(account(), pending_uncertain=True)):
            with self.assertRaises(RiskRejected): self.size(a)

    def test_leverage_and_liquidation(self):
        for leverage in (10, 19, 26):
            with self.assertRaises(RiskRejected): self.size(leverage=leverage)
        with self.assertRaises(RiskRejected):
            RiskManager().size(market(), account(), "LONG", 100, 5, .9, now=1000)

    def test_circuit_and_config(self):
        with self.assertRaises(RiskRejected): self.size(replace(account(), equity=d("8000")))
        with self.assertRaises(ValueError): RiskLimits(max_trade_risk=d("0.051"))
        with self.assertRaises(ValueError): RiskLimits(max_portfolio_risk=d("NaN"))

    def test_short_and_tightening(self):
        r = RiskManager()
        p = r.size(market(), account(), "SHORT", 100, .3, .9, now=1000)
        self.assertGreater(p.stop, p.entry)
        self.assertLess(p.take_profit, p.entry)
        with self.assertRaises(RiskRejected): r.validate_tightening("LONG", 99, 98, 100, 102)

    def test_coarse_tick_cannot_increase_risk(self):
        p = self.size(m=replace(market(), tick=d("0.5")))
        self.assertLessEqual(p.risk, p.budget)


class FeatureTests(unittest.TestCase):
    def test_prefix_invariance_no_future_leakage(self):
        c = candles()
        full, atr = compute(c)
        for end in (65, 100, 201, 499):
            prefix, pat = compute(c.subset(0, end))
            np.testing.assert_allclose(full[:end], prefix, atol=1e-12)
            np.testing.assert_allclose(atr[:end], pat)

    def test_normalizer_fits_training_only(self):
        x = np.array([[1., 2.], [3., 4.]])
        n = Normalizer.fit(x)
        np.testing.assert_allclose(n.mean, [2,3])
        transformed = n.transform(np.array([1000., -1000.]))
        self.assertTrue(np.isfinite(transformed).all())
        self.assertLessEqual(abs(transformed).max(), 10)

    def test_scanner_dynamic_symbols_and_spread(self):
        markets = [{"symbol":s, "active":True,"linear":True,"max_leverage":25} for s in ("NEW", "WIDE")]
        stats = [{"symbol":"NEW","quote_volume":100,"volatility":.02,"bid":100,"ask":100.01},
                 {"symbol":"WIDE","quote_volume":10000,"volatility":.05,"bid":100,"ask":105}]
        self.assertEqual([c.symbol for c in rank(markets, stats)], ["NEW"])
