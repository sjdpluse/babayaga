"""Explicit data lineage, canonical candle files and no fallback data vendors."""
import hashlib
import json
from pathlib import Path
import numpy as np
from truetrade.features.technical import Candles
from truetrade.risk.manager import Market, decimal


def parse_udf(payload, now, resolution_seconds):
    """UDF response convention from the brief; contract must be checked on real API."""
    if not isinstance(payload, dict) or payload.get("s") != "ok":
        raise ValueError("Expected UDF s=ok; do not treat API errors/no_data as empty prices")
    raw = Candles(**{dest: payload[src] for dest, src in {
        "timestamp": "t", "open": "o", "high": "h", "low": "l", "close": "c", "volume": "v"}.items()})
    closed = raw.timestamp + resolution_seconds <= now
    c = Candles(**{k: getattr(raw, k)[closed] for k in raw.__dataclass_fields__})
    if (np.diff(c.timestamp) != resolution_seconds).any():
        raise ValueError("Missing or irregular candles; never fill OHLC gaps silently")
    return c


def save_dataset(path, candles, funding, market, source, provenance):
    path = Path(path); path.mkdir(parents=True, exist_ok=False)
    if source not in {"thetruetrade", "synthetic_test"}: raise ValueError("Unsupported data source")
    arrays = {k: getattr(candles, k) for k in candles.__dataclass_fields__}
    arrays["funding"] = np.asarray(funding, dtype=float)
    if arrays["funding"].shape != candles.close.shape or not np.isfinite(arrays["funding"]).all():
        raise ValueError("Explicit aligned funding required")
    np.savez_compressed(path / "candles.npz", **arrays)
    metadata = {"source": source, "symbol": market.symbol, "market": {
        k: str(getattr(market, k)) if k not in {"symbol", "max_leverage"} else getattr(market, k)
        for k in market.__dataclass_fields__}, "provenance": provenance,
        "sha256": hashlib.sha256((path / "candles.npz").read_bytes()).hexdigest()}
    (path / "dataset.json").write_text(json.dumps(metadata, indent=2, allow_nan=False))


def load_dataset(path):
    path = Path(path)
    meta = json.loads((path / "dataset.json").read_text())
    if meta["source"] not in {"thetruetrade", "synthetic_test"}: raise ValueError("Invalid source")
    if hashlib.sha256((path / "candles.npz").read_bytes()).hexdigest() != meta["sha256"]:
        raise ValueError("Dataset hash mismatch")
    with np.load(path / "candles.npz", allow_pickle=False) as d:
        c = Candles(**{k: d[k].copy() for k in Candles.__dataclass_fields__})
        funding = d["funding"].copy()
    if (np.diff(c.timestamp) != np.diff(c.timestamp)[0]).any():
        raise ValueError("Dataset contains missing or irregular candles")
    m = meta["market"]
    market = Market(**{k: m[k] if k in {"symbol", "max_leverage"} else decimal(m[k]) for k in Market.__dataclass_fields__})
    return c, market, funding, meta
