from dataclasses import asdict
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
import numpy as np
from helpers import candles,market
from truetrade.exchange.data import save_dataset,load_dataset
from truetrade.exchange.collector import collect,align_funding
from truetrade.rl.trainer import train_dataset,RetrainSchedule,activate_candidate
from truetrade.rl.ppo import PPO
from truetrade.main import Worker
from truetrade.config import Settings


class PipelineTests(unittest.TestCase):
    def test_training_walkforward_and_final_holdout_end_to_end(self):
        c = candles(2400)
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp)/"dataset"
            save_dataset(data,c,np.zeros(len(c.close)),market(),"synthetic_test",{"purpose":"software_test_only"})
            path,m=train_dataset(data,Path(tmp)/"models",episodes=1)
            self.assertEqual(len(m["walk_forward"]),3)
            self.assertEqual(m["training"]["episodes"],1)
            self.assertFalse(m["promotion"]["eligible_for_demo_review"])
            loaded,norm,_=PPO.load(path)
            self.assertEqual(len(norm.mean),18)
            with self.assertRaises(ValueError): activate_candidate(path,Path(tmp)/"champion.json")
            self.assertTrue(m["holdout_start"] > m["walk_forward"][-1]["split"]["train_end"])

    def test_funding_alignment_requires_unique_events(self):
        c=candles(100)
        timestamp=c.timestamp[80]+60
        rates=align_funding(c,[{"timestamp":timestamp,"rate":.001}],60)
        self.assertEqual(rates[80],.001)
        with self.assertRaises(ValueError): align_funding(c,[{"timestamp":timestamp,"rate":.001}]*2,60)

    def test_retraining_requires_new_data_and_trigger(self):
        schedule=RetrainSchedule(every_seconds=60,every_trades=2)
        self.assertFalse(schedule.due("a",now=schedule.last+1))
        schedule.record_closed_trade(); schedule.record_closed_trade()
        self.assertTrue(schedule.due("a"))
        schedule.completed("a")
        self.assertFalse(schedule.due("a",now=schedule.last+100))
        self.assertTrue(schedule.due("b",now=schedule.last+100))


class AsyncPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_research_worker_checks_connection_when_keys_are_added(self):
        from unittest.mock import AsyncMock, patch
        with tempfile.TemporaryDirectory() as tmp:
            settings=Settings(api_key="TEST_ONLY",api_secret="TEST_ONLY",state_dir=tmp)
            worker=Worker(settings)
            try:
                with patch("scripts.check_connection.check",new_callable=AsyncMock) as check:
                    check.return_value={"profile":"ok","futures_markets":"ok","exchange_writes_enabled":False}
                    result=await worker.run(once=True)
                    check.assert_awaited_once_with(settings)
                    self.assertEqual(result["trading"],"blocked")
                    self.assertEqual(result["connection"]["profile"],"ok")
            finally: worker.journal.close()

    async def test_collector_with_explicit_fixture_contract(self):
        c=candles(600)
        m=market()
        rows=[{**asdict(m),"active":True,"linear":True}]
        for k,v in list(rows[0].items()):
            if k not in {"symbol","max_leverage","active","linear"}: rows[0][k]=str(v)
        stats=[{"symbol":m.symbol,"quote_volume":100000.,"volatility":.02,"bid":100.,"ask":100.01}]
        class FixtureClient:
            async def markets(self): return rows
            async def stats(self): return stats
            async def history(self,**params):
                mask=(c.timestamp>=params["from"])&(c.timestamp<params["to"])
                return {"s":"ok",**{short:getattr(c,long)[mask].tolist() for short,long in {
                    "t":"timestamp","o":"open","h":"high","l":"low","c":"close","v":"volume"}.items()}}
            async def funding(self,**params): return []
        fields=list(rows[0])
        config={"reviewed_against_exchange":True,"evidence_reference":"UNIT_TEST_FIXTURE_ONLY",
                "funding_range_complete_without_pagination":True,"resolution_seconds":60,"resolution_value":"1",
                "markets":{"envelope":"","fields":{k:k for k in fields}},
                "stats":{"envelope":"","fields":{k:k for k in stats[0]}},
                "funding":{"envelope":"","fields":{"timestamp":"timestamp","rate":"rate"}},
                "history_params":{"symbol":"symbol","start":"from","end":"to","resolution":"resolution"},
                "funding_params":{"symbol":"symbol","start":"from","end":"to"},"bars_per_request":200}
        with tempfile.TemporaryDirectory() as tmp:
            config_path=Path(tmp)/"mapping.json";config_path.write_text(json.dumps(config))
            paths=await collect(FixtureClient(),config_path,tmp,int(c.timestamp[0]),int(c.timestamp[-1])+60)
            loaded,_,_,_=load_dataset(paths[0])
            np.testing.assert_allclose(loaded.close,c.close)

    async def test_health_is_distinct_from_trading_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker=Worker(Settings(state_dir=tmp))
            server=await asyncio.start_server(worker.handle_http,"127.0.0.1",0)
            port=server.sockets[0].getsockname()[1]
            try:
                for path,expected in (("/health",b"200 OK"),("/ready",b"503 Service Unavailable")):
                    reader,writer=await asyncio.open_connection("127.0.0.1",port)
                    writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode());await writer.drain()
                    raw=await reader.read();self.assertIn(expected,raw)
                    writer.close();await writer.wait_closed()
            finally:
                server.close();await server.wait_closed();worker.journal.close()
