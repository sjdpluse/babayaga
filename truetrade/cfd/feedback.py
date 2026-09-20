"""Real deal-level demo evidence and conservative cost calibration, not PPO replay."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import numpy as np
from truetrade.cfd.evaluation import summarize,gate
from truetrade.cfd.pipeline import load_release,activate,atomic_json


def validate_feedback(document,key):
    if document['decision_id']!=key:raise ValueError('Wrong feedback decision')
    o,p,s=document['outcome'],document['plan'],document['signal']
    required=('net_pnl','profit','commission','fee','swap','volume','entry','opened_at','closed_at')
    if any(not np.isfinite(float(o[k])) for k in required):raise ValueError('Incomplete trade outcome')
    if (float(o['volume'])<=0 or any(not np.isfinite(float(p.get(k,0))) or float(p.get(k,0))<=0
                                   for k in ('risk','equity'))):
        raise ValueError('Missing original risk/equity')
    if float(o['closed_at'])<float(o['opened_at']) or o['mode'] not in {'demo','live'}:
        raise ValueError('Invalid actual execution evidence')
    if not np.isclose(float(o['net_pnl']),sum(float(o[k]) for k in ('profit','commission','fee','swap')),atol=1e-6):
        raise ValueError('Outcome economics do not reconcile')
    if not o['deal_ids'] or len(o['deal_ids'])!=len(set(o['deal_ids'])):raise ValueError('Missing/duplicated deals')
    return document


def demo_evidence(feedback,model_sha,*,created_at=0):
    selected=[f for f in feedback if f['signal'].get('model_sha256')==model_sha and f['outcome']['mode']=='demo']
    selected.sort(key=lambda f:f['outcome']['closed_at'])
    curve=[1.];trades=[];ids=set();deals=set();prior_close=created_at
    for f in selected:
        validate_feedback(f,f['decision_id'])
        o,p=f['outcome'],f['plan'];pnl=float(o['net_pnl']);risk=float(p['risk']);eq=float(p['equity'])
        if (f['decision_id'] in ids or deals.intersection(o['deal_ids']) or o['currency']!='USD'
                or float(o['opened_at'])<prior_close):
            raise ValueError('Repeated, overlapping or pre-model demo evidence')
        ids.add(f['decision_id']);deals.update(o['deal_ids']);prior_close=float(o['closed_at'])
        curve.append(curve[-1]*(1+pnl/eq))
        trades.append({'net_pnl':pnl,'r_multiple':pnl/risk})
    if not trades:return {'model_sha256':model_sha,'eligible':False,'reasons':['no_closed_demo_trades']}
    metrics=summarize(curve,trades); reasons=gate(metrics,100)
    span=(selected[-1]['outcome']['closed_at']-selected[0]['outcome']['opened_at'])/86400
    if span<30:reasons.append('less_than_30_days_forward_demo')
    return {'model_sha256':model_sha,'eligible':not reasons,'reasons':reasons,'metrics':metrics,'days':span,
            'decision_ids':[f['decision_id'] for f in selected],
            'drawdown_scope':'compounded_closed_trade_returns; not intratrade drawdown'}


def calibrate(contract,feedback):
    fee=contract.commission; slip=contract.entry_slippage_points; count=0
    for f in feedback:
        o,p=f['outcome'],f['plan']
        if o['mode']!='demo' or o['currency']!=contract.currency:continue
        lots=float(o['volume']);count+=1
        fee=max(fee,(-float(o['commission'])-float(o['fee']))/lots)
        sign=1 if p['side']=='LONG' else -1
        slip=max(slip,max(0,sign*(float(o['entry'])-float(p['entry'])))/float(contract.symbol['point']))
    # Never improve assumed costs from a small favorable sample.
    return replace(contract,commission=fee,entry_slippage_points=slip),{'closed_demo_trades':count,
        'method':'maximum_of_reviewed_and_observed_costs; fresh_on_policy_simulation_not_trade_replay'}


def authorize_live(registry,db_path):
    model,norm,meta,record=load_release(registry,mode='demo')
    db=sqlite3.connect(f'file:{Path(db_path).resolve()}?mode=ro',uri=True)
    try:feedback=[json.loads(r[0]) for r in db.execute('SELECT payload FROM worker_feedback WHERE model_sha=?',(meta['sha256'],))]
    finally:db.close()
    evidence=demo_evidence(feedback,meta['sha256'],created_at=meta['created_at'])
    if not evidence['eligible']:raise ValueError('Live evidence gate failed: '+','.join(evidence['reasons']))
    # Approval is explicit; an active demo model is never silently switched to live.
    record['live_evidence']=evidence
    target=Path(registry).with_name('live-approved.json');atomic_json(target,record)
    return target


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('registry');p.add_argument('worker_db')
    a=p.parse_args();print(authorize_live(a.registry,a.worker_db))


if __name__=='__main__':main()
