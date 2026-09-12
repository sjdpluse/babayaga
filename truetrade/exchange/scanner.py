"""Rank normalized records; field mapping must be confirmed against real responses."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class Candidate:
    symbol: str
    quote_volume: float
    volatility: float
    bid: float
    ask: float
    score: float


def rank(markets, stats, max_spread_bps=20., min_volume=0.):
    active = {m["symbol"] for m in markets if m["active"] is True and m["linear"] is True and m["max_leverage"] >= 20}
    result = []
    for s in stats:
        if s["symbol"] not in active:
            continue
        volume, volatility, bid, ask = (float(s[k]) for k in ("quote_volume", "volatility", "bid", "ask"))
        if not all(math.isfinite(v) for v in (volume, volatility, bid, ask)):
            continue
        if bid <= 0 or ask < bid or volume <= min_volume or volatility <= 0:
            continue
        spread = (ask - bid) / ((ask + bid) / 2) * 10000
        if spread > max_spread_bps:
            continue
        score = math.log1p(volume) * min(volatility, .10) / (1 + spread)
        result.append(Candidate(s["symbol"], volume, volatility, bid, ask, score))
    return sorted(result, key=lambda x: (-x.score, x.symbol))


def map_records(payload, envelope, mapping):
    """Explicit dot-path envelope + normalized-key -> exchange-key mapping; no guessing."""
    value = payload
    for key in envelope.split(".") if envelope else []:
        value = value[key]
    if not isinstance(value, list):
        raise ValueError("Expected a list at the reviewed response envelope")
    return [{target: row[source] for target, source in mapping.items()} for row in value]
