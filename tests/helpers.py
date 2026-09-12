import numpy as np
from truetrade.features.technical import Candles
from truetrade.risk.manager import Market, Account, decimal as d


def market():
    return Market("SYNTHETIC_TEST", d("0.01"), d("0.001"), d("0.001"), d("1"), 25,
                  d("0.0004"), d("0.0002"), d("0.005"))


def account(now=1000.):
    return Account(d("10000"), d("10000"), d("0"), d("0"), d("10000"), now)


def candles(n=600, seed=1):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, .001, n)))
    o = np.r_[close[0], close[:-1]]
    return Candles(1700000000 + np.arange(n)*60, o, np.maximum(o, close)*1.001,
                   np.minimum(o, close)*.999, close, rng.uniform(50, 150, n))
