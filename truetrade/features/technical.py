"""Causal features. Input timestamps are candle OPEN seconds, UTC."""
from dataclasses import dataclass
import numpy as np

FEATURE_NAMES = ("return_1", "return_5", "ema_8_distance", "ema_21_distance", "ema_55_distance",
    "ema_cross", "vwap_distance", "atr_fraction", "rsi", "relative_volume",
    "support_distance", "resistance_distance", "support_slope", "resistance_slope",
    "bos_up", "bos_down", "breakout_up", "breakout_down")
WARMUP = 64


@dataclass
class Candles:
    timestamp: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            setattr(self, name, np.asarray(getattr(self, name), dtype=np.float64))
        if len(self.timestamp) < 2 or any(len(getattr(self, k)) != len(self.timestamp) for k in self.__dataclass_fields__):
            raise ValueError("Candle lengths differ or fewer than two candles")
        if any(not np.isfinite(getattr(self, k)).all() for k in self.__dataclass_fields__):
            raise ValueError("Non-finite candles")
        if (np.diff(self.timestamp) <= 0).any() or (self.timestamp < 0).any():
            raise ValueError("Candles must have unique ascending UTC timestamps")
        if (self.low <= 0).any() or (self.volume < 0).any():
            raise ValueError("Invalid price or volume")
        if (self.high < np.maximum(self.open, self.close)).any() or (self.low > np.minimum(self.open, self.close)).any():
            raise ValueError("OHLC bounds inconsistent")

    def subset(self, start, end):
        return Candles(**{k: getattr(self, k)[start:end] for k in self.__dataclass_fields__})


def ema(x, period):
    y = np.empty(len(x), dtype=float)
    y[0] = x[0]
    alpha = 2 / (period + 1)
    for i in range(1, len(x)):
        y[i] = alpha * x[i] + (1 - alpha) * y[i - 1]
    return y


def compute(c: Candles, swing_window=3):
    n = len(c.close)
    if swing_window < 1:
        raise ValueError("Swing window must be positive")
    prev = np.r_[c.close[0], c.close[:-1]]
    tr = np.maximum(c.high - c.low, np.maximum(abs(c.high - prev), abs(c.low - prev)))
    atr = ema(tr, 27)  # alpha=1/14, Wilder-style seeded at first observation
    delta = c.close - prev
    up, down = ema(np.maximum(delta, 0), 27), ema(np.maximum(-delta, 0), 27)
    rsi = np.divide(up, up + down, out=np.full(n, .5), where=(up + down) > 0)
    e8, e21, e55 = (ema(c.close, p) for p in (8, 21, 55))
    out = np.zeros((n, len(FEATURE_NAMES)))
    out[:, 0] = np.log(c.close / prev)
    out[:, 1] = np.log(c.close / np.r_[np.repeat(c.close[0], min(5, n)), c.close[:-5]])
    out[:, 2:5] = np.column_stack((c.close / e8 - 1, c.close / e21 - 1, c.close / e55 - 1))
    out[:, 5] = (e8 - e21) / c.close
    out[:, 7] = atr / c.close
    out[:, 8] = rsi * 2 - 1
    highs, lows = [], []
    pv, vol, day = 0., 0., None
    last_high = last_low = None
    for i in range(n):
        current_day = int(c.timestamp[i] // 86400)
        if current_day != day:
            pv, vol, day = 0., 0., current_day
        pv += (c.high[i] + c.low[i] + c.close[i]) / 3 * c.volume[i]
        vol += c.volume[i]
        out[i, 6] = c.close[i] / (pv / vol if vol else c.close[i]) - 1
        past_volume = c.volume[max(0, i - 20):i]
        baseline = past_volume.mean() if len(past_volume) else c.volume[i]
        out[i, 9] = c.volume[i] / baseline - 1 if baseline > 0 else 0
        # At time i, candidate i-w has enough RIGHT bars for confirmation.
        k, w = i - swing_window, swing_window
        if k >= w:
            if c.high[k] == max(c.high[k-w:i+1]) and c.high[k] > max(c.high[k-w:k]):
                highs.append((k, c.high[k])); highs = highs[-5:]
                last_high = c.high[k]
            if c.low[k] == min(c.low[k-w:i+1]) and c.low[k] < min(c.low[k-w:k]):
                lows.append((k, c.low[k])); lows = lows[-5:]
                last_low = c.low[k]
        for points, col in ((lows, 10), (highs, 11)):
            if len(points) >= 2:
                xs, ys = np.array(points).T
                slope, intercept = np.polyfit(xs - xs[0], ys, 1)
                level = intercept + slope * (i - xs[0])
                out[i, col] = (c.close[i] - level) / c.close[i]
                out[i, col + 2] = slope / c.close[i]
        if i:
            out[i, 14] = bool(last_high and c.close[i] > last_high >= c.close[i-1])
            out[i, 15] = bool(last_low and c.close[i] < last_low <= c.close[i-1])
            # Breakout of prior 20 closed candles; current high/low excluded.
            out[i, 16] = c.close[i] > max(c.high[max(0, i-20):i])
            out[i, 17] = c.close[i] < min(c.low[max(0, i-20):i])
    if not np.isfinite(out).all():
        raise ValueError("Non-finite feature vector")
    return out, atr


@dataclass
class Normalizer:
    mean: np.ndarray
    scale: np.ndarray

    @classmethod
    def fit(cls, train):
        if len(train) < 2 or not np.isfinite(train).all():
            raise ValueError("Invalid training features")
        return cls(train.mean(axis=0), np.maximum(train.std(axis=0), 1e-6))

    def transform(self, x):
        return np.clip((x - self.mean) / self.scale, -10, 10).astype(np.float32)
