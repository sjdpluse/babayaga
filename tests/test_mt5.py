import asyncio
from dataclasses import replace, asdict
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from fake_mt5 import FakeMT5
from truetrade.brokers.base import Signal, Broker, MarketBroker, BrokerError, OrderRejected, OrderNotSubmitted, OrderUncertain
from truetrade.brokers.mt5 import MT5Broker
from truetrade.brokers.mt5_config import MT5Settings
from truetrade.brokers.mt5_paper import MT5PaperBroker
from truetrade.execution.agent import Agent, ProcessLease, tls_context
from truetrade.execution.engine import ExecutionEngine, ExecutionHalted
from truetrade.execution.remote import SignalClient
from truetrade.persistence.store import Journal
from truetrade.risk.cfd import normalize_volume, validate_volume, protection
from truetrade.risk.manager import RiskManager, RiskRejected, decimal as D


def signal(key="signal1", side="LONG"):
    now = time.time()
    return Signal(key, "XAUUSD", side, D("1990.2") if side == "LONG" else D("2010"),
                  D("2020.2") if side == "LONG" else D("1980"), D(".01"), now, now+60)


class MT5Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/"journal.sqlite"
        self.journal = Journal(self.path)
        self.api = FakeMT5()
        self.settings = MT5Settings(login=12345, password="fixture-secret", server="Fixture-Demo",
            terminal_path="fixture-terminal", mode="demo", commission_per_lot=D("7"))
        self.broker = MT5Broker(self.settings, self.journal, api=self.api)
        await self.broker.connect()
        self.engine = ExecutionEngine(self.broker, RiskManager(), self.journal)

    async def asyncTearDown(self):
        self.journal.close()
        self.tmp.cleanup()

    async def test_xauusd_end_to_end_and_duplicate(self):
        self.assertIsInstance(self.broker, Broker)
        self.assertIsInstance(self.broker, MarketBroker)
        sig = signal()
        result = await self.engine.submit(sig)
        self.assertEqual(result["result"], "protected")
        self.assertEqual(result["plan"]["size"], D(".09"))
        self.assertEqual(result["plan"]["risk"], D("94.23"))
        self.assertEqual(result["observed"]["stop"], sig.stop)
        self.assertEqual(result["observed"]["take_profit"], sig.take_profit)
        self.assertEqual(result["observed"]["entry"], D("2000.2"))
        req = self.api.requests[0]
        self.assertEqual((req["type"], req["volume"], req["sl"], req["tp"]), (0, .09, 1990.2, 2020.2))
        self.assertNotIn("price", req)
        self.assertEqual(req["type_filling"], self.api.ORDER_FILLING_FOK)
        self.assertNotEqual(int(result["position_id"]), next(iter(self.api.deals.values())).order)
        self.assertEqual(self.engine.status(sig.decision_id)["state"], "protected")
        self.assertEqual(self.journal.db.execute("SELECT count(*) FROM events WHERE table_name='trades'").fetchone()[0], 1)
        self.assertEqual((await self.engine.submit(sig))["result"], "duplicate_suppressed")
        with self.assertRaises(ValueError):
            await self.engine.submit(replace(sig, risk_fraction=D(".005")))
        self.assertEqual(len(self.api.requests), 1)

    async def test_duplicate_survives_restart(self):
        sig = signal()
        await self.engine.submit(sig)
        self.journal.close()
        self.journal = Journal(self.path)
        broker = MT5Broker(self.settings, self.journal, api=self.api)
        await broker.connect()
        engine = ExecutionEngine(broker, RiskManager(), self.journal)
        self.assertEqual((await engine.submit(sig))["result"], "duplicate_suppressed")
        self.assertEqual(len(self.api.requests), 1)

    async def test_concurrent_duplicate(self):
        sig = signal()
        results = await asyncio.gather(self.engine.submit(sig), self.engine.submit(sig))
        self.assertEqual(sorted(x["result"] for x in results), ["duplicate_suppressed", "protected"])
        self.assertEqual(len(self.api.requests), 1)

    async def test_sell_instant_execution_price(self):
        self.api.info.trade_exemode = 1
        result = await self.engine.submit(signal(side="SHORT"))
        req = self.api.requests[0]
        self.assertEqual((req["type"], req["price"], req["sl"], req["tp"]), (1, self.api.bid, 2010., 1980.))
        self.assertEqual(result["observed"]["side"], "SHORT")

    async def test_exact_symbol_preferred(self):
        self.api.names = ["XAUUSD", "XAUUSDm", "XAUUSD.a"]
        self.assertEqual(await self.broker.resolve_symbol("XAUUSD"), "XAUUSD")

    async def test_unique_suffix_ambiguity_and_explicit_mapping(self):
        self.api.names = ["XAUUSDm"]
        self.assertEqual(await self.broker.resolve_symbol("XAUUSD"), "XAUUSDm")
        self.api.names.append("XAUUSD.a")
        with self.assertRaises(OrderRejected): await self.broker.resolve_symbol("XAUUSD")
        self.broker.settings = replace(self.settings, symbol_map={"XAUUSD": "XAUUSD.a"})
        self.assertEqual(await self.broker.resolve_symbol("XAUUSD"), "XAUUSD.a")
        with self.assertRaises(OrderRejected): await self.broker.resolve_symbol("GOLD")

    async def test_volume_rounds_down_and_rejects_minimum(self):
        info = await self.broker.symbol_info("XAUUSD")
        self.assertEqual(normalize_volume(D(".019"), info), D(".01"))
        self.assertEqual(normalize_volume(D("101"), info), D("100"))
        with self.assertRaises(RiskRejected): normalize_volume(D(".009"), info)
        with self.assertRaises(OrderRejected): validate_volume(D(".015"), info)

    async def test_tick_grid_not_digits_only(self):
        self.api.info.trade_tick_size = .25
        info, q = await self.broker.symbol_info("XAUUSD"), await self.broker.quote("XAUUSD")
        self.assertEqual(protection(info, q, "LONG", D("1990.2"), D("2020.2")), (D("1990"), D("2020")))

    async def test_invalid_stops(self):
        with self.assertRaises(OrderRejected): await self.engine.submit(replace(signal(), stop=D("1999.95")))
        self.assertEqual(self.api.requests, [])

    async def test_spread_rejected(self):
        self.api.ask = self.api.bid+1
        with self.assertRaises(RiskRejected): await self.engine.submit(signal())
        self.assertEqual(self.api.requests, [])

    async def test_insufficient_margin(self):
        self.api.account_value.margin_free = 1
        with self.assertRaises(RiskRejected): await self.engine.submit(signal())
        self.assertEqual(self.api.requests, [])

    async def test_portfolio_margin_rejected(self):
        self.api.account_value.margin = 4990
        with self.assertRaises(RiskRejected): await self.engine.submit(signal())

    async def test_profit_failure_no_fallback(self):
        self.api.fail_profit = True
        with self.assertRaises(BrokerError): await self.engine.submit(signal())
        self.assertEqual(self.api.requests, [])

    async def test_margin_failure(self):
        self.api.fail_margin = True
        with self.assertRaises(BrokerError): await self.engine.submit(signal())

    async def test_missing_commission_blocks(self):
        self.broker.settings = replace(self.settings, commission_per_lot=None)
        with self.assertRaises(RiskRejected): await self.engine.submit(signal())

    async def test_check_rejection_durable_and_unsent(self):
        self.api.check_code = 10019
        sig = signal()
        with self.assertRaises(OrderRejected): await self.engine.submit(sig)
        self.assertEqual(self.engine.status(sig.decision_id)["state"], "rejected")
        self.assertFalse(self.engine.status()["halted"])
        self.assertEqual(self.api.requests, [])
        self.assertEqual((await self.engine.submit(sig))["result"], "duplicate_suppressed")

    def last_trade_event(self):
        return json.loads(self.journal.db.execute(
            "SELECT payload FROM events WHERE table_name='trades' ORDER BY rowid DESC LIMIT 1").fetchone()[0])

    async def test_check_none_is_unsent_and_new_decision_can_succeed(self):
        self.api.check_none = True
        self.api.last_error_value = (-10005, "IPC timeout fixture-secret")
        sig = signal()
        with self.assertRaises(OrderNotSubmitted): await self.engine.submit(sig)
        self.assertEqual(self.engine.status(sig.decision_id)["state"], "rejected")
        self.assertFalse(self.engine.status()["halted"])
        self.assertEqual(self.api.requests, [])
        self.assertEqual(len(self.api.checks), 1)
        diagnostic = self.last_trade_event()["broker_diagnostic"]
        self.assertEqual(diagnostic["last_error"], {"code": -10005, "message": "internal IPC timeout"})
        self.assertEqual(diagnostic["method"], "order_check")
        self.assertFalse(diagnostic["send_attempted"])
        self.assertNotIn("fixture-secret", json.dumps(self.last_trade_event()))
        self.journal.close()
        self.journal = Journal(self.path)
        self.broker.journal = self.journal
        self.engine = ExecutionEngine(self.broker, RiskManager(), self.journal)
        self.api.check_none = False
        self.assertEqual((await self.engine.submit(sig))["result"], "duplicate_suppressed")
        self.assertEqual((await self.engine.submit(signal("new")))["result"], "protected")
        self.assertEqual(len(self.api.requests), 1)

    async def test_check_throws_or_is_interrupted_before_send(self):
        for index, error in enumerate((RuntimeError("fixture-secret"), TimeoutError(), asyncio.CancelledError())):
            with self.subTest(error=type(error).__name__):
                self.api.check_error = error
                self.api.last_error_value = (-2, 'Invalid "comment" argument')
                sig = signal("check"+str(index))
                with self.assertRaises(OrderNotSubmitted): await self.engine.submit(sig)
                self.assertEqual(self.engine.status(sig.decision_id)["state"], "rejected")
                self.assertFalse(self.engine.status()["halted"])
                self.assertEqual(self.last_trade_event()["broker_diagnostic"]["last_error"]["message"],
                                 "invalid arguments/parameters: comment")
                self.assertNotIn("fixture-secret", json.dumps(self.last_trade_event()))
        self.assertEqual(self.api.requests, [])
        self.assertEqual(len(self.api.checks), 3)

    async def test_read_failure_after_intent_before_send_is_rejected(self):
        original = self.broker._prepare_open
        async def fail_after_prepare(plan):
            self.api.fail_positions = True
            return await original(plan)
        with patch.object(self.broker, "_prepare_open", side_effect=fail_after_prepare):
            with self.assertRaises(OrderNotSubmitted): await self.engine.submit(signal())
        self.assertEqual(self.engine.status("signal1")["state"], "rejected")
        self.assertFalse(self.engine.status()["halted"])
        self.assertEqual(self.api.requests, [])

    async def test_account_read_failure_after_check_is_unsent(self):
        original = self.api.order_check
        def disconnect(req):
            result = original(req)
            self.api.terminal_value.connected = False
            return result
        with patch.object(self.api, "order_check", side_effect=disconnect):
            with self.assertRaises(OrderNotSubmitted): await self.engine.submit(signal())
        self.assertEqual(self.engine.status("signal1")["state"], "rejected")
        self.assertFalse(self.engine.status()["halted"])
        self.assertEqual(self.api.requests, [])

    async def test_definite_send_rejections_are_durable_without_halt(self):
        for code in (10006, 10014, 10019, 10030):
            self.api.send_code = code
            sig = signal("rejected"+str(code))
            with self.assertRaises(OrderRejected): await self.engine.submit(sig)
            self.assertEqual(self.engine.status(sig.decision_id)["state"], "rejected")
            self.assertFalse(self.engine.status()["halted"])
            self.assertEqual(self.last_trade_event()["receipt"]["retcode"], code)
            self.assertTrue(self.last_trade_event()["broker_diagnostic"]["send_attempted"])
            self.assertEqual((await self.engine.submit(sig))["result"], "duplicate_suppressed")
        self.assertEqual(len(self.api.requests), 4)
        self.assertEqual(self.api.positions, {})

    async def test_ambiguous_send_codes_never_classified_as_rejected(self):
        for code in (10008, 10011, 10012, 10023, 10028, 10031, 99999):
            with self.subTest(code=code):
                self.api.send_code = code
                with self.assertRaises(OrderUncertain) as caught:
                    self.broker._send({"action": 1})
                self.assertTrue(caught.exception.diagnostic["send_attempted"])
        self.assertEqual(len(self.api.requests), 7)

    async def test_rejection_with_execution_evidence_or_missing_fields_is_uncertain(self):
        for result in (NS(retcode=10014, order=123, deal=0, volume=0.),
                       NS(retcode=10014, order=0, deal=0, volume=.01), NS(retcode=10014),
                       NS(retcode=10014., order=0., deal=0., volume=0.)):
            with patch.object(self.api, "order_send", return_value=result) as send:
                with self.assertRaises(OrderUncertain): self.broker._send({"action": 1})
                self.assertEqual(send.call_count, 1)

    async def test_fill_history_failure_stays_unknown_even_when_check_succeeded(self):
        with patch.object(self.api, "history_deals_get", return_value=None):
            self.api.last_error_value = (-10002, "receive failed")
            with self.assertRaises(ExecutionHalted): await self.engine.submit(signal())
        self.assertEqual(self.engine.status("signal1")["state"], "unknown")
        self.assertTrue(self.engine.status()["halted"])
        self.assertEqual(len(self.api.requests), 1)
        diagnostic = self.last_trade_event()["broker_diagnostic"]
        self.assertEqual(diagnostic["stage"], "fill_verification")
        self.assertEqual(diagnostic["method"], "history_deals_get")
        self.assertEqual(diagnostic["last_error"]["code"], -10002)
        with self.assertRaises(ExecutionHalted): await self.engine.submit(signal("next"))
        self.assertEqual(len(self.api.requests), 1)

    async def test_last_error_unavailable_cannot_change_submission_certainty(self):
        self.api.check_none = True
        with patch.object(self.api, "last_error", side_effect=RuntimeError("fixture-secret")):
            with self.assertRaises(OrderNotSubmitted): await self.engine.submit(signal())
        self.assertEqual(self.last_trade_event()["broker_diagnostic"]["last_error"]["code"], None)
        self.api.check_none = False
        self.api.lose_response = True
        with patch.object(self.api, "last_error", side_effect=RuntimeError("fixture-secret")):
            with self.assertRaises(ExecutionHalted): await self.engine.submit(signal("lost"))
        self.assertTrue(self.engine.status()["halted"])
        self.assertEqual(len(self.api.requests), 1)
        self.assertNotIn("fixture-secret", json.dumps(self.last_trade_event()))

    async def test_send_exception_types_all_remain_unknown_and_single_attempt(self):
        for error in (RuntimeError("fixture-secret"), TimeoutError(), asyncio.CancelledError()):
            with patch.object(self.api, "order_send", side_effect=error) as send:
                with self.assertRaises(OrderUncertain) as caught: self.broker._send({"action": 1})
                self.assertEqual(send.call_count, 1)
                self.assertTrue(caught.exception.diagnostic["send_attempted"])
                self.assertNotIn("fixture-secret", str(caught.exception))

    async def test_unclassified_broker_error_is_still_unknown(self):
        # The engine cannot assume every BrokerError means the adapter never sent.
        with patch.object(self.broker, "open", side_effect=BrokerError("Unclassified failure")):
            with self.assertRaises(ExecutionHalted): await self.engine.submit(signal())
        self.assertEqual(self.engine.status("signal1")["state"], "unknown")
        self.assertTrue(self.engine.status()["halted"])

    async def test_send_timeout_response_halts_without_retry(self):
        self.api.send_code = 10012
        sig = signal()
        with self.assertRaises(ExecutionHalted): await self.engine.submit(sig)
        self.assertEqual(self.engine.status(sig.decision_id)["state"], "unknown")
        for item in (sig, signal("next")):
            with self.assertRaises(ExecutionHalted): await self.engine.submit(item)
        self.assertEqual(len(self.api.requests), 1)

    async def test_malformed_check_is_unsent(self):
        for index, result in enumerate((NS(), NS(retcode="0"), NS(retcode=False))):
            with patch.object(self.api, "order_check", return_value=result):
                with self.assertRaises(OrderNotSubmitted): await self.engine.submit(signal("bad"+str(index)))
            self.assertFalse(self.engine.status()["halted"])
        self.assertEqual(self.api.requests, [])

    async def test_unknown_last_error_text_and_receipt_strings_are_redacted(self):
        self.api.last_error_value = (-98765, "password=fixture-secret Bearer token /private/path")
        self.api.check_none = True
        with self.assertRaises(OrderNotSubmitted): await self.engine.submit(signal())
        self.assertEqual(self.last_trade_event()["broker_diagnostic"]["last_error"]["code"], -98765)
        self.assertNotIn("fixture-secret", json.dumps(self.last_trade_event()))
        receipt = self.broker._receipt(NS(retcode=10014, order="fixture-secret", deal=0,
                                          volume=float("nan"), price=float("inf")))
        self.assertEqual(receipt, {"retcode": 10014, "deal": 0})

    async def test_unknown_send_blocks_restart_no_retry(self):
        self.api.lose_response = True
        sig = signal()
        with self.assertRaises(ExecutionHalted): await self.engine.submit(sig)
        self.assertEqual(len(self.api.positions), 1)
        self.assertEqual(self.engine.status(sig.decision_id)["state"], "unknown")
        self.journal.close()
        self.journal = Journal(self.path)
        broker = MT5Broker(self.settings, self.journal, api=self.api)
        await broker.connect()
        engine = ExecutionEngine(broker, RiskManager(), self.journal)
        for item in (sig, signal("different")):
            with self.assertRaises(ExecutionHalted): await engine.submit(item)
        with self.assertRaises(ExecutionHalted): await engine.reconcile()
        self.assertEqual(len(self.api.requests), 1)

    async def test_send_exception_unknown(self):
        self.api.fail_send = True
        with self.assertRaises(ExecutionHalted): await self.engine.submit(signal())
        self.assertTrue(self.engine.status()["halted"])
        self.assertEqual(len(self.api.requests), 1)

    async def test_partial_fill_close_and_latched_halt(self):
        self.api.partial = True
        with self.assertRaises(ExecutionHalted): await self.engine.submit(signal())
        self.assertEqual(self.api.positions, {})
        self.assertTrue(self.engine.status()["halted"])
        with self.assertRaises(ExecutionHalted): await self.engine.submit(signal("next"))
        self.assertEqual(len(self.api.requests), 2)
        self.assertEqual((await self.engine.reconcile())["result"], "reconciled")

    async def test_failed_protection_close_halt_until_reconcile(self):
        self.api.broken_protection = True
        with self.assertRaises(ExecutionHalted): await self.engine.submit(signal())
        self.assertEqual(self.api.positions, {})
        self.assertTrue(self.engine.status()["halted"])
        self.assertEqual(self.api.requests[-1]["type"], self.api.ORDER_TYPE_SELL)
        self.assertIn("position", self.api.requests[-1])
        await self.engine.reconcile()
        self.assertFalse(self.engine.status()["halted"])

    async def test_adverse_fill_rejected_closed(self):
        self.api.fill_slippage = 2
        with self.assertRaises(ExecutionHalted): await self.engine.submit(signal())
        self.assertEqual(self.api.positions, {})
        self.assertTrue(self.engine.status()["halted"])

    async def test_position_read_failure_is_not_empty(self):
        self.api.fail_positions = True
        with self.assertRaises(BrokerError): await self.broker.position("123")

    async def test_demo_rejects_real(self):
        self.api.account_value.trade_mode = self.api.ACCOUNT_TRADE_MODE_REAL
        with self.assertRaises(OrderRejected): await self.engine.submit(signal())
        self.assertEqual(self.api.requests, [])

    async def test_live_requires_both_flags(self):
        self.api.account_value.trade_mode = self.api.ACCOUNT_TRADE_MODE_REAL
        self.broker.settings = replace(self.settings, mode="live", allow_live=False)
        with self.assertRaises(OrderRejected): await self.engine.submit(signal())
        self.broker.settings = replace(self.settings, mode="live", allow_live=True)
        await self.broker.assert_execution_allowed()  # Mock only; no order.
        self.assertEqual(self.api.requests, [])

    async def test_paper_never_sends(self):
        self.broker.settings = replace(self.settings, mode="paper", allow_live=True)
        with self.assertRaises(OrderRejected): await self.broker.assert_execution_allowed()
        broker = MT5PaperBroker(self.broker)
        engine = ExecutionEngine(broker, RiskManager(), self.journal)
        self.assertEqual((await engine.submit(signal()))["result"], "protected")
        self.assertEqual(self.api.requests, [])

    async def test_account_switch_and_permissions(self):
        self.api.account_value.login = 999
        with self.assertRaises(OrderRejected): await self.broker.assert_execution_allowed()
        self.api.account_value.login = 12345
        self.api.terminal_value.tradeapi_disabled = True
        with self.assertRaises(OrderRejected): await self.engine.submit(signal())
        self.assertEqual(self.api.requests, [])

    async def test_netting_blocked(self):
        self.api.account_value.margin_mode = 0
        with self.assertRaises(OrderRejected): await self.engine.submit(signal())

    async def test_stale_tick_expired_signal(self):
        self.api.tick_age = 30
        with self.assertRaises(OrderRejected): await self.engine.submit(signal())
        self.api.tick_age = 0
        with self.assertRaises(OrderRejected):
            await self.engine.submit(replace(signal(), created_at=time.time()-100, expires_at=time.time()-1))

    async def test_disconnect_bad_quotes(self):
        self.api.terminal_value.connected = False
        with self.assertRaises(BrokerError): await self.engine.submit(signal())
        self.api.terminal_value.connected = True
        self.api.bid = -1
        with self.assertRaises(OrderRejected): await self.engine.submit(signal())

    async def test_pending_orders_block(self):
        self.api.pending = (NS(ticket=100),)
        with self.assertRaises(RiskRejected): await self.engine.submit(signal())

    async def test_persistent_drawdown(self):
        await self.broker.account()
        self.api.account_value.equity = 8000
        with self.assertRaises(RiskRejected): await self.engine.submit(signal())
        self.assertEqual(self.journal.meta("peak_equity"), "10000")

    async def test_close_verified_position_identifier(self):
        result = await self.engine.submit(signal())
        await self.broker.close(result["position_id"])
        await self.engine.reconcile()
        self.assertEqual(self.engine.status("signal1")["state"], "closed")

    async def test_stop_tightening_no_widening(self):
        result = await self.engine.submit(signal())
        pid = result["position_id"]
        with self.assertRaises(OrderRejected): await self.broker.modify_position(pid, D("1980"), D("2020.2"))
        await self.broker.modify_position(pid, D("1995"), D("2020.2"))
        self.assertEqual((await self.broker.position(pid))["stop"], D("1995"))

    async def test_candles_closed_tick_volume(self):
        bars = await self.broker.candles("XAUUSD", "M1", 10)
        self.assertEqual(len(bars), 10)
        self.assertEqual((bars[0]["volume"], bars[0]["real_volume"]), (10, 0))
        self.assertLess(bars[-1]["time"], time.time())

    async def test_journal_identity_isolation(self):
        with self.assertRaises(ValueError): self.journal.bind_broker("another-account")

    async def test_agent_auth_and_risk_flow(self):
        agent = Agent(self.engine, "x"*32, {"XAUUSD"})
        body = json.dumps(asdict(signal()), default=str).encode()
        self.assertEqual((await agent.dispatch("POST", "/signals", {}, body))[0], 401)
        self.assertEqual(self.api.requests, [])
        headers = {"authorization": "Bearer "+"x"*32}
        code, result = await agent.dispatch("POST", "/signals", headers, body)
        self.assertEqual((code, result["result"]), (200, "protected"))
        self.assertEqual((await agent.dispatch("POST", "/signals", headers, body))[1]["result"], "duplicate_suppressed")
        self.assertEqual((await agent.dispatch("GET", "/decisions/signal1", headers, b""))[1]["state"], "protected")

    async def test_agent_arbitrary_symbols_config_blocked(self):
        agent = Agent(self.engine, "x"*32, {"XAUUSD"})
        headers = {"authorization": "Bearer "+"x"*32}
        self.assertEqual((await agent.dispatch("POST", "/market", headers, b'{"symbol":"EURUSD"}'))[0], 422)
        self.assertEqual((await agent.dispatch("POST", "/config", headers, b'{}'))[0], 404)
        self.assertEqual(self.api.requests, [])

    async def test_expiry_during_check_no_send(self):
        with self.assertRaises(OrderRejected):
            self.broker._send({"action": 1}, deadline=time.time()-1)
        self.assertEqual(self.api.requests, [])

    async def test_symbol_filling_ioc_and_unsupported_market(self):
        self.api.info.filling_mode = 2  # Documented symbol flag, absent from Python exports.
        self.assertEqual(self.broker._filling(self.api.info), self.api.ORDER_FILLING_IOC)
        self.api.info.filling_mode = 0
        with self.assertRaises(OrderRejected): self.broker._filling(self.api.info)

    async def test_foreign_position_blocks_entries(self):
        await self.engine.submit(signal())
        next(iter(self.api.positions.values())).magic = 999
        with self.assertRaises(ExecutionHalted): await self.engine.submit(signal("new"))
        self.assertEqual(len(self.api.requests), 1)
        self.assertTrue(self.engine.status()["halted"])

    async def test_demo_cli_cannot_send_live(self):
        from scripts.mt5_demo import run
        client = NS(_request=lambda *a: {"mode": "live", "execution_allowed": True})
        from unittest.mock import AsyncMock
        client.market = AsyncMock(return_value={})
        client.submit = AsyncMock()
        args = NS(decision_status=None, symbol="XAUUSD", send=True)
        with patch("scripts.mt5_demo.SignalClient.from_env", return_value=client):
            with self.assertRaises(BrokerError): await run(args)
        client.submit.assert_not_called()

    async def test_http_transport(self):
        agent = Agent(self.engine, "x"*32, {"XAUUSD"})
        server = await asyncio.start_server(agent.handle, "127.0.0.1", 0, limit=8192)
        port = server.sockets[0].getsockname()[1]
        try:
            client = SignalClient(f"http://127.0.0.1:{port}", "x"*32)
            result = await client.submit(signal())
            self.assertEqual(result["result"], "protected")
            self.assertEqual((await client.decision("signal1"))["state"], "protected")
        finally:
            server.close()
            await server.wait_closed()


