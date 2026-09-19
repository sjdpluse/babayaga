# MT5 migration validation

## Scope

All trading calls are mocked. No broker login, demo trade or live trade was performed
by development tests. Linux Python execution does not load the Windows MT5 binary.

Local checks:

- `python -m unittest discover -s tests -v`: **97 tests passed** (50 existing/regression + 47 MT5/config/API tests).
- `python -m unittest discover -s tests -p test_mt5.py -v`: **47 tests passed**.
- `python -m truetrade.main --once`: existing research worker starts with trading blocked.
- `python -m compileall -q truetrade scripts tests`: successful compilation.
- `git diff --check`: no whitespace errors.

The Windows CPython 3.12 wheel `MetaTrader5 5.0.6180` was downloaded for static export
inspection, not executed. It does not export `SYMBOL_FILLING_FOK`/`SYMBOL_FILLING_IOC`.
The adapter uses the documented symbol bitmask values 1/2 locally and the actual
exported ORDER_FILLING enums for requests. All other constant references were checked
against that wheel. This is not a terminal integration test or a package-version pin.

## Required demonstrations

| Scenario | Verified result |
|---|---|
| XAUUSD LONG, fixture equity 10,000, risk 1%, reviewed costs | 0.09 lots; estimated risk 94.23; protected observed position and trade journal event |
| Duplicate exact decision | One order_send total; duplicate suppressed |
| Same ID with changed payload | Rejected |
| Restart with protected decision | Duplicate still suppressed |
| Lost response after simulated fill | Unknown + persistent halt; original/new IDs blocked; no retry after restart |
| Partial fill | Known position emergency-close attempt; halt remains until reconciliation |
| Unverified SL/TP | Emergency close attempted; halt remains even after confirmed close |
| Live mode without allow flag | Rejected before order_send |
| Demo mode pointed at REAL account | Rejected before order_send |
| Paper mode | No terminal order_send |
| Order/deal/position IDs differ | Resolved via deal.position_id and position.identifier |
| Invalid spread, stops, volume, margin, stale quotes or expired signal | Rejected |
| Missing profit/margin calculation or unreviewed commission | Rejected; no invented fallback |
| Invalid auth / arbitrary configuration request | Rejected |
| HTTP timeout | Client raises uncertain; sends once |
| Local HTTP integration | Authenticated signal → journal; subsequent status query succeeds |
| Foreign position / changed account / netting mode | Execution blocked |

GitHub Actions is configured to run the same tests on Ubuntu and Windows. CI execution
status is available in the pull request/checks; the local results above do not imply
that Windows/terminal smoke testing has been performed.

Before any live rollout, the operator must complete the real-terminal demo checks,
CFD strategy/cost validation, secure deployment and recovery procedures in README.
