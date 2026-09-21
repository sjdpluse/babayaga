"""Predeclared economic gates; win rate alone is not a promotion criterion."""
import numpy as np


def summarize(equity,trades):
    e=np.asarray(equity,float); r=np.asarray([t['r_multiple'] for t in trades],float)
    pnl=np.asarray([t['net_pnl'] for t in trades],float)
    if len(e)<2 or not np.isfinite(e).all() or not np.isfinite(r).all() or not np.isfinite(pnl).all():
        raise ValueError('Invalid evaluation results')
    lower=None
    if len(r)>=10:
        # Moving-block bootstrap retains short-range serial dependence. Still not a guarantee.
        rng=np.random.default_rng(812); block=5; samples=[]
        for _ in range(1000):
            starts=rng.integers(0,len(r)-block+1,size=int(np.ceil(len(r)/block)))
            samples.append(np.concatenate([r[x:x+block] for x in starts])[:len(r)].mean())
        lower=float(np.quantile(samples,.025))
    losses=-pnl[pnl<0].sum(); wins=pnl[pnl>0].sum()
    return {'trades':len(r),'net_return':float(e[-1]/e[0]-1),
            'max_drawdown':float(np.max(1-e/np.maximum.accumulate(e))),
            'expectancy_r':float(r.mean()) if len(r) else None,'expectancy_lower95_r':lower,
            'profit_factor':float(wins/losses) if losses>0 else None,
            'win_rate':float((pnl>0).mean()) if len(r) else None}


def gate(report,min_trades=30):
    reasons=[]
    required=('trades','net_return','max_drawdown','profit_factor','expectancy_lower95_r')
    if any(k not in report or (report[k] is not None and not np.isfinite(report[k])) for k in required):
        return ['invalid_evaluation_metrics']
    if (type(report['trades']) is not int or report['trades']<0 or report['net_return'] is None
            or report['max_drawdown'] is None or report['max_drawdown']<0):
        return ['invalid_evaluation_metrics']
    if report['trades']<min_trades:reasons.append('insufficient_closed_trades')
    if report['net_return']<=0:reasons.append('nonpositive_net_return')
    if report['max_drawdown']>.10:reasons.append('drawdown_above_10_percent')
    if report['profit_factor'] is None or report['profit_factor']<1.2:reasons.append('profit_factor_below_1_2_or_no_losses')
    if report['expectancy_lower95_r'] is None or report['expectancy_lower95_r']<=0:reasons.append('expectancy_uncertain_or_negative')
    return reasons


def evaluate(model,env):
    obs,_=env.reset(); done=False
    while not done:
        action,*_=model.choose(obs,deterministic=True)
        obs,_,a,b,_=env.step(action); done=a or b
    return summarize(env.equity_curve,env.closed)
