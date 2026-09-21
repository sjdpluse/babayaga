"""Nonblocking data collection/training controller for the existing worker."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import time
from truetrade.cfd.contract import Contract
from truetrade.cfd.observations import observation
from truetrade.cfd.pipeline import atomic_json,load_release,activate
from truetrade.cfd.feedback import calibrate,validate_feedback


class Learning:
    def __init__(self,store,settings,root):
        self.store,self.settings,self.root=store,settings,Path(root)
        self.root.mkdir(parents=True,exist_ok=True)
        self.process=None;self.joblog=None;self.contract=None
        self.status='waiting_for_market_data';self.policy=None
        self.registry=Path(os.getenv('CFD_MODEL_REGISTRY',str(self.root/'active.json')))
        self.last_profile=0

    def compatible(self,meta,seconds):
        return (meta['canonical_symbol']==self.settings.symbol and meta['seconds']==seconds
                and meta['risk_fraction']==float(self.settings.risk)
                and Contract(**meta['contract']).compatible(self.contract))

    async def sync_feedback(self,client):
        for key,payload in self.store.feedback_pending():
            signal=json.loads(payload)
            if not signal.get('model_sha256'):continue
            document=validate_feedback(await client.outcome(key),key)
            if document['signal'].get('model_sha256')!=signal['model_sha256']:
                raise ValueError('Feedback policy identity mismatch')
            self.store.save_feedback(key,signal['model_sha256'],document)

    async def service(self,client,rows,seconds):
        cfg=self.settings;stream=cfg.symbol+':'+cfg.timeframe
        if time.time()-self.last_profile>60 or self.contract is None:
            self.status='waiting_for_reviewed_gold_contract'
            self.contract=Contract(**(await client.research_contract(cfg.symbol)))
            self.status='collecting_gold_history'
            self.last_profile=time.time()
        await self.sync_feedback(client)
        # Bootstrap history a page per cycle. Merge by UTC open time; never train on a forming bar.
        offset=int(self.store.meta('cfd_history_offset:'+stream) or 1)
        if offset<=60000 and cfg.mode!='live':
            try:
                page=(await client.history(cfg.symbol,cfg.timeframe,2000,offset))['candles']
                from truetrade.worker.strategy import closed_candles
                from types import SimpleNamespace
                closed_candles(page,SimpleNamespace(bars=2000,timeframe=cfg.timeframe),time.time())
                self.store.capture(stream,page)
                self.store.set_meta('cfd_history_offset:'+stream,offset+2000)
            except Exception:
                # Recent valid bars still accumulate; unavailable broker history is visible.
                self.status='historical_backfill_unavailable'
        if self.process and self.process.returncode is not None:
            if self.joblog:self.joblog.close();self.joblog=None
            success=self.process.returncode==0;self.process=None
            if not success:self.status='training_failed_inspect_training_log'
        # Recover a completely saved candidate even if the worker restarted after training.
        # A failed/partial job has no qualifying manifest and cannot replace active weights.
        candidate=self.root/'latest-candidate.json'
        if cfg.mode=='demo' and candidate.exists():
            record=json.loads(candidate.read_text())
            fingerprint=record['manifest_sha256']
            if self.store.meta('cfd_last_candidate')!=fingerprint:
                if record['eligible_demo']:
                    _,_,meta,_=load_release(candidate,mode='demo')
                    current_created=load_release(self.registry)[2]['created_at'] if self.registry.exists() else 0
                    if self.compatible(meta,seconds) and meta['created_at']>current_created:
                        activate(self.root/record['model_dir'],self.registry)
                        self.status='qualified_candidate_activated_for_demo'
                else:self.status='candidate_failed_validation'
                self.store.set_meta('cfd_last_candidate',fingerprint)
        self.policy=None
        if self.registry.exists():
            model,norm,meta,record=load_release(self.registry,mode=cfg.mode)
            if self.compatible(meta,seconds):
                # Freeze learned weights during execution; loading never updates them.
                self.policy=(model,norm,meta)
                self.status='qualified_model_loaded'
            else:
                # Halt entries, but continue collecting data and training a new candidate.
                self.status='model_contract_changed_requalification_required'
        all_rows=self.store.bars(stream)
        last_trained=int(self.store.meta('cfd_last_training_end:'+stream) or 0)
        new_bars=sum(r['time']>last_trained for r in all_rows)
        enabled=os.getenv('CFD_AUTO_TRAIN','true').lower()=='true'
        if enabled and cfg.mode=='demo' and not self.process and len(all_rows)>=50000 and new_bars>=5000:
            span=(all_rows[-1]['time']-all_rows[0]['time'])/86400
            if span>=180:
                contract,feedback=calibrate(self.contract,self.store.feedback())
                dataset=self.root/('dataset-'+str(all_rows[-1]['time'])+'.json')
                atomic_json(dataset,{'rows':all_rows,'seconds':seconds,'contract':contract.json(),
                                     'captured_at':time.time(),'feedback_calibration':feedback})
                self.store.set_meta('cfd_last_training_end:'+stream,all_rows[-1]['time'])
                self.joblog=(self.root/'training.log').open('ab')
                self.process=await asyncio.create_subprocess_exec(sys.executable,'-m','truetrade.cfd.pipeline',
                    str(dataset),str(self.root),'--episodes',os.getenv('CFD_TRAIN_EPISODES','2000'),
                    stdout=self.joblog,stderr=self.joblog)
                self.status='training_candidate_in_separate_process'
        if self.policy is None and not self.process and self.status in {'waiting_for_market_data','collecting_gold_history'}:
            self.status='waiting_for_50000_bars_and_180_days'
        self.store.set_meta('cfd_learning_status',self.status)
        return self.policy is not None

    def choose(self,rows):
        if not self.policy:raise ValueError('No qualified policy')
        model,norm,meta=self.policy
        x,atr=observation(rows,meta['contract']['symbol']['point'])
        action,logp,value,probs=model.choose(norm.transform(x),deterministic=True)
        return (None if action==0 else 'LONG' if action==1 else 'SHORT'),atr,meta['sha256']

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:await asyncio.wait_for(self.process.wait(),10)
            except TimeoutError:self.process.kill();await self.process.wait()
        if self.joblog:self.joblog.close()
