"""Nonblocking data collection/training controller for the existing worker."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
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
        self.metrics={}

    def compatible(self,meta,seconds):
        return (meta['canonical_symbol']==self.settings.symbol and meta['seconds']==seconds
                and meta['risk_fraction']==float(self.settings.risk)
                and Contract(**meta['contract']).compatible(self.contract))

    def _attempt_consumed(self,end_time):
        if not end_time:return False
        ledger=self.root/'research.sqlite'
        if not ledger.exists():return False
        db=sqlite3.connect(ledger)
        try:
            table=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='attempts'").fetchone()
            if not table:return False
            return db.execute('SELECT 1 FROM attempts WHERE end_time=?',(int(end_time),)).fetchone() is not None
        finally:
            db.close()

    def _release_attempt(self,end_time):
        ledger=self.root/'research.sqlite'
        if not ledger.exists():return
        db=sqlite3.connect(ledger)
        try:
            table=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='attempts'").fetchone()
            if table:
                with db:
                    db.execute('DELETE FROM attempts WHERE end_time=?',(int(end_time),))
        finally:
            db.close()

    def _safe_to_release_unseen_attempt(self,end_time):
        """Release only an interrupted reservation that cannot have evaluated holdout data.

        pipeline.py writes a flushed training_fold_completed line after every chronological
        fold and evaluates the holdout only after all three folds. A zero-byte log therefore
        proves no fold completed and the holdout could not have been evaluated.
        """
        if not end_time or not self._attempt_consumed(end_time):return False
        if self.registry.exists() or (self.root/'latest-candidate.json').exists():return False
        log=self.root/'training.log'
        if not log.exists() or log.stat().st_size!=0:return False
        if not (self.root/('dataset-'+str(int(end_time))+'.json')).exists():return False
        return not any(self.root.glob('model-*'))

    def _recover_training_markers(self,stream,candidate):
        """Recover crashes without reusing any holdout that may have been evaluated."""
        last_key='cfd_last_training_end:'+stream
        inflight_key='cfd_training_inflight_end:'+stream
        last=int(self.store.meta(last_key) or 0)
        # A live child process owns its inflight reservation. Recovery must never clear,
        # release, or reinterpret that state while the trainer is still running.
        if self.process and self.process.returncode is None:
            return last
        inflight=int(self.store.meta(inflight_key) or 0)
        if inflight:
            if self._safe_to_release_unseen_attempt(inflight):
                self._release_attempt(inflight)
                self.status='retrying_interrupted_training_before_any_fold_completed'
            elif candidate.exists() or self._attempt_consumed(inflight):
                if inflight>last:
                    self.store.set_meta(last_key,inflight);last=inflight
                self.status=('recovered_completed_training_attempt' if candidate.exists()
                             else 'interrupted_training_consumed_holdout_waiting_for_new_data')
            else:
                self.status='retrying_interrupted_training_before_holdout_reservation'
            self.store.set_meta(inflight_key,0)
        # Legacy worker wrote last_training_end before subprocess creation. If that
        # reservation never completed even one fold, it is statistically unseen and safe
        # to release. Otherwise retain it and require genuinely new holdout data.
        if last and not candidate.exists() and not self.registry.exists() and not self.process:
            if self._safe_to_release_unseen_attempt(last):
                self._release_attempt(last)
                self.store.set_meta(last_key,0);last=0
                self.status='released_legacy_unseen_training_reservation'
            elif not self._attempt_consumed(last):
                self.store.set_meta(last_key,0);last=0
                self.status='recovered_legacy_prelaunch_training_marker'
        return last

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
        offset=int(self.store.meta('cfd_history_offset:'+stream) or 1)
        if offset<=60000 and cfg.mode!='live':
            try:
                page=(await client.history(cfg.symbol,cfg.timeframe,2000,offset))['candles']
                from truetrade.worker.strategy import closed_candles
                from types import SimpleNamespace
                closed_candles(page,SimpleNamespace(bars=2000,timeframe=cfg.timeframe),time.time())
                self.store.capture(stream,page)
                self.store.set_meta('cfd_history_offset:'+stream,offset+2000)
                offset += 2000
            except Exception:
                self.status='historical_backfill_unavailable'
        candidate=self.root/'latest-candidate.json'
        inflight_key='cfd_training_inflight_end:'+stream
        if self.process and self.process.returncode is not None:
            if self.joblog:self.joblog.close();self.joblog=None
            success=self.process.returncode==0
            inflight=int(self.store.meta(inflight_key) or 0)
            if inflight:
                if success:
                    self.store.set_meta('cfd_last_training_end:'+stream,inflight)
                elif self._safe_to_release_unseen_attempt(inflight):
                    self._release_attempt(inflight)
                elif self._attempt_consumed(inflight):
                    self.store.set_meta('cfd_last_training_end:'+stream,inflight)
            self.store.set_meta(inflight_key,0)
            self.process=None
            self.status=('training_process_completed' if success else 'training_failed_inspect_training_log')
        last_trained=self._recover_training_markers(stream,candidate)
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
                self.policy=(model,norm,meta)
                self.status='qualified_model_loaded'
            else:
                self.status='model_contract_changed_requalification_required'
        all_rows=self.store.bars(stream)
        last_trained=int(self.store.meta('cfd_last_training_end:'+stream) or last_trained or 0)
        new_bars=sum(r['time']>last_trained for r in all_rows)
        span=((all_rows[-1]['time']-all_rows[0]['time'])/86400) if len(all_rows)>=2 else 0.0
        enabled=os.getenv('CFD_AUTO_TRAIN','true').lower()=='true'
        inflight=int(self.store.meta(inflight_key) or 0)
        self.metrics={
            'stream':stream,'bar_count':len(all_rows),'span_days':round(span,2),
            'history_offset':offset,'history_limit':60000,
            'new_bars_since_last_training':new_bars,'last_training_end':last_trained,
            'training_inflight_end':inflight,'auto_train_enabled':enabled,
            'training_process_running':bool(self.process and self.process.returncode is None),
            'minimum_bars':50000,'minimum_span_days':180,
        }
        if all_rows:self.metrics.update(first_bar=all_rows[0]['time'],last_bar=all_rows[-1]['time'])
        if self.process and self.process.returncode is None:
            self.status='training_candidate_in_separate_process'
        if enabled and cfg.mode=='demo' and not self.process and len(all_rows)>=50000 and new_bars>=5000:
            if span>=180:
                contract,feedback=calibrate(self.contract,self.store.feedback())
                end_time=all_rows[-1]['time']
                dataset=self.root/('dataset-'+str(end_time)+'.json')
                atomic_json(dataset,{'rows':all_rows,'seconds':seconds,'contract':contract.json(),
                                     'captured_at':time.time(),'feedback_calibration':feedback})
                self.store.set_meta(inflight_key,end_time)
                self.joblog=(self.root/'training.log').open('ab')
                try:
                    self.process=await asyncio.create_subprocess_exec(sys.executable,'-m','truetrade.cfd.pipeline',
                        str(dataset),str(self.root),'--episodes',os.getenv('CFD_TRAIN_EPISODES','2000'),
                        stdout=self.joblog,stderr=self.joblog)
                except BaseException:
                    self.store.set_meta(inflight_key,0)
                    self.joblog.close();self.joblog=None
                    raise
                self.status='training_candidate_in_separate_process'
                self.metrics['training_process_running']=True
                self.metrics['training_inflight_end']=end_time
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
