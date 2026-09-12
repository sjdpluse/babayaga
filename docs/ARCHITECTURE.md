# Architecture decisions — 2026-09-11

Status: executable research implementation; exchange demo activation blocked.

1. User-provided API contract is implemented for signed reads. The official public
   website confirms a demo UI, but does not establish API demo routing or the
   meaning of `walletType=debit`. All exchange writes are therefore blocked in
   code, including generic requests. No environment flag can bypass this.
2. Network client signs the exact percent-encoded transmitted path and query;
   body is excluded. It retries safe reads only, never follows redirects, never
   requests transfer/withdrawal access, and strips server messages from errors.
3. PPO actor/critic with a tanh hidden layer, categorical action distribution,
   GAE, clipped objective, Adam and gradient clipping. Implemented in NumPy so
   CPU research can run without a GPU/PyTorch dependency. This is PPO, not
   AlphaZero: no self-play claim, no perfect market simulator claim.
4. Gymnasium adapter wraps a dependency-light simulation core. Actions include
   hold, close, tighten protection and long/short entries with risk tiers and
   leverage choices. Risk/size and protection are never delegated to the model.
5. State includes causal technical features and account/position aggregates.
   Swings enter state only after confirmation. At close of bar t an action is
   chosen and executed at t+1 open with adverse slippage. Simultaneous TP/SL
   touches assume stop first. End-of-episode positions are liquidated with costs.
6. Linear quote-margined perpetual approximation only. Historical order books,
   liquidation rules, funding settlement convention and fees need exchange
   verification. Cost inputs must be explicit; missing costs never become zero.
7. 5% maximum stop-based estimated loss per trade, leverage 20–25, no count cap.
   Engineering defaults: 10% aggregate stop risk, 50% margin utilization,
   optional 15% peak-equity circuit breaker. Gap losses can exceed estimates;
   stops do not guarantee a monetary maximum. Contract precision, fees and
   maintenance margin enter sizing. Margin limits may reduce risk below 5%.
8. Chronological splits and expanding walk-forward validation. Normalizer fits
   training data only and is checkpoint-bound. Thousands of completed episodes
   are required before a model is eligible for demo review, not just timesteps.
9. PPO uses fresh on-policy rollouts. Archived demo experiences are retained for
   audit/calibration; blindly feeding an off-policy replay buffer to PPO is
   incorrect. Scheduled retraining generates fresh rollouts over newly collected
   TheTrueTrade candles. Direct demo-rollout fine-tuning awaits the demo adapter.
10. Local durable SQLite journal and transactional outbox; Supabase receives
    idempotent UUID-keyed audit records. Supabase schema is backend-only, RLS
    enabled, no anon/authenticated grants. Single execution worker; unknown
    order outcomes block new orders and require reconciliation.
11. Explanations report model probabilities and local feature-ablation effects.
    These are sensitivity measurements, not causal reasons, literal model
    weights, calibrated win probabilities or fabricated narratives.
12. The user created `sjdpluse/babayaga` for this project. Publish staged commits
    there and do not reuse unrelated repositories. Railway configuration is included, but a config
    file alone does not enable GitHub autodeploy or provision credentials.

## Primary sources checked
- https://www.thetruetrade.io/ (demo product and link)
- https://www.thetruetrade.io/profile/api-management (public page does not expose full contract)
- https://stable-baselines3.readthedocs.io/en/master/modules/ppo.html
- https://arxiv.org/abs/1707.06347
- https://supabase.com/docs/guides/database/postgres/row-level-security
- https://supabase.com/changelog (new tables need explicit grants; no blanket public grants here)

Exchange endpoint/body details originate in the supplied project brief. Fees,
response envelopes, market precision, demo API semantics and liquidation formula
have not been independently verified and must not be guessed.