class ConfigTests(unittest.TestCase):
    def test_defaults_secrets(self):
        with patch.dict(os.environ, {}, clear=True): s = MT5Settings.from_env()
        self.assertEqual(s.mode, "paper")
        self.assertFalse(s.allow_live)
        self.assertNotIn("fixture-secret", repr(MT5Settings(password="fixture-secret")))

    def test_env_live_flag_strict(self):
        with patch.dict(os.environ, {"MT5_MODE": "live", "ALLOW_LIVE_TRADING": "yes"}, clear=True):
            self.assertFalse(MT5Settings.from_env().allow_live)

    def test_remote_https_no_retry(self):
        with self.assertRaises(ValueError): SignalClient("http://example.com", "x"*32)
        client = SignalClient("https://example.com", "x"*32)
        with patch.object(client.opener, "open", side_effect=TimeoutError) as send:
            with self.assertRaises(OrderUncertain): client._request("POST", "/signals", {})
            self.assertEqual(send.call_count, 1)

    def test_tls_required(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError): tls_context("0.0.0.0")
            self.assertIsNone(tls_context("127.0.0.1"))

    def test_process_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"lock"
            with ProcessLease(path):
                with self.assertRaises(RuntimeError):
                    with ProcessLease(path): self.fail("Second agent acquired lease")
