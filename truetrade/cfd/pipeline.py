"""Chronological CFD PPO training, untouched holdout and immutable releases."""
import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4
import numpy as np
from truetrade.cfd.contract import Contract
from truetrade.cfd.observations import cache, SCHEMA, ACTIONS, WINDOW
from truetrade.cfd.environment import CFDEnv
from truetrade.cfd.evaluation import evaluate, gate
from truetrade.features.technical import Candles, Normalizer
from truetrade.rl.ppo import PPO


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_json(path, value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+str(uuid4())+'.tmp')
    with temporary.open('w') as f:
        json.dump(value,f,allow_nan=False,indent=2); f.flush(); os.fsync(f.fileno())
    temporary.replace(path)


def validate_dataset(document):
    contract=Contract(**document['contract']); rows=document['rows']; seconds=document['seconds']
    if seconds not in {60,300,900,1800,3600,14400,86400} or len(rows)<600:
        raise ValueError('Insufficient/invalid dataset')
    Candles(**{k:[r['time' if k=='timestamp' else k] for r in rows] for k in Candles.__dataclass_fields__})
    for r in rows:
        if r['time']%seconds or not np.isfinite(r['spread']) or r['spread']<0:
            raise ValueError('Invalid spread/timestamp')
        r['_seconds']=seconds
    if rows[-1]['time']+seconds>document['captured_at'] or document['captured_at']>time.time()+2:
        raise ValueError('Future/forming data in dataset')
    return rows,contract


def train(dataset,output,episodes=2000,*,test_only=False):
    document=json.loads(Path(dataset).read_text()); rows,contract=validate_dataset(document)
    if contract.source=='synthetic_test' and not test_only:
        raise ValueError('Synthetic data cannot train a deployable model')
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    n=len(rows); span=(rows[-1]['time']-rows[0]['time'])/86400
    if not test_only and (n<50000 or span<180):
        raise ValueError('At least 50000 bars and 180 calendar days required')
    # Reserve a genuinely new test interval BEFORE training; failed attempts consume it too.
    ledger=sqlite3.connect(output/'research.sqlite')
    try:
        ledger.execute('CREATE TABLE IF NOT EXISTS attempts (end_time INTEGER PRIMARY KEY, dataset_sha TEXT NOT NULL)')
        previous=ledger.execute('SELECT max(end_time) FROM attempts').fetchone()[0]
        holdout=int(.8*n)
        if previous is not None:
            holdout=max(holdout,next((i for i,r in enumerate(rows) if r['time']>previous),n)+5)
        if n-holdout<(50 if test_only else 5000):
            raise ValueError('Wait for new untouched holdout data; no repeated peeking')
        with ledger:
            ledger.execute('INSERT INTO attempts VALUES (?,?)',(rows[-1]['time'],digest(dataset)))
    finally:ledger.close()
    x,atr=cache(rows,contract.symbol['point'])
    reports=[]; dev_end=holdout-5; first=max(WINDOW+10,int(dev_end*.5)); width=(dev_end-first)//3
    if width<10:raise ValueError('Insufficient chronological folds')
    for fold in range(3):
        end=first+fold*width; vs=end+5; ve=dev_end-1 if fold==2 else end+width-1
        norm=Normalizer.fit(x[WINDOW-1:end])
        model=PPO(len(SCHEMA),len(ACTIONS),seed=fold)
        model.train(CFDEnv(rows,contract,x,atr,norm,WINDOW-1,end-1),episodes)
        result=evaluate(model,CFDEnv(rows,contract,x,atr,norm,vs,ve,random_start=False))
        reports.append({'train_end':rows[end-1]['time'],'validation_start':rows[vs]['time'],
                        'validation_end':rows[ve]['time'],'metrics':result})
        print(json.dumps({'training_fold_completed':fold+1}),flush=True)
    norm=Normalizer.fit(x[WINDOW-1:dev_end]);model=PPO(len(SCHEMA),len(ACTIONS),seed=17)
    budget=model.train(CFDEnv(rows,contract,x,atr,norm,WINDOW-1,dev_end-1),episodes)
    normal=evaluate(model,CFDEnv(rows,contract,x,atr,norm,holdout,n-1,random_start=False))
    stressed=evaluate(model,CFDEnv(rows,contract,x,atr,norm,holdout,n-1,stress=1.5,random_start=False))
    reasons=[]
    for i,r in enumerate(reports):reasons += [f'fold_{i}:{s}' for s in gate(r['metrics'])]
    reasons += ['holdout:'+s for s in gate(normal,50)]
    reasons += ['stress:'+s for s in gate(stressed,50)]
    if episodes<2000:reasons.append('insufficient_training_budget')
    if n<50000 or span<180:reasons.append('insufficient_history')
    if contract.source!='mt5_demo' or test_only:reasons.append('not_real_mt5_demo_training')
    # Compare the frozen incumbent on this NEW holdout; never trade on a lower score.
    incumbent=None
    active=output/'active.json'
    if active.exists():
        prior,prior_norm,meta,_=load_release(active,mode='demo')
        if meta['train_end']>=rows[holdout]['time']:raise ValueError('Incumbent overlaps test window')
        incumbent=evaluate(prior,CFDEnv(rows,contract,x,atr,prior_norm,holdout,n-1,random_start=False))
        if normal['net_return']<=incumbent['net_return'] or normal['max_drawdown']>incumbent['max_drawdown']:
            reasons.append('candidate_does_not_improve_incumbent')
    metadata={'algorithm':'numpy_ppo_cfd_v1','feature_schema':SCHEMA,'actions':ACTIONS,'window':WINDOW,
        'canonical_symbol':'XAUUSD','seconds':document['seconds'],'contract':contract.json(),'risk_fraction':.005,
        'stop_atr':2.,'target_atr':4.,'training':budget,'dataset_sha256':digest(dataset),'history_bars':n,'history_days':span,
        'train_end':rows[dev_end-1]['time'],'holdout_start':rows[holdout]['time'],'holdout_end':rows[-1]['time'],
        'walk_forward':reports,'holdout':normal,'stress':stressed,'incumbent':incumbent,
        'eligible_demo':not reasons,'reasons':reasons,'created_at':time.time(),
        'feedback_used':document.get('feedback_calibration',{}),
        'limitations':'Bar-based USD gold, reviewed historical swap/margin proxies; requires real forward validation'}
    target=output/('model-'+str(uuid4()));manifest=model.save(target,norm,metadata)
    atomic_json(output/'latest-candidate.json',{'model_dir':target.name,'manifest_sha256':digest(target/'manifest.json'),
                                              'weights_sha256':manifest['sha256'],'eligible_demo':not reasons,'reasons':reasons})
    return target,manifest


