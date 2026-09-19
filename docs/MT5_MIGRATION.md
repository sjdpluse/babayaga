# MT5 migration review

| Classification | Existing files | Decision |
|---|---|---|
| Broker-independent | features/technical.py; rl/ppo.py, trainer.py, evaluation.py; explain/decisions.py | Preserve features, policy and validation. No MT5 imports. |
| The True Trade-specific | exchange/client.py, collector.py, scanner.py, data.py; scripts/check_connection.py, collect.py | Retain the read-only legacy integration; do not pretend it is MT5. |
| Reusable but coupled | execution/engine.py; risk/manager.py; persistence/store.py; main.py | Generalize execution contract, retain crypto sizing for legacy data, add a separate CFD risk calculator, extend existing SQLite journal. Keep legacy worker available. |
| Obsolete for the new path | paper-only engine constructor guard; 20–25x linear crypto assumptions for CFDs | Replace the guard with the broker safety contract. Never apply crypto liquidation sizing to MT5. No existing file is deleted. |

The existing historical simulator models linear crypto contracts and funding. It must not be used to claim Forex/CFD backtest performance without a separate cost/session/margin calibration. This migration supplies execution infrastructure and an explicit signal entry point; it does not claim a validated autonomous gold strategy.
