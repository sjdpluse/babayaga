# MT5 submission certainty and diagnostics

The adapter separates read-only entry preparation and `order_check` from the
single `order_send` call. A pre-submit failure raises `OrderNotSubmitted` (an
`OrderRejected` subclass). The engine durably records `rejected`, keeps that
decision ID deduplicated, and does not latch a new execution halt. A fresh,
independently qualified decision must pass all risk checks again.

Once `order_send` is attempted, a missing response, exception, timeout, placed
order, unrecognized code, or unverified fill remains uncertain. The engine
persists `unknown` and halts. There is no automatic write retry or reconnect and
resend. Existing emergency-close behavior for an identified unsafe position is
preserved; it never clears the halt by itself.

Only an explicit allowlisted broker rejection with zero order ticket, zero deal
ticket, and zero executed volume is classified as rejected after a send. Missing
or contradictory receipt fields remain uncertain. Generic `BrokerError` from an
unknown adapter phase is still fail-closed.

## Diagnostic event fields

Rejected and unknown trade events retain `phase`, `error_type`, sanitized `error`,
and numeric `receipt` fields. `broker_diagnostic` includes the failing MT5 method,
`stage`, `send_attempted`, and (where available) `retcode` and `last_error`.

`last_error` is captured immediately after a failing MT5 call, before another
terminal call can replace it. Its code is retained. Its message is normalized to
the documented error category; recognized invalid request argument names are
retained. Arbitrary vendor descriptions, exception text, credentials, paths,
comments, and raw request/response objects are not logged. Failure to read
`last_error` is itself recorded without changing submission certainty.

Relevant codes: `-2` indicates invalid arguments, while `-10000` through `-10005`
indicate IPC problems (the documented codes are not contiguous). Do not diagnose
IPC solely from a `None` return. Compare the diagnostic from the persistent
agent with a read-only standalone check using the same request, Python/package
version, terminal, Windows user/session, and account. The historical event that
only says `order_check returned no result` cannot identify the root cause.

References: [MetaQuotes last_error](https://www.mql5.com/en/docs/python_metatrader5/mt5lasterror_py)
and [trade server return codes](https://www.mql5.com/en/docs/constants/errorswarnings/enum_trade_return_codes).

## Existing halted journals

This change does not migrate or clear old unknown intents. Before any manual
resolution, verify the running agent's authenticated `/execution-state`, its
exact state directory and `agent_state_id`, the broker identity, and matching
positions, pending orders, historical orders and deals. A failed history read
is not proof of absence. Do not delete or reset `journal.sqlite`, and never
resubmit an unknown decision. Retain the audit trail of any operator resolution.

Keep `MT5_MODE=demo` and `ALLOW_LIVE_TRADING=false`. Restart the local agent under
its configured Windows account to load code changes. The restart alone does not
clear the existing halt. This change does not alter PPO qualification or Railway
training settings.

## Verification

Run `.venv/Scripts/python.exe -m unittest discover -s tests -q` from the repository.
The MT5 tests use `FakeMT5` and temporary journals; they never send terminal orders.
