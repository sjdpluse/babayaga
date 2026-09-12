from dataclasses import dataclass
import numpy as np
from truetrade.features.technical import WARMUP


@dataclass(frozen=True)
class Fold:
    train_end: int
    validation_start: int
    validation_end: int


def walk_forward(n, folds=3, embargo=5):
    if folds < 1 or embargo < 0: raise ValueError("Invalid split parameters")
    first_end = n // 2
    width = (n - first_end) // folds
    if first_end < WARMUP + 32 or width - embargo < WARMUP + 10:
        raise ValueError("More chronological history is required for walk-forward evaluation")
    return [Fold(first_end+i*width, first_end+i*width+embargo,
                 n if i == folds-1 else first_end+(i+1)*width) for i in range(folds)]


def metrics(equity, trades):
    e = np.asarray(equity, float)
    pnl = np.asarray([t["net_pnl"] for t in trades], float)
    peaks = np.maximum.accumulate(e)
    drawdown = 1 - e / np.maximum(peaks, 1e-12)
    returns = np.diff(e) / np.maximum(e[:-1], 1e-12)
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    return {"net_return": float(e[-1]/e[0]-1), "max_drawdown": float(drawdown.max()),
            "trades": len(pnl), "win_rate": float((pnl > 0).mean()) if len(pnl) else None,
            "expectancy": float(pnl.mean()) if len(pnl) else None,
            "profit_factor": float(wins.sum() / abs(losses.sum())) if len(losses) else None,
            "per_bar_sharpe": float(returns.mean()/returns.std()) if len(returns) and returns.std() else None,
            "final_equity": float(e[-1]), "fees_funding_included": True}


def evaluate(model, env):
    obs, _ = env.reset(seed=0)
    done = False
    while not done:
        action, _, _, probs = model.choose(obs, deterministic=True)
        obs, _, terminated, truncated, _ = env.step(action, confidence=float(probs[action]))
        done = terminated or truncated
    return metrics(env.equity_curve, env.closed)


def promotion_gate(reports, episodes, source, min_trades=30, max_drawdown=.15):
    reasons = []
    if source != "thetruetrade": reasons.append("non_exchange_data")
    if episodes < 2000: reasons.append("fewer_than_2000_completed_episodes")
    if len(reports) < 3: reasons.append("fewer_than_three_walk_forward_folds")
    for i, r in enumerate(reports):
        if r["trades"] < min_trades: reasons.append(f"fold_{i}_too_few_trades")
        if r["net_return"] <= 0 or r["expectancy"] is None or r["expectancy"] <= 0:
            reasons.append(f"fold_{i}_nonpositive_net_expectancy")
        if r["max_drawdown"] > max_drawdown: reasons.append(f"fold_{i}_drawdown")
    return {"eligible_for_demo_review": not reasons, "reasons": reasons,
            "exchange_execution_enabled": False}