def validate_qualification(meta):
    """Recheck recorded economic gates, not just an editable eligibility boolean."""
    if (not meta.get('eligible_demo') or meta.get('reasons') or meta['contract']['source']!='mt5_demo'
            or meta['history_bars']<50000 or meta['history_days']<180
            or meta['training']['episodes']<2000 or meta['training']['steps']<2000
            or not meta['train_end']<meta['holdout_start']<meta['holdout_end']
            or meta['stop_atr']!=2 or meta['target_atr']!=4 or meta['risk_fraction']!=.005
            or len(meta['walk_forward'])!=3):
        raise ValueError('Incomplete CFD qualification evidence')
    Contract(**meta['contract'])
    if gate(meta['holdout'],50) or gate(meta['stress'],50):
        raise ValueError('CFD holdout/stress gate failed')
    for fold in meta['walk_forward']:
        if (not fold['train_end']<fold['validation_start']<fold['validation_end']<=meta['train_end']
                or gate(fold['metrics'])):
            raise ValueError('Invalid walk-forward evidence')
    incumbent=meta.get('incumbent')
    if incumbent and (meta['holdout']['net_return']<=incumbent['net_return']
                      or meta['holdout']['max_drawdown']>incumbent['max_drawdown']):
        raise ValueError('Candidate does not improve incumbent')


def load_release(path,mode='demo'):
    path=Path(path);record=json.loads(path.read_text());directory=(path.parent/record['model_dir']).resolve()
    if not directory.is_relative_to(path.parent.resolve()):raise ValueError('Model path outside registry')
    if digest(directory/'manifest.json')!=record['manifest_sha256']:raise ValueError('Manifest changed')
    model,norm,meta=PPO.load(directory)
    validate_qualification(meta)
    if (meta['algorithm']!='numpy_ppo_cfd_v1' or meta['feature_schema']!=SCHEMA or meta['actions']!=ACTIONS
        or meta['window']!=WINDOW or meta['sha256']!=record['weights_sha256'] or not meta['eligible_demo']
        or model.updates<1 or model.parameters['w1'].shape[0]!=len(SCHEMA) or len(model.parameters['bp'])!=3
        or norm.mean.shape!=(len(SCHEMA),) or norm.scale.shape!=(len(SCHEMA),)):
        raise ValueError('Incompatible/unqualified CFD model')
    if mode=='live':
        evidence=record.get('live_evidence')
        if (not evidence or evidence['model_sha256']!=meta['sha256'] or not evidence['eligible']
                or evidence.get('reasons') or gate(evidence.get('metrics',{}),100)
                or not np.isfinite(evidence.get('days',float('nan'))) or evidence['days']<30
                or len(evidence.get('decision_ids',[]))!=evidence['metrics']['trades']
                or len(set(evidence['decision_ids']))!=len(evidence['decision_ids'])):
            raise ValueError('Live requires evidence for exactly these model weights')
    return model,norm,meta,record


def activate(directory,registry,*,evidence=None):
    directory,registry=Path(directory).resolve(),Path(registry).resolve()
    if not directory.is_relative_to(registry.parent):raise ValueError('Registry must contain model directory')
    _,_,m=PPO.load(directory)
    validate_qualification(m)
    record={'model_dir':str(directory.relative_to(registry.parent)),
            'manifest_sha256':digest(directory/'manifest.json'),'weights_sha256':m['sha256'],
            'activated_at':time.time(),'live_evidence':evidence}
    atomic_json(registry,record)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('dataset');p.add_argument('output')
    p.add_argument('--episodes',type=int,default=2000)
    a=p.parse_args();_,m=train(a.dataset,a.output,a.episodes)
    print(json.dumps({'eligible_demo':m['eligible_demo'],'reasons':m['reasons']}))


if __name__=='__main__':main()
