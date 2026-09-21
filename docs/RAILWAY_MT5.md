# Operational MT5 gold learning worker

This version collects real gold data, trains/evaluates PPO candidates and operates
automatically in demo after qualification. Setting connection variables starts that
workflow; it does not supply a trained model or guarantee a trade. Default strategy is
`ppo_cfd`, with no experimental-rule fallback. Live requires explicit authorization
and forward evidence for the same frozen model. The terminal still runs on Windows.
Read [GOLD_PPO.md](GOLD_PPO.md) for qualification, learning and remaining limitations.

## Automatic behavior

1. Authenticate to the agent and bind the worker database to its account identity and
   persistent journal ID. Changing account/journal requires operator investigation.
2. Query any previously sent decisions; never resend an uncertain POST.
3. Read 256 closed candles by default; validate timestamps, OHLCV and freshness.
   Tick volume is preserved as tick volume, not labeled real traded volume.
4. Backfill real history and obtain reviewed USD gold economics. Train in a separate
   process once 50,000 bars/180 days are available. A qualified PPO uses the existing
   causal features plus session/spread context to select HOLD/LONG/SHORT. No qualified
   model means no orders. The current candle is never used.
5. Set a 2-ATR stop distance from current Bid/Ask and a 4-ATR take-profit distance.
   Use 0.5% equity risk matching the qualified model; the agent calculates valid lots,
   costs, margin and final normalized protection. No crypto lot/leverage assumptions.
6. Persist the complete signal and stable per-bar decision ID before sending once.
   Include expected mode/account/journal and require the account to be flat. The agent
   rechecks these constraints within its execution lock, in addition to normal risk gates.
7. Verify journal outcome; allow only one open position across the dedicated account.
   Exit through broker SL/TP. No automatic reversal or trailing stop.
8. Record closed demo trade P&L and costs under the exact model identity. Calibrate
   subsequent training costs and evaluate new candidates on newly reserved holdouts.
   HOLD is a normal result, not a reason to force an order.

**Uncertainty:** an HTTP timeout is followed by status queries only. If the agent
journal confirms protected/closed/rejected, the worker records that outcome. `not_seen`
is NOT evidence that it is safe to resend. Unknown/not-seen decisions remain blocked
for manual investigation. There is no automatic reset, even after signal expiry.
Do not erase either database, change the agent journal, or generate replacement IDs
as a recovery workaround. Engine reconciliation still controls the agent halt latch.

## Railway setup

Use branch `feature/justmarkets-mt5`, the repository Dockerfile and `railway.json`.
Start command: `python -m truetrade.worker.mt5`. Healthcheck `/health`.
Run one replica, disable sleeping, and attach a persistent volume at `/app/data`.
With this Dockerfile's non-root user, Railway documents `RAILWAY_RUN_UID=0` for volume
permissions; a custom ownership-setting startup can retain non-root operation instead.

Variables for a deliberate DEMO deployment:

```dotenv
STATE_DIR=/app/data
RAILWAY_RUN_UID=0
MT5_REQUIRE_VOLUME=true
MT5_MODE=demo
ALLOW_LIVE_TRADING=false
MT5_STRATEGY=ppo_cfd
CFD_AUTO_TRAIN=true
CFD_TRAIN_EPISODES=2000
MT5_SYMBOL=XAUUSD
MT5_TIMEFRAME=M5
MT5_HISTORY_BARS=256
MT5_POLL_SECONDS=10
MT5_MAX_BAR_AGE_SECONDS=90
MT5_SIGNAL_RISK=0.005
```

Add these two values from your own Windows agent; do not use literal placeholders:

- `MT5_AGENT_URL`: the actual trusted HTTPS origin, for example the format
  `https://your-agent-domain:8787` (not localhost, not an MT5 broker server name).
- `MT5_AGENT_TOKEN`: the same random secret of at least 32 characters as Windows.

The worker starts even if those two values are absent and reports
`missing_agent_url_or_token`. Add the values and redeploy. It then collects data and
trains without manual signal commands, provided the Windows research settings and
terminal history are available. Until qualification, `no_qualified_ppo_model` is an
expected safe state. Weekends, holds, training failure and risk rejection can prevent
orders. Tests and successful deployment alone prove no trading performance.

`BOT_MODE`, `ENABLE_TRAINING`, `TRUETRADE_API_KEY` and `TRUETRADE_API_SECRET` are not
used by this MT5 worker. `MT5_LOGIN/PASSWORD/SERVER/TERMINAL_PATH` belong on Windows,
not on Railway. `/health` is liveness, `/ready` is current readiness (503 when blocked),
and `/status` explains the reason and last decision. No secrets, balances or account
login numbers are exposed by these public read-only endpoints.

