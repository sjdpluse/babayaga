"""Replay unseen history and save every policy decision, including hold. No orders."""
import argparse
from pathlib import Path
from truetrade.exchange.data import load_dataset
from truetrade.rl.ppo import PPO
from truetrade.rl.environment import TradingEnv
from truetrade.explain.decisions import explain
from truetrade.persistence.store import Journal


def shadow(checkpoint, dataset, journal_path):
    model,norm,meta = PPO.load(checkpoint)
    c,market,funding,lineage = load_dataset(dataset)
    env = TradingEnv(c,market,funding,norm,episode_length=len(c.close),random_start=False)
    obs,_ = env.reset(seed=0)
    journal = Journal(journal_path)
    decisions = 0
    try:
        while True:
            features = env.features[env.i].copy()
            timestamp = float(c.timestamp[env.i])
            action,_,_,probs = model.choose(obs,deterministic=True)
            next_obs, reward, term,trunc,info = env.step(action,float(probs[action]))
            record = explain(model,obs,features,meta["sha256"],info["risk_result"],risk=info)
            record.update(symbol=market.symbol,timestamp=timestamp,mode="historical_shadow",
                          source=lineage["source"],reward=reward)
            journal.append("decision_logs",record,record["id"])
            journal.append("risk_state",{"symbol":market.symbol,"equity":info["equity"],
                          "drawdown":info["drawdown"],"timestamp":timestamp,"mode":"historical_shadow"})
            decisions += 1; obs = next_obs
            if term or trunc: break
        for trade in env.closed:
            journal.append("trades",{**trade,"symbol":market.symbol,"model_version":meta["sha256"],"mode":"historical_shadow"})
    finally:
        journal.close()
    return decisions


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("checkpoint"); p.add_argument("dataset"); p.add_argument("--journal",default="data/journal.sqlite")
    a=p.parse_args(); print({"decisions_logged":shadow(a.checkpoint,a.dataset,a.journal)})


if __name__ == "__main__": main()
