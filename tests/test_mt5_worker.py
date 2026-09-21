import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from decimal import Decimal
from unittest.mock import patch, AsyncMock

from fake_mt5 import FakeMT5
from truetrade.brokers.base import OrderRejected, OrderUncertain
from truetrade.brokers.mt5 import MT5Broker
from truetrade.brokers.mt5_config import MT5Settings
from truetrade.execution.agent import Agent
from truetrade.execution.engine import ExecutionEngine
from truetrade.execution.remote import SignalClient
from truetrade.persistence.store import Journal
from truetrade.risk.manager import RiskManager, decimal as D
from truetrade.worker.mt5 import MT5Worker, serve
from truetrade.worker.store import WorkerStore
from truetrade.worker.strategy import WorkerSettings, closed_candles, choose


class WorkerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.journal = Journal(self.path/'agent.sqlite')
        self.api = FakeMT5()
        self.settings = MT5Settings(login=12345, password='test-only', server='Fixture-Demo',
                                    terminal_path='fixture', mode='demo', commission_per_lot=D('7'))
        self.broker = MT5Broker(self.settings, self.journal, self.api)
        await self.broker.connect()
        self.engine = ExecutionEngine(self.broker, RiskManager(), self.journal)
        self.agent = Agent(self.engine, 't'*48, {'XAUUSD'})
        self.server = await asyncio.start_server(self.agent.handle, '127.0.0.1', 0)
        port = self.server.sockets[0].getsockname()[1]
        self.client = SignalClient(f'http://127.0.0.1:{port}', 't'*48)
        self.store = WorkerStore(self.path/'worker.sqlite')
        self.cfg = WorkerSettings(mode='demo', strategy='breakout_demo', timeframe='M1', bars=100)
        self.worker = MT5Worker(self.store, self.cfg, self.client)
        boundary = int(time.time())//60*60
        self.rows = [dict(time=boundary-(100-i)*60, open=1995., high=1999., low=1990., close=1995.,
                          tick_volume=10, real_volume=0, spread=20) for i in range(100)]
        self.rows[-1].update(high=2001., close=2000.2)
        self.api.copy_rates_from_pos = lambda *args: self.rows

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        self.store.close()
        self.journal.close()
        self.tmp.cleanup()

    async def test_closed_bars_to_real_strategy_to_http_to_mock_mt5_to_both_journals(self):
        await self.worker.cycle()
        self.assertEqual(self.worker.state['reason'], 'signal_processed')
        self.assertEqual(len(self.api.requests), 1)
        request = self.api.requests[0]
        self.assertEqual(request['type'], self.api.ORDER_TYPE_BUY)
        self.assertGreater(request['volume'], 0)
        self.assertLess(request['sl'], self.api.bid)
        self.assertGreater(request['tp'], self.api.ask)
        row = self.store.db.execute('SELECT id,state,payload FROM worker_decisions').fetchone()
        self.assertEqual(row[1], 'protected')
        payload = json.loads(row[2])
        self.assertTrue(payload['require_flat'])
        self.assertEqual(payload['expected_mode'], 'demo')
        self.assertEqual(self.engine.status(row[0])['state'], 'protected')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM worker_bars').fetchone()[0], 100)
        self.assertEqual(self.journal.db.execute("SELECT count(*) FROM events WHERE table_name='trades'").fetchone()[0], 1)

    async def test_restart_same_bar_no_duplicate(self):
        await self.worker.cycle()
        self.store.close()
        self.store = WorkerStore(self.path/'worker.sqlite')
        restarted = MT5Worker(self.store, self.cfg, self.client)
        await restarted.cycle()
        self.assertEqual(len(self.api.requests), 1)
        self.assertEqual(restarted.state['reason'], 'waiting_for_next_closed_bar')

    async def test_simultaneous_cycles_send_once(self):
        await asyncio.gather(self.worker.cycle(), self.worker.cycle())
        self.assertEqual(len(self.api.requests), 1)

    async def test_no_blind_retry_after_terminal_lost_result_and_restart(self):
        self.api.lose_response = True
        await self.worker.cycle()
        self.assertEqual(self.store.last_decision()['state'], 'unknown')
        for _ in range(2):
            await MT5Worker(self.store, self.cfg, self.client).cycle()
        self.assertEqual(len(self.api.requests), 1)
        self.assertTrue(self.engine.status()['halted'])

    async def test_http_lost_after_protected_fill_queries_status_without_resend(self):
        submit = self.client.submit
        async def lost(sig):
            await submit(sig)
            raise OrderUncertain()
        with patch.object(self.client, 'submit', side_effect=lost):
            await self.worker.cycle()
        self.assertEqual(self.store.last_decision()['state'], 'unknown')
        await self.worker.cycle()
        self.assertEqual(self.store.last_decision()['state'], 'protected')
        self.assertEqual(len(self.api.requests), 1)

    async def test_crash_after_durable_record_before_post_never_resends(self):
        with patch.object(self.client, 'submit', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await self.worker.tick()
        await self.worker.cycle()
        self.assertEqual(self.worker.state['reason'], 'uncertain_signal_manual_reconciliation_required')
        self.assertEqual(self.api.requests, [])
        self.assertEqual(self.store.last_decision()['state'], 'unknown')

    async def test_401_is_known_rejection_and_never_replayed(self):
        original = self.client.submit
        async def wrong_token(sig):
            self.client.token = 'x'*48
            try:
                return await original(sig)
            finally:
                self.client.token = 't'*48
        with patch.object(self.client, 'submit', side_effect=wrong_token):
            await self.worker.cycle()
        self.assertEqual(self.store.last_decision()['state'], 'rejected')
        await self.worker.cycle()
        self.assertEqual(self.api.requests, [])

    async def test_order_rejection_no_later_retry_of_bar(self):
        self.api.account_value.margin_free = 0
        await self.worker.cycle()
        self.assertEqual(self.store.last_decision()['state'], 'rejected')
        self.api.account_value.margin_free = 10000
        await self.worker.cycle()
        self.assertEqual(self.api.requests, [])

    async def test_live_agent_blocked_even_if_agent_live_enabled(self):
        self.broker.settings = replace(self.settings, mode='live', allow_live=True)
        self.api.account_value.trade_mode = 2
        await self.worker.cycle()
        self.assertEqual(self.worker.state['reason'], 'agent_mode_mismatch_or_live_blocked')
        self.assertEqual(self.api.requests, [])

    async def test_signal_mode_checked_on_server_after_snapshot(self):
        submit = self.client.submit
        async def switched(sig):
            self.broker.settings = replace(self.settings, mode='live', allow_live=True)
            self.api.account_value.trade_mode = 2
            return await submit(sig)
        with patch.object(self.client, 'submit', side_effect=switched):
            await self.worker.cycle()
        self.assertEqual(self.api.requests, [])
        self.assertEqual(self.store.last_decision()['state'], 'rejected')

    async def test_agent_state_reset_blocks_existing_worker(self):
        self.rows[-1]['close'] = 1995.
        await self.worker.cycle()
        self.journal.set_meta('agent_state_id', 'different-journal')
        await self.worker.cycle()
        self.assertEqual(self.worker.state['reason'], 'agent_identity_or_journal_changed')
        self.assertEqual(self.api.requests, [])

    async def test_journal_switch_between_snapshot_and_post_rejected_by_agent(self):
        submit = self.client.submit
        async def switched(sig):
            self.journal.set_meta('agent_state_id', 'changed-before-post')
            return await submit(sig)
        with patch.object(self.client, 'submit', side_effect=switched):
            await self.worker.cycle()
        self.assertEqual(self.store.last_decision()['state'], 'rejected')
        self.assertEqual(self.api.requests, [])

    async def test_paper_worker_runs_without_any_terminal_order(self):
        from truetrade.brokers.mt5_paper import MT5PaperBroker
        self.agent.engine = ExecutionEngine(MT5PaperBroker(self.broker), RiskManager(), self.journal)
        worker = MT5Worker(self.store, replace(self.cfg, mode='paper'), self.client)
        await worker.cycle()
        self.assertEqual(self.store.last_decision()['state'], 'protected')
        self.assertEqual(self.api.requests, [])

    async def test_invalid_secret_configuration_not_echoed(self):
        worker = MT5Worker(self.store)
        with patch.dict(os.environ, {'MT5_AGENT_URL':'https://user:secret-password@example.invalid',
                                     'MT5_AGENT_TOKEN':'secret-token'*5}, clear=True):
            await worker.cycle()
        self.assertFalse(worker.state['ready'])
        self.assertNotIn('secret-', json.dumps(worker.snapshot()))

    async def test_existing_position_flat_check_is_atomic_server_side(self):
        from test_mt5 import signal
        submit = self.client.submit
        async def race(sig):
            await self.engine.submit(signal('other-entry'))
            return await submit(sig)
        with patch.object(self.client, 'submit', side_effect=race):
            await self.worker.cycle()
        self.assertEqual(len(self.api.requests), 1)
        self.assertEqual(self.store.last_decision()['state'], 'rejected')

    async def test_new_bar_open_position_skipped(self):
        await self.worker.cycle()
        # A different timeframe has a different stream, but still cannot stack entries.
        self.store.db.execute('DELETE FROM worker_decisions')
        self.store.db.commit()
        await self.worker.cycle()
        self.assertEqual(self.worker.state['reason'], 'position_open_waiting_for_sl_tp')
        self.assertEqual(len(self.api.requests), 1)

    async def test_hold_uses_features_without_order(self):
        self.rows[-1]['close'] = 1995.
        await self.worker.cycle()
        self.assertEqual(self.store.last_decision()['state'], 'hold')
        self.assertEqual(self.api.requests, [])

    async def test_short_breakout(self):
        self.rows[-1].update(low=1989., close=1989.5)
        await self.worker.cycle()
        self.assertEqual(self.api.requests[0]['type'], self.api.ORDER_TYPE_SELL)
        self.assertEqual(self.store.last_decision()['state'], 'protected')

    async def test_stale_candles_market_closed_no_order(self):
        for r in self.rows:
            r['time'] -= 3600
        await self.worker.cycle()
        self.assertEqual(self.worker.state['reason'], 'stale_candles_or_market_closed')
        self.assertEqual(self.api.requests, [])

    async def test_incomplete_bar_rejected(self):
        self.rows[-1]['time'] += 60
        await self.worker.cycle()
        self.assertEqual(self.worker.state['reason'], 'invalid_configuration_or_market_data')
        self.assertEqual(self.api.requests, [])

    async def test_read_failure_can_recover_without_false_halt(self):
        with patch.object(self.client, 'execution_state', side_effect=OSError('test')):
            await self.worker.cycle()
        self.assertFalse(self.worker.state['ready'])
        await self.worker.cycle()
        self.assertEqual(self.worker.state['reason'], 'signal_processed')

    async def test_missing_config_liveness_readiness_no_secrets(self):
        worker = MT5Worker(self.store)
        with patch.dict(os.environ, {}, clear=True):
            await worker.cycle()
        self.assertEqual(worker.state['reason'], 'missing_agent_url_or_token')
        server = await asyncio.start_server(worker.handle_http, '127.0.0.1', 0)
        try:
            for path, status in [('/health', '200 OK'), ('/ready', '503 Service Unavailable')]:
                reader, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
                writer.write(f'GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n'.encode())
                await writer.drain()
                text = (await reader.read()).decode()
                self.assertIn(status, text)
                self.assertNotIn('t'*48, text)
                self.assertNotIn('test-only', text)
                writer.close()
                await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()

    async def test_volume_required_no_ephemeral_fallback(self):
        with patch.dict(os.environ, {'MT5_REQUIRE_VOLUME':'true', 'STATE_DIR':str(self.path)}, clear=True):
            with self.assertRaises(ValueError):
                await serve(once=True)


class WorkerConfigTests(unittest.TestCase):
    def test_default_paper_and_live_always_rejected(self):
        self.assertEqual(WorkerSettings().mode, 'paper')
        with self.assertRaises(ValueError): WorkerSettings(mode='live')
        with patch.dict(os.environ, {'MT5_MODE':'live', 'ALLOW_LIVE_TRADING':'true'}, clear=True):
            self.assertEqual(WorkerSettings.from_env().mode, 'live')

    def test_reject_random_model_mode_invalid_risk_or_window(self):
        for kw in [dict(strategy='ppo'), dict(risk=Decimal('NaN')), dict(risk=D('.01')), dict(bars=20), dict(timeframe='M2')]:
            with self.assertRaises(ValueError): WorkerSettings(**kw)