## Windows agent setup

Update the Windows checkout to the **same latest branch version** before connecting.
The worker needs `/execution-state` protocol 1 and the `/history`, `/research-contract`
and `/outcome/{id}` APIs; an older agent will not work.
Follow README's exact installation, terminal login and permission steps first.
Use one dedicated **USD hedging DEMO** account, one agent and one durable directory.

Windows environment:

- Required: `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, `MT5_TERMINAL_PATH`.
- `MT5_MODE=demo`, `ALLOW_LIVE_TRADING=false`.
- `MT5_COMMISSION_PER_LOT`: reviewed round-trip commission in account currency/lot;
  explicit zero only if the account is confirmed commission-free.
- Required reviewed research assumptions: `MT5_RESEARCH_SWAP_LONG_PER_LOT_DAY`,
  `MT5_RESEARCH_SWAP_SHORT_PER_LOT_DAY`, `MT5_RESEARCH_ROLLOVER_UTC_HOUR`,
  `MT5_RESEARCH_TRIPLE_WEEKDAY`. Units and conversion guidance are in GOLD_PPO.md.
- `MT5_MAX_SPREAD_POINTS`: reviewed spread limit in symbol points.
- `MAX_TRADE_RISK=0.005`; retain reviewed aggregate/margin/drawdown limits.
- `MT5_ALLOWED_SYMBOLS=XAUUSD`; `MT5_SYMBOL_MAP` if suffix selection is ambiguous.
- `MT5_STATE_DIR`: persistent Windows folder, never temporary.
- `MT5_AGENT_TOKEN`: same secret as Railway.
- `MT5_AGENT_HOST=0.0.0.0`, `MT5_AGENT_PORT=8787` for direct network access.
- `MT5_AGENT_TLS_CERT`, `MT5_AGENT_TLS_KEY`: certificate/key PEM paths for a certificate
  trusted by the Railway Python client, matching the configured hostname.

Run `python -m truetrade.execution.agent`. Route DNS/network to that VPS and limit
firewall access to intended clients/private network. A generated Railway domain is
for the Linux worker, not a replacement for the Windows endpoint. Do not disable TLS
verification or use an untrusted self-signed certificate to bypass connection errors.
The MT5 terminal and agent must remain running; verify Windows restart/session behavior.

## Data and recovery

Railway automatically creates `/app/data/mt5-worker.sqlite`:
- `worker_bars`: received candles, keyed by symbol/timeframe/open time.
- `worker_decisions`: per-bar hold/skip/rejection and complete submitted signal/state.
- `worker_feedback`: closed trade economics, original risk and exact model identity.
- existing audit `events` and metadata tables.

`/app/data/cfd` stores dataset snapshots, the research holdout ledger, immutable
checkpoints, active/approved model manifests and training logs. Include it in backups.

Windows retains `MT5_STATE_DIR/journal.sqlite` for authoritative execution intents,
transitions, protection verification, account binding and halt state. Both use SQLite
WAL with FULL synchronous writes. Use SQLite's backup API for consistent backups.
A separate PostgreSQL/Supabase service is not required; no DATABASE_URL is consumed.
There is no automatic cross-host archive sync. Monitor disk space: histories are retained.

Never run worker replicas with independent databases against the same account.
A local file lease blocks concurrent processes sharing a directory; it is not a
cross-region distributed lock. `require_flat` and stable IDs add server-side protection.

Paper mode uses in-memory virtual inventory and is not a realistic CFD simulator.
For unattended repeated entry/exit checks use an actual demo account: the paper
adapter does not simulate automatic market-triggered SL/TP exits.

## Verification

```bash
python -m unittest discover -s tests -v
python -m truetrade.worker.mt5 --once
```

Tests use FakeMT5 and a local authenticated HTTP server. They cover real feature
calculation to HTTP signal to simulated terminal execution to both databases, restart,
concurrency, pre-send persistence, lost responses, no blind retries, stale/forming bars,
mode changes and server-side flat-account checks. No real terminal is initialized.

Operational acceptance still requires your Windows agent URL, token and terminal.
A successful Railway deployment alone proves neither broker connectivity nor strategy
profitability. Live needs the separate promotion procedure and evidence in GOLD_PPO.md,
explicit mode/allow flags on both hosts, and reviewed operational readiness. Code
defaults remain paper and `ALLOW_LIVE_TRADING=false`; no learning job changes these.
