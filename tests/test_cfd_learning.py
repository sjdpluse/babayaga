import asyncio
from dataclasses import asdict,replace
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import numpy as np

from fake_mt5 import FakeMT5
from test_mt5 import signal
from truetrade.brokers.base import Symbol,BrokerError
from truetrade.brokers.mt5 import MT5Broker
from truetrade.brokers.mt5_config import MT5Settings
from truetrade.cfd.contract import Contract
from truetrade.cfd.observations import observation,SCHEMA,ACTIONS,WINDOW
from truetrade.cfd.environment import CFDEnv
from truetrade.cfd.evaluation import summarize,gate
from truetrade.cfd.feedback import validate_feedback,demo_evidence,calibrate,authorize_live
from truetrade.cfd.pipeline import train,load_release,activate,atomic_json,digest,validate_dataset
from truetrade.cfd.learning import Learning
from truetrade.config import RiskLimits
from truetrade.execution.agent import Agent
from truetrade.execution.engine import ExecutionEngine
from truetrade.execution.remote import SignalClient
from truetrade.features.technical import Normalizer
from truetrade.persistence.store import Journal
from truetrade.rl.ppo import PPO
from truetrade.risk.manager import RiskManager
from truetrade.worker.mt5 import MT5Worker
from truetrade.worker.store import WorkerStore
from truetrade.worker.strategy import WorkerSettings


def contract(source='synthetic_test'):
    return Contract(symbol=dict(symbol='XAUUSD',volume_min='.01',volume_max='100',volume_step='.01',
        trade_tick_size='.01',trade_tick_value='1',trade_contract_size='100',point='.01',digits=2,
        trade_stops_level=20,trade_freeze_level=10),currency='USD',value_per_price_lot=100,margin_rate=.01,
        commission=7,entry_slippage_points=20,exit_slippage_points=20,max_spread_points=50,
        swap_long_cost=10,swap_short_cost=12,rollover_utc_hour=21,triple_weekday=2,
        source=source,observed_at=time.time())


def rows(n=1000):
    end=int(time.time())//300*300
    rng=np.random.default_rng(20);prices=2000+np.cumsum(rng.normal(0,1,n))
    result=[]
    for i,p in enumerate(prices):
        o=prices[max(0,i-1)]
        result.append(dict(time=end-(n-i)*300,open=float(o),high=float(max(o,p)+1),low=float(min(o,p)-1),
                           close=float(p),volume=50.,real_volume=0,spread=20.,_seconds=300))
    return result


def policy_fixture(root,action=None):
    """Software fixture only: not genuine evaluation or broker data."""
    model=PPO(len(SCHEMA),3);model.updates=1
    if action is not None:model.parameters['bp'][action]=1000
    norm=Normalizer(np.zeros(len(SCHEMA)),np.ones(len(SCHEMA)))
    metrics=dict(trades=100,net_return=.05,max_drawdown=.03,profit_factor=1.5,expectancy_lower95_r=.1)
    meta={'algorithm':'numpy_ppo_cfd_v1','feature_schema':SCHEMA,'actions':ACTIONS,'window':WINDOW,
          'canonical_symbol':'XAUUSD','seconds':300,'contract':contract('mt5_demo').json(),
          'risk_fraction':.005,'eligible_demo':True,'train_end':10,'holdout_start':20,'holdout_end':30,
          'created_at':40,'history_bars':50000,'history_days':180,'training':{'episodes':2000,'steps':50000},
          'stop_atr':2.,'target_atr':4.,'holdout':metrics,'stress':metrics,
          'walk_forward':[dict(train_end=1,validation_start=2,validation_end=3,metrics=metrics)]*3}
    target=Path(root)/'model-fixture';model.save(target,norm,meta)
    registry=Path(root)/'active.json';activate(target,registry)
    return model,norm,target,registry


