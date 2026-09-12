# What remains before exchange demo activation

1. **Connection.** Put the exchange key/secret in Railway Variables. Run
   `python -m scripts.check_connection`. Check IP allowlist, key status, relevant
   scopes and clock before changing signing code. Do not grant withdrawal/transfer.
2. **Official demo contract.** Obtain documentation showing the precise demo
   base/path/header/wallet selector and a read-only account response that proves
   the authenticated target is demo. The public demo webpage does not prove the
   same API key routes to demo. `debit` is not assumed to mean demo.
3. **Read adapters.** Verify response envelopes/query names, pagination, candle
   timestamps and close semantics, contract size units, tick/step/minimums, fees,
   margin method and funding conventions. Implement mappings/joins using actual
   responses. Confirm complete requested history rather than guessing defaults.
4. **Dedicated persistence.** Configure the chosen Supabase project, apply the
   bootstrap schema through the project's migration workflow, run the SQL checks,
   and confirm a durable writable Railway state volume and backup for model bytes.
5. **Train and evaluate.** Acquire only TheTrueTrade data. Run the offline workflow
   at the episode budget, inspect out-of-sample results and compare to hold/random
   baselines across seeds. Calibrate execution costs. Do not promote a model just
   because a short synthetic smoke test succeeded.
6. **Reviewed exchange demo broker.** Implement and test server-side demo proof,
   atomic stop/target attachment, fill-aware size validation, account-wide risk,
   startup reconciliation, stale-data rejection, partial fills, external closes,
   pending orders and unknown write outcomes. Add close/tighten position management
   to the scheduler. Never auto-retry an uncertain order without documented
   exchange idempotency or a verified request lookup.
7. **Demo learning integration.** Record every decision and every transition with
   policy version, pre-risk action, executed action, old log-probability, actual
   reward, terminal state and account state. Wire trade-count retrain triggers.
   For direct PPO demo fine-tuning, use fresh on-policy rollouts with a frozen
   behavior policy per rollout and correct episode boundaries. Keep historical
   replay/calibration separate. Shadow/evaluate a candidate before switching.

No request to remove the write guard should be implemented solely by adding an
environment flag. The missing adapter and verified contract are substantive work.

## Implementation choices worth reviewing

- 5% per-trade loss estimate with 20–25x is aggressive. The margin and aggregate
  risk constraints frequently reduce actual risk well below that ceiling.
- A price gap can lose more than the stop estimate. The simulator records those
  losses; the system cannot promise an absolute monetary loss cap at a market stop.
- Current episodes can hold multiple positions in one symbol. Cross-symbol RL
  portfolio training and calibrated orderbook simulation are future work.
- The current feature set uses several EMA horizons on one candle resolution;
  synchronized multi-resolution features are not yet implemented.
- Checkpoint ablation explanations describe local sensitivity, not causal
  knowledge of the market or a calibrated probability of a profitable trade.
- The optional Gymnasium adapter has not been exercised in the initial local
  runtime because Gymnasium was not installed. The underlying simulator and PPO
  have direct tests.
