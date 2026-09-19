"""Paper implementation of the common execution contract."""
from truetrade.brokers.base import OrderRejected, BrokerError

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

    identity = "paper"

    async def assert_execution_allowed(self):
        if self.kind != "paper":
            raise OrderRejected("Paper broker cannot authorize non-paper execution")

    async def health(self):
        return {"connected": True, "mode": "paper", "execution_allowed": True}

    async def verify(self, plan, observed):
        if observed is None:
            raise BrokerError("Missing paper position")
        for key, expected in (("size", plan.size), ("entry", plan.entry),
                              ("stop", plan.stop), ("take_profit", plan.take_profit)):
            if observed[key] != expected:
                raise BrokerError("Paper fill/protection mismatch")

    async def confirm_closed(self, pid):
        return pid not in self.positions

    async def prepare(self, signal, limits):
        raise OrderRejected("Paper signals require an explicit market data provider")
