# Baba Yaga — TheTrueTrade RL research bot

**نسخهٔ فعلی: هستهٔ پژوهشی قابل اجرا؛ معامله در صرافی هنوز فعال نیست.**

این پروژه از صفر برای Sajad ساخته شده است. تصمیم‌گیری با PPO انجام می‌شود؛
EMA، VWAP، ساختار سوئینگ، BOS و شکست‌ها ورودی مدل هستند. قواعد ریسک مستقل‌اند.
هیچ سود، نرخ موفقیت یا همگرایی مدل ادعا نشده است. دادهٔ واقعی صرافی هنوز دریافت
نشده و آموزش واقعی روی حساب دمو انجام نشده است.

## Current capabilities and exact limits

| Component | Implemented | Remaining before exchange demo operation |
|---|---|---|
| Exchange client | Exact URI HMAC, milliseconds, read allowlist, rate limiting, retry, clock/auth diagnostics | Real-key connection test; confirm full API response contract |
| Market selection | Dynamic metadata/statistics ranking and explicit field mapping | Supply reviewed response mapping; verify volume, spread and contract fields |
| Features | EMA 8/21/55, UTC session VWAP, confirmed swings, fitted support/resistance, BOS, breakout, ATR, RSI, volume | Validate feed semantics and actual candle quality |
| RL | NumPy PPO actor/critic, GAE, clipping, Adam; optional Gymnasium adapter | Exchange-history training, multi-seed validation and benchmark comparison |
| Simulation | Multiple positions per episode, next-bar entry, costs, funding events, gap handling, terminal close | Order-book execution calibration and exact exchange liquidation rules |
| Risk | Stop-based sizing, 5% trade ceiling, 20–25x, aggregate risk and margin budgets, configurable circuit | Fresh account parsing and actual position/fill reconciliation |
| Execution | Paper broker, durable intent journal, duplicate suppression, stop verification, unknown-outcome halt | Verified demo broker; **all exchange POST/PATCH/DELETE requests are blocked** |
| Learning loop | Time-scheduled collection/retraining, on-policy historical rollouts, checkpoint gates and rollback functions | Actual demo rollouts and trade-trigger wiring depend on verified demo broker |
| Explanations | Raw probabilities, value, feature ablation, Persian explanation, complete historical shadow journal | Exchange decision stream integration |
| Persistence | Durable SQLite outbox, Supabase REST sink, backend-only SQL schema and grant checks | Dedicated Supabase connection, schema application and database test |
| Deployment | Docker, Railway worker, health/status endpoints, GitHub CI | Credentials, durable data volume and reviewed API mappings |

This is a research release, **not an operational autonomous demo trader**. A deployment
can be healthy while trading readiness is blocked. The brief's `walletType=debit`
does not establish demo routing. No environment flag can bypass the exchange write
block. Live-money trading is not implemented.

## Run locally

Python 3.11+; Python 3.12 and NumPy 2.3.5 were used for verification.

```bash
git clone https://github.com/sjdpluse/babayaga.git
cd babayaga
python -m venv .venv
```