class CFDTests(unittest.TestCase):
    def test_observation_is_fixed_window_causal_and_finite(self):
        r=rows();x,atr=observation(r[:256],.01)
        self.assertEqual(x.shape,(len(SCHEMA),));self.assertGreater(atr,0)
        r[-1]['close']*=2
        np.testing.assert_array_equal(x,observation(r[:256],.01)[0])
        with self.assertRaises(ValueError):observation(r[:255],.01)

    def test_currency_and_unknown_contract_fail_safely(self):
        with self.assertRaises(ValueError):replace(contract(),currency='EUR')
        with self.assertRaises(ValueError):replace(contract(),commission=float('nan'))
        self.assertFalse(contract().compatible(replace(contract(),commission=20)))

    def test_simulated_stop_spread_fees_and_lot_risk(self):
        r=rows(600);x=np.zeros((600,len(SCHEMA)));atr=np.ones(600)*2
        for b in r:b.update(open=2000.,high=2001.,low=1999.,close=2000.)
        r[257]['low']=1900.
        env=CFDEnv(r,contract(),x,atr,Normalizer(np.zeros(len(SCHEMA)),np.ones(len(SCHEMA))),
                   256,599,random_start=False)
        env.step(1);trade=env.closed[0]
        self.assertEqual(trade['reason'],'stop')
        self.assertLess(trade['net_pnl'],0);self.assertLessEqual(trade['risk'],50)
        self.assertGreater(trade['entry'],2000.2)
        self.assertGreater(trade['fees'],0)

    def test_short_trigger_uses_ask_and_gap_stop_is_not_guaranteed(self):
        r=rows(600);x=np.zeros((600,len(SCHEMA)));atr=np.ones(600)*2
        for b in r:b.update(open=2000.,high=2001.,low=1999.,close=2000.)
        r[258].update(open=2100.,high=2101.,low=2099.,close=2100.)
        env=CFDEnv(r,contract(),x,atr,Normalizer(np.zeros(len(SCHEMA)),np.ones(len(SCHEMA))),256,599,random_start=False)
        env.step(2)
        self.assertEqual(env.closed[0]['reason'],'gap_stop')
        self.assertLess(env.closed[0]['net_pnl'],-env.closed[0]['risk'])

    def test_no_entry_across_weekend_gap(self):
        r=rows(600);r[257]['time']+=2*86400
        env=CFDEnv(r,contract(),np.zeros((600,len(SCHEMA))),np.ones(600)*2,
                   Normalizer(np.zeros(len(SCHEMA)),np.ones(len(SCHEMA))),256,599,random_start=False)
        env.step(1);self.assertEqual(env.closed,[])

    def test_swap_calendar_and_triple_day(self):
        from datetime import datetime,timezone
        r=rows(600);env=CFDEnv(r,contract(),np.zeros((600,len(SCHEMA))),np.ones(600),
                   Normalizer(np.zeros(len(SCHEMA)),np.ones(len(SCHEMA))),256,599)
        wed=datetime(2026,9,16,21,tzinfo=timezone.utc).timestamp()
        self.assertEqual(env.carry(wed-1,wed+1,'LONG',.1),3.)

    def test_positive_win_rate_alone_does_not_qualify(self):
        trades=[dict(net_pnl=1,r_multiple=.1)]*90+[dict(net_pnl=-20,r_multiple=-2)]*10
        report=summarize([10000,9890],trades)
        self.assertEqual(report['win_rate'],.9);self.assertTrue(gate(report))

    def test_untrained_or_tampered_model_not_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            _,_,target,registry=policy_fixture(tmp)
            (target/'weights.npz').write_bytes(b'corrupt')
            with self.assertRaises(ValueError):load_release(registry)

    def test_same_weights_and_predictions_demo_and_live_with_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            _,_,target,registry=policy_fixture(tmp)
            with self.assertRaises(ValueError):load_release(registry,mode='live')
            rec=json.loads(registry.read_text())
            rec['live_evidence']={'model_sha256':rec['weights_sha256'],'eligible':True,'days':31,
                'metrics':json.loads((target/'manifest.json').read_text())['holdout'],
                'decision_ids':[str(i) for i in range(100)]}
            atomic_json(registry,rec)
            demo,norm,md,_=load_release(registry,mode='demo');live,nl,ml,_=load_release(registry,mode='live')
            x=observation(rows(256),.01)[0]
            self.assertEqual(md['sha256'],ml['sha256'])
            np.testing.assert_array_equal(demo.forward(norm.transform(x))[0],live.forward(nl.transform(x))[0])

    def test_contract_mismatch_never_silently_reuses_crypto(self):
        with tempfile.TemporaryDirectory() as tmp:
            _,_,target,registry=policy_fixture(tmp)
            m=json.loads((target/'manifest.json').read_text());m['algorithm']='numpy_ppo_v1'
            atomic_json(target/'manifest.json',m)
            rec=json.loads(registry.read_text());rec['manifest_sha256']=digest(target/'manifest.json');atomic_json(registry,rec)
            with self.assertRaises(ValueError):load_release(registry)

    def test_actual_ppo_training_fixture_not_promoted_and_holdout_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);r=rows(1000);doc={'rows':r,'seconds':300,'contract':contract().json(),'captured_at':time.time()}
            dataset=root/'data.json';atomic_json(dataset,doc)
            # Fast cache fixture tests optimizer/splits/gates, while observation causality is tested separately.
            x=np.random.default_rng(1).normal(size=(1000,len(SCHEMA)));atr=np.ones(1000)*2
            with patch('truetrade.cfd.pipeline.cache',return_value=(x,atr)):
                target,meta=train(dataset,root/'models',episodes=1,test_only=True)
                self.assertGreater(meta['optimizer_updates'],0)
                self.assertFalse(meta['eligible_demo'])
                self.assertLess(meta['train_end'],meta['holdout_start'])
                loaded,norm,_=PPO.load(target)
                end=next(i for i,b in enumerate(r) if b['time']==meta['train_end'])+1
                np.testing.assert_allclose(norm.mean,x[WINDOW-1:end].mean(axis=0))
                with self.assertRaises(ValueError):activate(target,root/'models'/'active.json')
                with self.assertRaises(ValueError):train(dataset,root/'models',episodes=1,test_only=True)

    def test_no_demo_evidence_means_no_live_release(self):
        self.assertFalse(demo_evidence([],'x')['eligible'])
        with tempfile.TemporaryDirectory() as tmp:
            _,_,target,registry=policy_fixture(tmp);store=WorkerStore(Path(tmp)/'worker.sqlite')
            try:
                with self.assertRaises(ValueError):authorize_live(registry,store.path)
            finally:store.close()

    def test_dataset_rejects_forming_candle_and_synthetic_production_training(self):
        r=rows(600);doc={'rows':r,'seconds':300,'contract':contract().json(),'captured_at':r[-1]['time']}
        with self.assertRaises(ValueError):validate_dataset(doc)
        with tempfile.TemporaryDirectory() as tmp:
            doc['captured_at']=time.time();p=Path(tmp)/'data.json';atomic_json(p,doc)
            with self.assertRaises(ValueError):train(p,Path(tmp)/'models',episodes=1)

    def test_boolean_eligibility_cannot_override_failed_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            _,_,target,registry=policy_fixture(tmp)
            meta=json.loads((target/'manifest.json').read_text());meta['stress']['net_return']=-.1
            atomic_json(target/'manifest.json',meta)
            record=json.loads(registry.read_text());record['manifest_sha256']=digest(target/'manifest.json')
            atomic_json(registry,record)
            with self.assertRaises(ValueError):load_release(registry)
            self.assertTrue(gate(dict(trades=100,net_return=float('nan'),max_drawdown=0,
                                     profit_factor=2,expectancy_lower95_r=1)))

    def test_live_boolean_without_trade_evidence_cannot_override_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            _,_,_,registry=policy_fixture(tmp);rec=json.loads(registry.read_text())
            rec['live_evidence']={'model_sha256':rec['weights_sha256'],'eligible':True}
            atomic_json(registry,rec)
            with self.assertRaises(ValueError):load_release(registry,mode='live')

    def test_forward_evidence_promotes_exact_weights_and_rejects_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            _,_,target,registry=policy_fixture(tmp)
            sha=json.loads((target/'manifest.json').read_text())['sha256']
            fixtures=[]
            store=WorkerStore(Path(tmp)/'worker.sqlite')
            try:
                for i in range(100):
                    pnl=-2 if i%10==0 else 4;opened=1000000+i*8*3600
                    f={'decision_id':str(i),'signal':{'model_sha256':sha},'plan':{'risk':5,'equity':10000},
                       'outcome':dict(net_pnl=pnl,profit=pnl,commission=0,swap=0,fee=0,volume=.01,
                                      entry=2000,opened_at=opened,closed_at=opened+60,mode='demo',
                                      currency='USD',deal_ids=[2*i+1,2*i+2])}
                    fixtures.append(f);store.save_feedback(str(i),sha,f)
                path=authorize_live(registry,store.path)
                self.assertEqual(load_release(path,mode='live')[2]['sha256'],sha)
                with self.assertRaises(ValueError):demo_evidence(fixtures+[fixtures[-1]],sha)
            finally:store.close()


class RealContractMockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.journal=Journal(self.root/'agent.sqlite')
        self.api=FakeMT5();self.api.info.currency_base='XAU';self.api.info.currency_profit='USD'
        self.settings=MT5Settings(login=12345,password='test-only',server='Fixture-Demo',terminal_path='fixture',
                                  mode='demo',commission_per_lot=Decimal(7))
        self.broker=MT5Broker(self.settings,self.journal,self.api);await self.broker.connect()
        self.engine=ExecutionEngine(self.broker,RiskManager(),self.journal)
        self.agent=Agent(self.engine,'t'*48,{'XAUUSD'})
        self.server=await asyncio.start_server(self.agent.handle,'127.0.0.1',0)
        self.client=SignalClient('http://127.0.0.1:'+str(self.server.sockets[0].getsockname()[1]),'t'*48)
        self.store=WorkerStore(self.root/'worker.sqlite')

    async def asyncTearDown(self):
        self.server.close();await self.server.wait_closed();self.store.close();self.journal.close();self.tmp.cleanup()

    async def test_history_ticket_parameter_is_deal_not_order(self):
        sig=signal();await self.engine.submit(sig)
        receipt=next(iter(self.api.deals.values()))
        self.assertEqual(len(self.api.history_deals_get(ticket=receipt.order)),0)
        self.assertEqual(len(self.api.history_deals_get(ticket=receipt.ticket)),1)
        self.assertEqual(len(self.api.history_orders_get(ticket=receipt.order)),1)
        self.assertEqual(len(self.api.history_deals_get(position=receipt.position_id)),1)

    async def test_research_contract_needs_reviewed_costs(self):
        with patch.dict('os.environ',{},clear=True):
            with self.assertRaises(BrokerError):await self.broker.research_contract('XAUUSD')
        values={'MT5_RESEARCH_SWAP_LONG_PER_LOT_DAY':'10','MT5_RESEARCH_SWAP_SHORT_PER_LOT_DAY':'12',
                'MT5_RESEARCH_ROLLOVER_UTC_HOUR':'21','MT5_RESEARCH_TRIPLE_WEEKDAY':'2'}
        with patch.dict('os.environ',values):
            result=Contract(**(await self.client.research_contract('XAUUSD')))
        self.assertEqual(result.value_per_price_lot,100)
        self.assertEqual(result.source,'mt5_demo');self.assertEqual(self.api.requests,[])

    async def test_real_closed_deal_feedback_net_costs_and_model_link(self):
        sig=replace(signal(),model_sha256='a'*64)
        result=await self.engine.submit(sig);await self.broker.close(result['position_id']);await self.engine.reconcile()
        for i,d in enumerate(self.api.deals.values()):
            d.profit=0 if i==0 else 10;d.commission=-.5;d.swap=0;d.fee=-.1
            d.time_msc=int((time.time()-3600+i*3000)*1000)
        f=validate_feedback(await self.client.outcome(sig.decision_id),sig.decision_id)
        self.assertAlmostEqual(float(f['outcome']['net_pnl']),8.8)
        self.assertEqual(f['signal']['model_sha256'],'a'*64)
        self.store.save_feedback(sig.decision_id,'a'*64,f);self.store.save_feedback(sig.decision_id,'a'*64,f)
        self.assertEqual(len(self.store.feedback()),1)
        c,meta=calibrate(contract(),[f]);self.assertGreater(c.commission,7)
        self.assertFalse(demo_evidence([f],'a'*64)['eligible'])

    async def test_missing_deal_money_is_not_zero_reward(self):
        sig=signal();r=await self.engine.submit(sig);await self.broker.close(r['position_id'])
        with self.assertRaises(AttributeError):await self.broker.closed_outcome(r['position_id'])

    async def test_worker_no_model_does_not_fall_back_to_breakout(self):
        cfg=WorkerSettings(mode='demo',max_bar_age=300)
        self.api.TIMEFRAME_M5=5
        data=rows(256)
        self.api.copy_rates_from_pos=lambda *args:[dict(b,tick_volume=int(b['volume'])) for b in data]
        worker=MT5Worker(self.store,cfg,self.client)
        with patch.object(Learning,'service',return_value=False):
            await worker.cycle()
        self.assertEqual(worker.state['reason'],'no_qualified_ppo_model');self.assertEqual(self.api.requests,[])

    async def test_live_ppo_requires_flag_and_qualified_model(self):
        with self.assertRaises(ValueError):WorkerSettings(mode='live')
        cfg=WorkerSettings(mode='live',allow_live=True)
        self.assertEqual(cfg.strategy,'ppo_cfd')
        with self.assertRaises(ValueError):WorkerSettings(mode='live',allow_live=True,strategy='breakout_demo')

    async def prepared_ppo_worker(self):
        _,_,target,registry=policy_fixture(self.root/'cfd',action=1)
        self.api.TIMEFRAME_M5=5;data=rows(256)
        self.api.copy_rates_from_pos=lambda *args:[dict(b,tick_volume=int(b['volume'])) for b in data]
        self.api.order_calc_margin=lambda action,symbol,volume,price:volume*price*.01
        worker=MT5Worker(self.store,WorkerSettings(mode='demo',max_bar_age=300),self.client)
        worker.learning=Learning(self.store,worker.settings,self.root/'cfd')
        worker.learning.contract=contract('mt5_demo');worker.learning.last_profile=time.time()
        self.store.set_meta('cfd_history_offset:XAUUSD:M5',60001)
        return worker,target,registry

    async def test_ppo_to_lot_fill_protection_journals_and_duplicate(self):
        worker,target,registry=await self.prepared_ppo_worker()
        await worker.cycle()
        self.assertEqual(worker.state['reason'],'signal_processed')
        self.assertEqual(len(self.api.requests),1)
        request=self.api.requests[0];self.assertEqual(request['type'],self.api.ORDER_TYPE_BUY)
        self.assertGreaterEqual(request['volume'],.01)
        observed=next(iter(self.api.positions.values()))
        self.assertLess(request['sl'],observed.price_open);self.assertGreater(request['tp'],observed.price_open)
        self.assertEqual(observed.sl,request['sl']);self.assertEqual(observed.tp,request['tp'])
        self.assertEqual(self.store.last_decision()['state'],'protected')
        self.assertEqual(worker.state['model_sha256'],json.loads((target/'manifest.json').read_text())['sha256'])
        await worker.cycle();self.assertEqual(len(self.api.requests),1)

    async def test_ppo_uncertain_fill_halts_without_retry(self):
        worker,_,_=await self.prepared_ppo_worker();self.api.lose_response=True
        await worker.cycle();self.assertEqual(self.store.last_decision()['state'],'unknown')
        self.assertTrue(self.engine.status()['halted'])
        await worker.cycle();self.assertEqual(len(self.api.requests),1)

    async def test_completed_candidate_recovers_after_worker_restart(self):
        worker,target,registry=await self.prepared_ppo_worker()
        rec=json.loads(registry.read_text());rec['eligible_demo']=True
        atomic_json(target.parent/'latest-candidate.json',rec);registry.unlink()
        await worker.cycle()
        self.assertTrue(registry.exists());self.assertEqual(len(self.api.requests),1)

    async def test_changed_contract_blocks_orders_but_keeps_learning(self):
        worker,_,_=await self.prepared_ppo_worker()
        worker.learning.contract=replace(worker.learning.contract,commission=20)
        await worker.cycle()
        self.assertEqual(worker.state['reason'],'no_qualified_ppo_model')
        self.assertEqual(worker.learning.status,'model_contract_changed_requalification_required')
        self.assertEqual(self.api.requests,[])
