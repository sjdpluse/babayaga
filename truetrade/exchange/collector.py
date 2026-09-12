"""Read-only collector with explicitly reviewed API field mappings.

Never infer precision/fees/wallet type or substitute data from another exchange.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from uuid import uuid4
import numpy as np
from truetrade.exchange.scanner import map_records, rank
from truetrade.exchange.data import parse_udf, save_dataset
from truetrade.features.technical import Candles
from truetrade.risk.manager import Market, decimal


def mapped(payload, spec):
    return map_records(payload, spec["envelope"], spec["fields"])


def request_params(spec, symbol, start, end, resolution):
    values = {"symbol":symbol, "start":start, "end":end, "resolution":resolution}
    return {api_key: values[internal] for internal, api_key in spec.items()}


def align_funding(candles, events, resolution_seconds):
    """Funding timestamps are UTC seconds; events are settled at containing bar close."""
    rates = np.zeros(len(candles.close))
    seen = set()
    for event in events:
        timestamp, rate = float(event["timestamp"]), float(event["rate"])
        if not np.isfinite(timestamp) or not np.isfinite(rate): raise ValueError("Invalid funding event")
        if timestamp in seen: raise ValueError("Duplicate funding settlement timestamp")
        seen.add(timestamp)
        idx = np.searchsorted(candles.timestamp + resolution_seconds, timestamp, side="left")
        if 0 <= idx < len(rates) and candles.timestamp[idx] <= timestamp:
            rates[idx] += rate
    return rates


async def collect(client, config_path, output_root, start, end, journal=None):
    config = json.loads(Path(config_path).read_text())
    if config.get("reviewed_against_exchange") is not True or not config.get("evidence_reference"):
        raise ValueError("Collection requires reviewed response mappings and an evidence reference")
    if config.get("funding_range_complete_without_pagination") is not True:
        raise ValueError("Funding pagination/coverage must be verified for the requested range")
    if not start < end <= int(time.time()): raise ValueError("Invalid collection time range")
    seconds = int(config["resolution_seconds"])
    if seconds <= 0: raise ValueError("Invalid resolution")
    if int(config.get("bars_per_request", 500)) < 2 or int(config.get("scan_top_n",3)) < 1:
        raise ValueError("Invalid history/scanner batch settings")
    markets_raw = await client.markets()
    stats_raw = await client.stats()
    markets = mapped(markets_raw, config["markets"])
    stats = mapped(stats_raw, config["stats"])
    candidates = rank(markets, stats, max_spread_bps=float(config.get("max_spread_bps", 20)))
    if not candidates: raise ValueError("No eligible markets from exchange metadata/stats")
    normalized = {row["symbol"]:row for row in markets}
    paths = []
    for candidate in candidates[:int(config.get("scan_top_n", 3))]:
        row = normalized[candidate.symbol]
        spec = {k: row[k] for k in Market.__dataclass_fields__}
        market = Market(**{k: spec[k] if k in {"symbol","max_leverage"} else decimal(spec[k]) for k in spec})
        chunks = []
        cursor = start
        # A short response is not treated as proof that history has ended.
        while cursor < end:
            chunk_end = min(end, cursor + seconds * int(config.get("bars_per_request", 500)))
            params = request_params(config["history_params"], candidate.symbol, cursor, chunk_end, config["resolution_value"])
            payload = await client.history(**params)
            c = parse_udf(payload, now=end, resolution_seconds=seconds)
            chunks.append(c)
            cursor = chunk_end
        rows = {}
        for c in chunks:
            for i, timestamp in enumerate(c.timestamp):
                if not start <= timestamp < end: continue
                values = tuple(getattr(c,k)[i] for k in Candles.__dataclass_fields__)
                if timestamp in rows and rows[timestamp] != values:
                    raise ValueError("Conflicting historical candles across overlapping requests")
                rows[timestamp] = values
        matrix = np.asarray([rows[k] for k in sorted(rows)])
        if matrix.ndim != 2: raise ValueError("No valid historical candles")
        c = Candles(**{k:matrix[:,i] for i,k in enumerate(Candles.__dataclass_fields__)})
        if (np.diff(c.timestamp) != seconds).any(): raise ValueError("History coverage has gaps")
        if c.timestamp[0] > start+seconds or c.timestamp[-1]+seconds < end-seconds:
            raise ValueError("History response did not cover the requested range")
        fp = request_params(config["funding_params"], candidate.symbol, start, end, config["resolution_value"])
        funding_raw = await client.funding(**fp)
        events = mapped(funding_raw, config["funding"])
        funding = align_funding(c, events, seconds)
        target = Path(output_root) / ("capture-"+str(uuid4()))
        save_dataset(target, c, funding, market, "thetruetrade", {
            "collected_at":datetime.now(timezone.utc).isoformat(), "base_url":"https://apiv2.thetruetrade.io",
            "start":start,"end":end,"resolution_seconds":seconds,
            "mapping_evidence":config["evidence_reference"],
            "funding_events":len(events), "funding_convention":"settlement at containing candle close",
            "slippage":"explicit calibration input; not historical execution certainty"})
        paths.append(target)
        if journal:
            journal.append("market_snapshots", {"symbol":candidate.symbol,"score":candidate.score,
                "dataset_path":str(target),"source":"thetruetrade","bars":len(c.close)})
    return paths
