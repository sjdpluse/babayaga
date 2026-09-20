# Gold PPO: data, learning and promotion

The automatic worker defaults to `MT5_STRATEGY=ppo_cfd`. It preserves the existing
NumPy PPO implementation, causal technical features, risk manager and execution
journals. It adds a separate CFD environment and qualification pipeline. No trained
gold weights are bundled. There are no measured real-market performance results yet.
Passing software tests does not establish profitable trading.

## Supported scope

Initial learned policy: **XAUUSD, USD-denominated dedicated hedging account, M5**.
Keep `MT5_SYMBOL=XAUUSD` even if the actual broker symbol needs a suffix; resolve that
on the agent using `MT5_SYMBOL_MAP`. Generic broker execution still supports other
Forex/CFD symbols, but this research contract rejects other assets and currencies
until their profit conversion and historical costs have been modeled and validated.
Changing a timeframe requires its own training; a checkpoint is never reused across
timeframes, feature schemas, risk settings or incompatible contract economics.

## What happens automatically in demo

1. The Windows agent reports actual symbol metadata and account-currency profit and
   margin calculations. It verifies linear USD gold economics and requires reviewed
   commission, swap and rollover settings. There is no invented gold price or fee.
2. The Railway worker collects closed candles through authenticated `/candles` and
   `/history`, backfilling 2,000 per cycle up to 60,000. Rows are deduplicated by UTC
   opening time. Tick volume remains tick volume. In MT5, increase **Tools → Options →
   Charts → Max bars in chart** and load the XAUUSD M5 chart history; the terminal must
   actually have the requested history available. Missing history blocks training.
3. With at least **50,000 bars spanning 180 calendar days**, a separate local Python
   process trains fresh PPO candidates. `CFD_AUTO_TRAIN=true` and
   `CFD_TRAIN_EPISODES=2000` are defaults. Three chronological expanding folds and a
   final candidate each receive that episode budget. CPU training can be substantial;
   monitor Railway CPU/memory/disk and `STATE_DIR/cfd/training.log`.
4. Training and inference use the same last 256 closed bars, the existing 18 technical
   features plus UTC hour sine/cosine and spread fraction. Normalization fits only
   training data. Actions are HOLD/LONG/SHORT at flat decision points; protection is
   2 ATR stop and 4 ATR target, with 0.5% equity risk including modeled costs. Lots
   use the same metadata-based sizing code as execution. Lower risk requires a
   correspondingly trained release; it is not silently substituted at inference.
5. A candidate must pass all three validation folds (30+ trades each), a previously
   unseen holdout (50+), and a 1.5x cost stress test (50+). Each requires positive net
   return, profit factor at least 1.2, drawdown no greater than 10%, and a positive
   lower 95% moving-block-bootstrap bound for expectancy in risk units. A no-loss
   sample has undefined profit factor and fails. These are engineering qualification
   gates, not a statistical guarantee of future returns.
6. The research ledger reserves a holdout **before** training, even for failed jobs.
   A later attempt needs at least 5,000 previously unused test bars after a five-bar
   embargo. Never delete this ledger to rerun until a favorable result appears.
   An incumbent is evaluated on the same new holdout; replacement must improve net
   return without increasing drawdown. The worker schedules fresh attempts after
   new data accumulates, not immediately after a failed test.
7. Only a qualified candidate is atomically activated for **demo**. Hashes bind the
   manifest, exact weights, feature schema and cost contract. A fully saved candidate
   can be recovered after restart. No qualified model means no automatic orders;
   there is no fallback to `breakout_demo`. That rule remains only as an explicitly
   selected demo connectivity test and is never allowed in live mode.
8. The frozen active policy submits durable, per-bar signals tagged with its model
   hash. Existing account binding, one-position rule, risk gates, duplicate protection,
   SL/TP verification, unknown-state halt and reconciliation remain in force.

## Learning from actual demo trades

After closure is verified by the agent, `/outcome/{decision_id}` returns deal-level
profit, commission, swap and fees with the original signal and risk/equity plan.
Missing monetary fields are errors, not zero rewards. Worker feedback is persisted
once per decision and linked to the exact model hash. Duplicate/overlapping evidence
cannot inflate qualification counts.

The next training dataset includes new real candles; observed demo commission and
adverse entry slippage raise the simulator's cost assumptions when they exceed the
reviewed estimates. Previously observed favorable costs never lower assumptions.
Actual trade P&L also supplies independent forward-demo evidence for that model.
The existing on-policy PPO optimizer generates fresh simulated rollouts; arbitrary
old live/demo trades are **not** replayed as if produced by the current policy.
This is batch retraining and cost calibration, not uncontrolled weight updates while
orders are open. No learning process edits a live policy in place.

## Required Windows settings

Follow README for terminal login, trading permissions, TLS and the agent token.
Additionally set all four values from reviewed account/symbol specifications:

