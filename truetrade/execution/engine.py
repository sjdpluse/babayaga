"""Broker-neutral execution with durable deduplication and persistent halt latch."""
import asyncio
import json
from dataclasses import asdict
from types import SimpleNamespace
from truetrade.brokers.base import Broker, BrokerError, OrderRejected, OrderUncertain
from truetrade.brokers.paper import PaperBroker  # Backwards-compatible import.
from truetrade.risk.manager import decimal as D


class ExecutionHalted(RuntimeError):
    pass


class ExecutionEngine:
    def __init__(self, broker: Broker, risk, journal):
        if not isinstance(broker, Broker):
            raise ExecutionHalted("Broker lacks the execution safety contract")
        self.broker, self.risk, self.journal = broker, risk, journal
        self.lock = asyncio.Lock()
        self.journal.bind_broker(broker.identity)

    def _halt(self, reason):
        self.journal.set_meta("execution_halt", reason)

    @staticmethod
    def _error_diagnostic(error, phase):
        """Persist useful diagnostics without leaking arbitrary exception text."""
        diagnostic = {"phase": phase, "error_type": type(error).__name__}
        if isinstance(error, BrokerError):
            diagnostic["error"] = str(error)
        receipt = getattr(error, "receipt", None)
        diagnostic["receipt"] = receipt if isinstance(receipt, dict) else {}
        if isinstance(error, BrokerError) and error.diagnostic:
            diagnostic["broker_diagnostic"] = error.diagnostic
        return diagnostic

    async def open(self, decision_id, market, side, entry, atr, confidence, tier=1., leverage=20):
        """Legacy paper/crypto entry point; preserve strategy and risk behavior."""
        request = {"market": asdict(market), "side": side, "entry": entry, "atr": atr,
                   "confidence": confidence, "tier": tier, "leverage": leverage}
        async def prepare():
            account = await self.broker.account()
            return self.risk.size(market, account, side, entry, atr, confidence, tier, leverage)
        return await self._execute(decision_id, request, prepare)

    async def submit(self, signal):
        async def prepare():
            signal.validate_time()
            if signal.expected_identity is not None and signal.expected_identity != self.broker.identity:
                raise OrderRejected("Signal broker identity differs")
            if signal.expected_state_id is not None and signal.expected_state_id != self.journal.meta("agent_state_id"):
                raise OrderRejected("Signal agent journal differs")
            if signal.expected_mode is not None:
                health = await self.broker.health()
                if health.get("mode") != signal.expected_mode:
                    raise OrderRejected("Signal mode differs from execution account mode")
            if signal.require_flat and await self.broker.open_positions():
                raise OrderRejected("Signal requires an account with no open positions")
            return await self.broker.prepare(signal, self.risk.limits)
        return await self._execute(signal.decision_id, asdict(signal), prepare)

    async def _execute(self, decision_id, request, prepare):
        async with self.lock:
            encoded = json.dumps(request, sort_keys=True, default=str, allow_nan=False)
            old = self.journal.db.execute("SELECT state,payload FROM intents WHERE id=?", (decision_id,)).fetchone()
            if old:
                if json.loads(old[1])["request"] != encoded:
                    raise ValueError("Decision ID reused with a different requested action")
                if old[0] in {"intent", "submitted", "unknown"}:
                    raise ExecutionHalted("Existing decision outcome is unsettled")
                return {"result": "duplicate_suppressed", "decision_id": decision_id}
            if self.journal.meta("execution_halt") or self.journal.unsettled():
                raise ExecutionHalted("Execution halted; reconciliation required")
            await self.broker.assert_execution_allowed()
            await self._audit_protected()
            plan = await prepare()
            payload = {"request": encoded, "plan": asdict(plan)}
            if not self.journal.create_intent(decision_id, payload):
                raise ExecutionHalted("Concurrent submission requires reconciliation")
            try:
                result = await self.broker.open(plan)
                pid = result["positionId"]
                if not isinstance(pid, str) or not pid:
                    raise OrderUncertain("Missing position identity")
            except OrderRejected as error:
                self.journal.transition(decision_id, "rejected")
                self.journal.append("trades", {"intent_id": decision_id, "status": "rejected",
                                               **self._error_diagnostic(error, "open")})
                raise
            except BaseException as error:
                pid = getattr(error, "position_id", None)
                self.journal.transition(decision_id, "unknown", pid)
                self._halt("Order outcome unknown")
                event = {"intent_id": decision_id, "status": "unknown",
                         **self._error_diagnostic(error, "open")}
                self.journal.append("trades", event)
                if pid:
                    await self._emergency_close(decision_id, pid)
                raise ExecutionHalted("Order outcome unknown; no automatic resubmission") from None
            self.journal.transition(decision_id, "submitted", pid)
            try:
                await self.broker.set_protection(pid, plan.stop, plan.take_profit)
                observed = await self.broker.position(pid)
                await self.broker.verify(plan, observed)
            except BaseException as error:
                self.journal.transition(decision_id, "unknown", pid)
                self._halt("Protection or fill not verified")
                self.journal.append("trades", {"intent_id": decision_id, "status": "unknown",
                                               **self._error_diagnostic(error, "verify")})
                await self._emergency_close(decision_id, pid)
                raise ExecutionHalted("Protection/fill unverified; emergency close attempted; reconcile required") from None
            self.journal.transition(decision_id, "protected", pid)
            self.journal.append("trades", {"intent_id": decision_id, "position_id": pid,
                                           "status": "protected", "plan": asdict(plan),
                                           "observed": observed, "receipt": result.get("receipt", {})})
            return {"result": "protected", "position_id": pid, "plan": asdict(plan), "observed": observed}

    async def _emergency_close(self, decision_id, pid):
        try:
            await self.broker.close(pid)
            if await self.broker.confirm_closed(pid):
                self.journal.transition(decision_id, "closed")
        except BaseException:
            pass
        # Even confirmed emergency close never clears the persistent halt.

    @staticmethod
    def _plan(payload):
        values = json.loads(payload)["plan"]
        for key in ("entry", "stop", "take_profit", "size", "risk", "margin", "budget", "risk_fraction"):
            if key in values:
                values[key] = D(values[key])
        return SimpleNamespace(**values)

    async def _audit_protected(self):
        rows = self.journal.db.execute(
            "SELECT id,position_id,payload FROM intents WHERE state='protected'").fetchall()
        for intent_id, pid, payload in rows:
            try:
                observed = await self.broker.position(pid)
                if observed is None and await self.broker.confirm_closed(pid):
                    self.journal.transition(intent_id, "closed")
                    continue
                await self.broker.verify(self._plan(payload), observed)
            except Exception:
                self.journal.transition(intent_id, "unknown")
                self._halt("Previously protected position changed")
                await self._emergency_close(intent_id, pid)
                raise ExecutionHalted("Protected position unverified; emergency close attempted") from None

    async def reconcile(self):
        """No unknown order is resubmitted. Known unresolved positions must be closed.

        Missing submission IDs require manual broker-history investigation. Protected
        position changes trigger a single emergency close and remain latched.
        """
        async with self.lock:
            await self.broker.health()
            for intent_id, state, pid in self.journal.unsettled():
                if state != "unknown":
                    self.journal.transition(intent_id, "unknown")
                if not pid or not await self.broker.confirm_closed(pid):
                    self._halt("Unsettled execution intent")
                    raise ExecutionHalted("Broker history/manual investigation required")
                self.journal.transition(intent_id, "closed")
            await self._audit_protected()
            self.journal.set_meta("execution_halt", "")
            return {"result": "reconciled"}

    def status(self, decision_id=None):
        if decision_id:
            row = self.journal.db.execute("SELECT state,position_id FROM intents WHERE id=?", (decision_id,)).fetchone()
            return {"decision_id": decision_id, "state": row[0] if row else "not_seen",
                    "position_id": row[1] if row else None}
        return {"halted": bool(self.journal.meta("execution_halt")), "unsettled": len(self.journal.unsettled())}
