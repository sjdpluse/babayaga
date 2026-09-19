import tempfile
import unittest
from pathlib import Path
from helpers import account, market
from truetrade.persistence.store import Journal, SupabaseSink
from truetrade.execution.engine import ExecutionEngine, ExecutionHalted, PaperBroker
from truetrade.risk.manager import RiskManager
from test_exchange import FakeTransport
from truetrade.exchange.client import Response


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/"journal.sqlite"
        self.journal = Journal(self.path)
    def tearDown(self):
        self.journal.close(); self.tmp.cleanup()

    async def test_paper_order_requires_protection(self):
        engine = ExecutionEngine(PaperBroker(account()), RiskManager(), self.journal)
        result = await engine.open("decision1", market(), "LONG", 100, .3, .9)
        self.assertEqual(result["result"], "protected")
        duplicate = await engine.open("decision1", market(), "LONG", 100, .3, .9)
        self.assertEqual(duplicate["result"], "duplicate_suppressed")

    async def test_timeout_blocks_restart_and_resubmission(self):
        class LostResponse(PaperBroker):
            async def open(self, plan):
                await super().open(plan)
                raise TimeoutError()
        broker = LostResponse(account())
        engine = ExecutionEngine(broker, RiskManager(), self.journal)
        with self.assertRaises(ExecutionHalted): await engine.open("d1", market(), "LONG", 100, .3, .9)
        self.assertEqual(len(broker.positions), 1)
        self.journal.close(); self.journal = Journal(self.path)
        restarted = ExecutionEngine(broker, RiskManager(), self.journal)
        with self.assertRaises(ExecutionHalted): await restarted.open("d2", market(), "LONG", 100, .3, .9)
        self.assertEqual(len(broker.positions), 1)

    async def test_failed_protection_emergency_close(self):
        class Broken(PaperBroker):
            async def set_protection(self, *args): raise TimeoutError()
        broker = Broken(account())
        engine = ExecutionEngine(broker, RiskManager(), self.journal)
        with self.assertRaises(ExecutionHalted): await engine.open("d", market(), "LONG", 100, .3, .9)
        self.assertEqual(broker.positions, {})

    async def test_supabase_outbox_idempotent_and_no_false_ack(self):
        event = self.journal.append("decision_logs", {"action":"hold"})
        transport = FakeTransport([Response(503, {}, b'{}'), Response(201, {}, b'')])
        sink = SupabaseSink("https://example.supabase.co", "TEST_ONLY_KEY", transport)
        self.assertEqual(await sink.flush(self.journal), 0)
        self.assertEqual(await sink.flush(self.journal), 1)
        self.assertEqual(await sink.flush(self.journal), 0)
        self.assertEqual(self.journal.get(event)["action"], "hold")

    def test_idempotency_payload_binding(self):
        self.journal.create_intent("x", {"amount":1})
        with self.assertRaises(ValueError): self.journal.create_intent("x", {"amount":2})
        with self.assertRaises(ValueError): self.journal.transition("x", "protected")

    async def test_paper_cannot_authorize_nonpaper_execution(self):
        from truetrade.brokers.base import OrderRejected
        broker = PaperBroker(account()); broker.kind = "demo"
        engine = ExecutionEngine(broker, RiskManager(), self.journal)
        with self.assertRaises(OrderRejected):
            await engine.open("blocked", market(), "LONG", 100, .3, .9)
        self.assertEqual(broker.positions, {})