| Variable | Meaning |
|---|---|
| `MT5_RESEARCH_SWAP_LONG_PER_LOT_DAY` | Nonnegative conservative USD cost per long lot per ordinary rollover day |
| `MT5_RESEARCH_SWAP_SHORT_PER_LOT_DAY` | Same for short positions |
| `MT5_RESEARCH_ROLLOVER_UTC_HOUR` | Integer 0–23, reviewed UTC rollover hour |
| `MT5_RESEARCH_TRIPLE_WEEKDAY` | Integer 0–4: Monday–Friday triple-cost weekday |

Do not paste raw MT5 swap values without converting their documented units into
account currency. A favorable swap credit can conservatively be modeled as zero;
zero expense is acceptable only after review. Include any swap-free account holding
charges. Review historical changes and daylight-saving effects. Missing settings
produce `waiting_for_reviewed_gold_contract` and block model qualification.
`MT5_COMMISSION_PER_LOT` remains the reviewed round-trip commission estimate.

## Demo to live uses the identical model

The observation function, normalizer, weights, deterministic action selection,
protection rules and execution checks are identical. Actual spreads, commissions,
slippage, liquidity and resulting performance may differ between accounts.

The application never automatically sets live flags. For a release to be eligible,
the **same model** must have at least 100 closed, nonoverlapping demo trades spanning
30 days after model creation and pass the economic gates above. Promotion refuses
missing, duplicated, insufficient or unprofitable evidence. With the demo worker's
durable data available, an operator can deliberately run:

```bash
python -m truetrade.cfd.feedback /app/data/cfd/active.json /app/data/mt5-worker.sqlite
```

On success this writes `live-approved.json` referencing exactly the existing weights;
it does not start live execution. Preserve a tested checkpoint with its matching
manifest, approval record and feedback database. Copy the approval record and its
referenced model directory together, preserving relative paths, to a **separate live
worker state directory**. Use a separate live Windows agent state directory, account
and token. Never repoint or erase the demo journal to make an account-binding error
disappear. Stop the intended live worker while configuring it, then set:

```dotenv
MT5_STRATEGY=ppo_cfd
CFD_MODEL_REGISTRY=/app/data/cfd/live-approved.json
CFD_AUTO_TRAIN=false
MT5_MODE=live
ALLOW_LIVE_TRADING=true
```

Both mode/allow flags must also be deliberately set on that account's Windows agent.
The live worker rechecks the approval evidence and current account's cost/contract
compatibility before entry. It never automatically replaces its release. If costs
exceed the trained envelope it blocks; validate a new candidate in demo first.
Model registry files are trusted operator-controlled local state, not signed remote
certificates. Protect them as carefully as execution configuration.

## Persistence and operations

- Railway: `STATE_DIR/mt5-worker.sqlite` contains candles, decisions and model-linked
  feedback. `STATE_DIR/cfd/research.sqlite` records consumed holdouts. Dataset snapshots,
  immutable `model-*` directories, active/approval records and training logs live under
  `STATE_DIR/cfd`. All must survive deployment. SQLite backups must be consistent.
- Windows: `MT5_STATE_DIR/journal.sqlite` remains authoritative for intent, fill,
  ownership, protection and uncertain execution. The model registry is not a substitute.
- One worker and one agent per dedicated account. A separate PostgreSQL database is
  not required. Histories/models are retained; monitor volume capacity and archive
  artifacts deliberately. There is no automatic pruning or remote backup.
- The status server/logs distinguish missing connection, missing reviewed contract,
  collecting history, training, failed qualification and an active model. `/health`
  is liveness; successful deployment does not imply trading readiness.

Offline reproduction of a collected dataset uses the same reserved-holdout directory:

```bash
python -m truetrade.cfd.pipeline DATASET.json MODEL_DIRECTORY --episodes 2000
python -m unittest discover -s tests -v
python -m unittest discover -s tests -p test_cfd_learning.py -v
```

Disable automatic training before a separate manual job; do not run competing
trainers against a registry. Test fixtures use synthetic data and explicitly labeled
software-only model manifests. They cannot establish market performance.

## Remaining limitations before capital is at risk

The simulator uses Bid OHLC and per-bar spread, adverse entry/exit allowances,
stop-first handling of ambiguous bars, gap losses and reviewed overnight charges.
It is not tick-level execution: intrabar sequencing, dynamic historical margin,
liquidity, stopout and fee-schedule changes are approximations. End-of-dataset open
positions are closed with modeled costs. Drawdown is measured at bar closes in
research and compounded closed-trade returns in forward evidence, not worst intratrade
drawdown. PPO discounts flat decision steps rather than elapsed clock time.

Deal-level costs exclude broker balance adjustments not attributable to a position;
later corrections to already stored outcomes are not automatically reimported.
Compare reports to terminal statements before promotion. There is no economic-news
calendar filter, general multiasset portfolio optimizer or guaranteed drift detection.
Review news/session risk, real-terminal restart/outage tests, slippage and cost
reconciliation, monitoring/alerts and backup restoration before considering live use.
More episodes, high win rate and these gates cannot guarantee accuracy or profitability.
