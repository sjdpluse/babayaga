"""Expanding walk-forward fits, untouched final holdout, checkpoint promotion gate."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from uuid import uuid4
from truetrade.config import RiskLimits
from truetrade.exchange.data import load_dataset
from truetrade.features.technical import compute, Normalizer, FEATURE_NAMES, WARMUP
from truetrade.risk.manager import RiskManager
from truetrade.rl.environment import TradingEnv, ACTIONS, ACCOUNT_FEATURES
from truetrade.rl.ppo import PPO
from truetrade.rl.evaluation import walk_forward, evaluate, promotion_gate


def train_dataset(dataset_path, output_root, episodes=2000, folds=3, seed=0, progress=None):
    c, market, funding, lineage = load_dataset(dataset_path)
    output_root = Path(output_root); output_root.mkdir(parents=True, exist_ok=True)
    # Final 20% never used to train or fit normalizers. Do not repeatedly tune on it.
    holdout_start = int(len(c.close)*.8)
    if len(c.close)-holdout_start <= WARMUP+10: raise ValueError("Insufficient final holdout")
    splits = walk_forward(holdout_start, folds=folds)
    reports = []
    limits = RiskLimits.from_env()
    for index, fold in enumerate(splits):
        train_c = c.subset(0, fold.train_end)
        norm = Normalizer.fit(compute(train_c)[0][WARMUP:])
        env = TradingEnv(train_c, market, funding[:fold.train_end], norm, RiskManager(limits))
        model = PPO(env.n_features, len(ACTIONS), seed=seed+index)
        model.train(env, episodes, progress=progress)
        valid_c = c.subset(fold.validation_start, fold.validation_end)
        valid = TradingEnv(valid_c, market, funding[fold.validation_start:fold.validation_end], norm,
                           RiskManager(limits), episode_length=len(valid_c.close), random_start=False)
        reports.append({**evaluate(model, valid), "split": asdict(fold)})
        if progress: progress({"completed_fold": index+1, "validation": reports[-1]})
    # Train final candidate on development period only; preserve final holdout.
    train_c = c.subset(0, holdout_start)
    norm = Normalizer.fit(compute(train_c)[0][WARMUP:])
    env = TradingEnv(train_c, market, funding[:holdout_start], norm, RiskManager(limits))
    model = PPO(env.n_features, len(ACTIONS), seed=seed)
    training = model.train(env, episodes, progress=progress)
    holdout_c = c.subset(holdout_start, len(c.close))
    holdout_env = TradingEnv(holdout_c, market, funding[holdout_start:], norm, RiskManager(limits),
                            episode_length=len(holdout_c.close), random_start=False)
    holdout = evaluate(model, holdout_env)
    gate = promotion_gate(reports + [holdout], training["episodes"], lineage["source"])
    metadata = {"created_at": datetime.now(timezone.utc).isoformat(), "algorithm": "numpy_ppo_v1",
                "feature_schema": list(FEATURE_NAMES+ACCOUNT_FEATURES), "actions": ACTIONS,
                "dataset_sha256": lineage["sha256"], "source": lineage["source"],
                "symbol": market.symbol, "risk_limits": {k:str(v) for k,v in asdict(limits).items()},
                "training": training, "walk_forward": reports, "holdout": holdout,
                "holdout_start": holdout_start, "promotion": gate,
                "simulation_limitations": "Linear bar-based isolated margin approximation; no queue/fill-depth calibration"}
    target = output_root / ("model-"+str(uuid4()))
    manifest = model.save(target, norm, metadata)
    (output_root / "latest-candidate.json").write_text(json.dumps({"path": str(target.resolve()), "sha256": manifest["sha256"]}, indent=2))
    return target, manifest


class RetrainSchedule:
    def __init__(self, every_seconds=21600, every_trades=50):
        if every_seconds <= 0 or every_trades <= 0: raise ValueError("Positive retrain intervals required")
        self.every_seconds, self.every_trades = every_seconds, every_trades
        self.last = time.monotonic(); self.trades = 0; self.last_dataset_hash = None

    def record_closed_trade(self): self.trades += 1

    def due(self, dataset_hash, now=None):
        now = time.monotonic() if now is None else now
        return dataset_hash != self.last_dataset_hash and (now-self.last >= self.every_seconds or self.trades >= self.every_trades)

    def completed(self, dataset_hash):
        self.last, self.trades, self.last_dataset_hash = time.monotonic(), 0, dataset_hash


def activate_candidate(model_path, registry_path):
    """Atomic research champion pointer. Does not unlock exchange trading."""
    _, _, m = PPO.load(model_path)
    if not m.get("promotion", {}).get("eligible_for_demo_review"):
        raise ValueError("Candidate failed promotion gates")
    registry = Path(registry_path); registry.parent.mkdir(parents=True, exist_ok=True)
    previous = json.loads(registry.read_text()) if registry.exists() else None
    record = {"current": str(Path(model_path).resolve()), "sha256": m["sha256"], "previous": previous}
    temp = registry.with_suffix(".tmp")
    temp.write_text(json.dumps(record, indent=2)); temp.replace(registry)


def rollback(registry_path):
    registry = Path(registry_path); record = json.loads(registry.read_text())
    previous = record.get("previous")
    if not previous: raise ValueError("No prior checkpoint")
    PPO.load(previous["current"])
    temp = registry.with_suffix(".tmp")
    temp.write_text(json.dumps(previous, indent=2)); temp.replace(registry)
