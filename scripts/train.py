import argparse
import json
import time
from truetrade.rl.trainer import train_dataset


def main():
    p = argparse.ArgumentParser(description="Train PPO with chronological walk-forward and held-out evaluation")
    p.add_argument("dataset"); p.add_argument("--output", default="artifacts")
    p.add_argument("--episodes", type=int, default=2000)
    p.add_argument("--seed", type=int, default=0)
    a = p.parse_args()
    last = [0.]
    def progress(record):
        if time.monotonic()-last[0] > 10 or "completed_fold" in record:
            print(json.dumps(record), flush=True); last[0] = time.monotonic()
    path, report = train_dataset(a.dataset, a.output, a.episodes, seed=a.seed, progress=progress)
    print(json.dumps({"checkpoint": str(path), "promotion": report["promotion"], "holdout": report["holdout"]}, indent=2))


if __name__ == "__main__": main()
