"""Execution state machine. Only a paper broker is supplied in this release.

An exchange demo broker must prove server-side account isolation before integration.
Timeouts on writes are UNKNOWN, never blindly retried. TP/SL are included on entry,
then patched and read back. Unknown outcomes block the next order, also on restart.
"""
import asyncio
import json
from dataclasses import asdict
from truetrade.risk.manager import RiskRejected


class ExecutionHalted(RuntimeError): pass


class ExecutionEngine:
    def __init__(self, broker, risk, journal):
        if broker.kind != "paper":
            raise ExecutionHalted("No verified exchange demo broker exists in this release")
        self.broker, self.risk, self.journal = broker, risk, journal
        self.lock = asyncio.Lock()

    async def open(self, decision_id, market, side, entry, atr, confidence, tier=1., leverage=20):
        async with self.lock:
            request = {"market": asdict(market), "side": side, "entry": entry, "atr": atr,
                       "confidence": confidence, "tier": tier, "leverage": leverage}
            encoded_request = json.dumps(request, sort_keys=True, default=str, allow_nan=False)
            existing = self.journal.db.execute("SELECT state,payload FROM intents WHERE id=?", (decision_id,)).fetchone()
            if existing:
                if json.loads(existing[1])["request"] != encoded_request:
                    raise ValueError("Decision ID reused with a different requested action")
                if existing[0] in {"intent", "submitted", "unknown"}:
                    raise ExecutionHalted("Existing decision outcome is unsettled")
                return {"result": "duplicate_suppressed", "decision_id": decision_id}
            if self.journal.unsettled():
                raise ExecutionHalted("Unsettled execution intent: reconcile before new orders")
            account = await self.broker.account()
            plan = self.risk.size(market, account, side, entry, atr, confidence, tier, leverage)
            payload = {"request": encoded_request, "plan": asdict(plan)}
            if not self.journal.create_intent(decision_id, payload):
                return {"result": "duplicate_suppressed", "decision_id": decision_id}
            try:
                # Broker must atomically attach stop and target to entry.
                result = await self.broker.open(plan)
                position_id = result["positionId"]
                if not isinstance(position_id, str) or not position_id:
                    raise ValueError("Missing position identity")
            except Exception:
                self.journal.transition(decision_id, "unknown")
                raise ExecutionHalted("Order outcome unknown; no automatic resubmission") from None
            self.journal.transition(decision_id, "submitted", position_id)
            try:
                await self.broker.set_protection(position_id, plan.stop, plan.take_profit)
                observed = await self.broker.position(position_id)
                if observed["stop"] != plan.stop or observed["take_profit"] != plan.take_profit:
                    raise ValueError("Protection mismatch")
                if observed["size"] != plan.size or observed["entry"] != plan.entry:
                    raise ValueError("Actual fill differs; fresh risk assessment required")
            except Exception:
                self.journal.transition(decision_id, "unknown")
                try:
                    await self.broker.close(position_id)
                    observed = await self.broker.position(position_id)
                    if observed is None:
                        self.journal.transition(decision_id, "closed")
                except Exception:
                    pass
                raise ExecutionHalted("Protection or fill not verified; emergency close attempted, reconcile required") from None
            self.journal.transition(decision_id, "protected")
            self.journal.append("trades", {"intent_id": decision_id, "position_id": position_id,
                                           "status": "protected", "plan": payload})
            return {"result": "protected", "position_id": position_id, "plan": payload}

    async def reconcile(self):
        async with self.lock:
            for intent_id, state, position_id in self.journal.unsettled():
                if state != "unknown": self.journal.transition(intent_id, "unknown")
                if not position_id:
                    raise ExecutionHalted("Unknown order without exchange ID requires broker-supported request lookup")
                position = await self.broker.position(position_id)
                if position is None:
                    self.journal.transition(intent_id, "closed")
                elif position.get("stop") and position.get("take_profit"):
                    # Presence alone is insufficient: close/review rather than guessing sizing.
                    raise ExecutionHalted("Open position found; validate fill and portfolio risk before resuming")


class PaperBroker:
    kind = "paper"
    def __init__(self, account):
        self.snapshot = account
        self.positions = {}

    async def account(self):
        from dataclasses import replace
        from decimal import Decimal
        import time
        used = sum((v["margin"] for v in self.positions.values()), Decimal(0))
        risk = sum((v["risk"] for v in self.positions.values()), Decimal(0))
        return replace(self.snapshot, used_margin=used, open_risk=risk,
                       available_margin=max(Decimal(0), self.snapshot.available_margin-used), timestamp=time.time())

    async def open(self, plan):
        from uuid import uuid4
        pid = "paper-" + str(uuid4())
        self.positions[pid] = {"size": plan.size, "entry": plan.entry, "stop": plan.stop,
            "take_profit": plan.take_profit, "margin": plan.margin, "risk": plan.risk}
        return {"positionId": pid}
    async def set_protection(self, pid, stop, target):
        self.positions[pid].update(stop=stop, take_profit=target)
    async def position(self, pid): return self.positions.get(pid)
    async def close(self, pid): self.positions.pop(pid, None)
