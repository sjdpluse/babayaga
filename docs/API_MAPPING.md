# Read-contract integration

The supplied brief specifies paths, signature and an entry payload, but not the
complete read schema. Do not guess field names from another exchange.

The collector reads a reviewed JSON file from `API_MAPPING_PATH`. It requires:

- `reviewed_against_exchange`: boolean true only after actual schema verification.
- `evidence_reference`: a document/version reference, without credentials.
- `funding_range_complete_without_pagination`: true only if the requested range is
  complete in one funding response. If the API paginates, implement/verify its
  documented pagination first; do not set this to suppress a check.
- `resolution_seconds`: candle duration in seconds.
- `resolution_value`: the exchange's documented query value for that duration.
- `bars_per_request`: positive bounded chunk size (at least 2).
- `scan_top_n`: positive scanner result count.
- `markets`, `stats`, `funding`: each has `envelope` (dot-separated object path to
  the list, or empty for a top-level list) and `fields` (internal name -> API name).
- `history_params`, `funding_params`: internal query name -> exchange query name.
  Internal query names are `symbol`, `start`, `end`, `resolution`.

Required normalized market fields:

| Internal name | Meaning |
|---|---|
| `symbol` | Exchange's own symbol identifier |
| `active`, `linear` | Actual boolean flags, not arbitrary truthy strings |
| `tick`, `size_step` | Price and base-quantity precision increments |
| `min_size`, `min_notional` | Contract order minimums |
| `max_leverage` | Integer supported maximum |
| `fee_rate` | Verified per-side execution fee as decimal fraction |
| `slippage_rate` | Explicit conservative per-side calibration assumption |
| `maintenance_rate` | Verified margin input for the simulator proxy |

The current mapper directly maps fields. If fees/calibration or contract metadata
come from separate API objects, implement an explicit reviewed join in the adapter.
Do not pretend `slippage_rate` is necessarily a field in the exchange API.

Stats fields: `symbol`, `quote_volume`, `volatility`, `bid`, `ask`. Their units must
be reviewed. If spread/volatility require separate orderbook/candle calls, compute
and join them explicitly before ranking; the adapter must not fabricate them.

Funding fields: `timestamp` in UTC seconds and signed `rate` as a decimal fraction
per actual settlement. Positive rate means longs pay, shorts receive under this
simulation convention. Confirm this matches TheTrueTrade. Multiple events within
a bar are summed; settlement is approximated at that bar's close, not exact tick time.

The history parser currently supports the standard UDF-shaped response with `s`,
`t`, `o`, `h`, `l`, `c`, `v`. `s=ok` is required, incomplete candles are discarded,
timestamps must ascend without duplicates/gaps, and OHLC relationships are checked.
This schema remains user-contract-derived until tested against real responses.

Tests intentionally contain fixture mappings and synthetic symbols. They are not
production exchange configuration and must not be copied as verified API evidence.