Activate the virtual environment for your OS, then:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m truetrade.main --once
```

The default worker starts in research mode with no credentials and sends no orders.
It does not invent data or start training without an exchange dataset. Configuration
is read from process environment variables, not automatically from a `.env` file.

## First connection test

Set the API key and secret in environment variables or Railway Variables. Never
paste keys into source, issues, commits or logs.

On Railway, adding both exchange credentials and redeploying automatically runs
the same connection preflight, including in research mode. Inspect the redacted
`connection_preflight` entry in deployment logs. This does not unlock orders.

```bash
python -m scripts.check_connection
```

The first authenticated request is `GET /users/profile`. A 403 on profile may mean
the optional readonly scope is absent, so the test separately checks futures markets.
Never diagnose every 401/403 as a signature problem: check API-key IP allowlist,
key activation, scopes and host clock first. Do not automatically disable an
allowlist. Use a permitted stable egress address when available.

Required scope: `api-keys.trade-futures`. Optional profile scope:
`api-keys.readonly`. Never grant transfer or withdrawal scope.

## Collect and train

The API brief does not include complete read response schemas or funding pagination.
`docs/API_MAPPING.md` describes the exact internal fields that need to be mapped.
The collector fails if those fields, fee inputs, precision or coverage are missing.

```bash
python -m scripts.collect data/reviewed-api-mapping.json --start START_UNIX_SECONDS --end END_UNIX_SECONDS
python -m scripts.train data/captures/CAPTURE_DIRECTORY --episodes 2000 --output data/models
python -m scripts.backtest data/models/MODEL_DIRECTORY data/captures/UNSEEN_CAPTURE_DIRECTORY
python -m scripts.shadow data/models/MODEL_DIRECTORY data/captures/UNSEEN_CAPTURE_DIRECTORY
python -m truetrade.main --explain DECISION_UUID
```

Replace uppercase path/time placeholders with real capture paths and timestamps.
The collector chooses symbols dynamically; no production trading symbol list is
hardcoded. It requires contiguous closed candles. It never fills market-data gaps
with fabricated prices or falls back to Binance or another vendor.

The final 20% of a dataset is held out. The development period uses three expanding
walk-forward splits. Each training fold fits its own normalizer on training data
only. At the default budget each fold and the final candidate complete 2,000
episodes; these episodes reuse historical windows and are not 2,000 independent
market regimes. CPU requirements depend on history length and episode budget.

Model review eligibility requires at least 2,000 completed episodes, positive net
expectancy/return, enough closed trades and bounded drawdown on all evaluation
segments. Passing these gates does not prove profitability and does not enable
exchange execution. The cost model approximates linear isolated-margin contracts;
it is not suitable for claims about sub-second scalping or exchange matching quality.

PPO uses **fresh on-policy rollouts**. Old trades are audit/calibration records, not
a SAC-style replay buffer to feed blindly into PPO. Automatic retraining currently
uses newly collected history. The training workflow builds new candidate policies;
continuous fine-tuning on actual demo transitions remains blocked with demo execution.
Each captured symbol is trained separately; cross-symbol portfolio learning is not
implemented. Risk aggregation in the execution interface covers its full account.

The optional Gymnasium adapter is available through `gymnasium_env` in
`truetrade/rl/environment.py`; install the project's `gym` extra to use it.
The dependency-light simulator and NumPy PPO run without that extra.

## Risk settings

The requested 5% is an estimated loss ceiling at the stop, not a margin allocation.
Fees and an explicit slippage allowance are included. Leverage is limited to 20–25.
There is no count cap on positions. Engineering defaults cap aggregate estimated
stop risk at 10% and used margin at 50% of marked equity. The optional circuit breaker
stops new entries after a 15% peak-equity decline; it can be disabled independently.

Gaps, stop execution delays and liquidation can exceed an estimated stop loss.
The exchange's exact maintenance/liquidation method must be verified before any
demo adapter is activated. Size is rounded downward to contract step size and then
rechecked; protection levels are rounded before monetary risk is calculated.

## Environment variable names

| Names | Purpose |
|---|---|
| `TRUETRADE_API_KEY`, `TRUETRADE_API_SECRET` | Exchange authentication; server environment only |
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | Dedicated backend database; server environment only |
| `BOT_MODE` | Research or collection operation; demo is deliberately blocked |
| `STATE_DIR`, `PORT` | Durable state location and status server port |
| `REQUEST_INTERVAL_SECONDS`, `REQUEST_TIMEOUT_SECONDS` | Conservative HTTP request limits |
| `API_MAPPING_PATH` | Reviewed non-secret response-mapping JSON file |
| `COLLECT_INTERVAL_SECONDS`, `HISTORY_BARS` | Collection frequency and historical window |
| `ENABLE_TRAINING`, `TRAIN_EPISODES`, `RETRAIN_SECONDS` | Enable/budget/schedule historical retraining |
| `MAX_TRADE_RISK`, `MAX_PORTFOLIO_RISK`, `MAX_MARGIN_UTILIZATION` | Independent monetary risk controls |
| `CIRCUIT_ENABLED`, `CIRCUIT_DRAWDOWN` | Optional drawdown circuit breaker |

## Supabase

Use a dedicated project. `supabase/schema.sql` creates immutable audit tables with
UUID event keys and JSONB payloads. All tables have RLS enabled; anon/authenticated
roles have no grants. Only the server service role can insert/read. Duplicate
outbox deliveries use `ON CONFLICT DO NOTHING`; they do not rewrite history.

This SQL has not been executed against a live Supabase database in this session.
After application, run `supabase/tests/permissions.sql` and verify insertion plus
anonymous denial. The Supabase CLI was unavailable while authoring; the bootstrap
file is not falsely presented as an applied migration. For managed migrations,
create a migration through `supabase migration new` and follow your project's flow.

Weights remain in versioned files with SHA-256 integrity checks. The Supabase
checkpoint record contains metadata and a path, **not the weight bytes**. Back up
the durable models directory; Supabase metadata alone cannot restore a checkpoint.

## Railway

Connect this repository's `main` branch to a dedicated Railway service. Docker and
`railway.json` define build/start and process health checks. The GitHub connection
must have autodeploy enabled; merely committing `railway.json` does not connect it.
Use one worker replica. Mount durable writable storage at the state directory
before enabling collection/training. A redeploy without durable storage loses local
history, model weights and the undelivered audit outbox.

- `/health`: process status; HTTP 200 does not mean trading is enabled.
- `/status`: non-secret operating status.
- `/ready`: HTTP 503 until a verified exchange demo implementation exists.

The worker has no public endpoint that can place orders, change configuration or
read account secrets. See `docs/ACTIVATION.md` for the outstanding integration work.

## Validation

`python -m unittest discover -s tests -v` covers the signed request path, retry/auth
handling, future-leakage regression, monetary limits, mathematical PPO gradients,
actual weight updates, checkpoint integrity, temporal splits, end-to-end training,
funding alignment, collection using an explicit fixture contract, execution failure
recovery, outbox retry and readiness behavior. Synthetic fixtures test software only;
their returns are not evidence of trading performance.

Primary documentation and design choices: [Architecture](docs/ARCHITECTURE.md).
