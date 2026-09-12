import argparse
import json
from truetrade.exchange.data import load_dataset
from truetrade.rl.environment import TradingEnv
from truetrade.rl.evaluation import evaluate
from truetrade.rl.ppo import PPO


def main():
    p = argparse.ArgumentParser(description="Evaluate a checkpoint on a separately supplied dataset")
    p.add_argument("checkpoint"); p.add_argument("dataset")
    a = p.parse_args()
    model, norm, meta = PPO.load(a.checkpoint)
    c, market, funding, lineage = load_dataset(a.dataset)
    if lineage["sha256"] == meta.get("dataset_sha256"):
        raise SystemExit("This is the training/development dataset. Its internal holdout is already in manifest.json; supply unseen data for a new backtest.")
    env = TradingEnv(c, market, funding, norm, episode_length=len(c.close), random_start=False)
    print(json.dumps(evaluate(model, env), indent=2))


if __name__ == "__main__": main()
