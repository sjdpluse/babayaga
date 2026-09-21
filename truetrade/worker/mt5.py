"""Railway worker: closed bars -> existing features -> durable signal -> agent.

No credential/endpoint logging, no order retries, no live baseline execution.
Missing configuration keeps liveness up and readiness false. State never uses a
fallback directory. A network ambiguity survives restart and is queried, not resent.
"""
import argparse
import asyncio
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import signal
import time
from urllib.parse import urlsplit

from truetrade.brokers.base import BrokerError, OrderRejected
from truetrade.diagnostics import emit
from truetrade.execution.agent import ProcessLease
from truetrade.execution.remote import SignalClient
from truetrade.worker.store import WorkerStore
from truetrade.worker.strategy import WorkerSettings, TIMEFRAMES, closed_candles, choose, make_signal


class MT5Worker:
    def __init__(self, store, settings=None, client=None):
        self.store = store
        self.settings = settings
        self.client = client
        self.stop = asyncio.Event()
        self.lock = asyncio.Lock()
        self.last_cycle = None
        self.state = {"process": "running", "worker": "mt5", "ready": False,
                      "trading": "blocked", "reason": "starting", "strategy": "ppo_cfd"}
        self.previous_status = None
        self.learning = None

    def status(self, ready, reason):
        self.state.update(ready=ready, trading="enabled" if ready else "blocked", reason=reason,
                          last_decision=self.store.last_decision())

    def configuration(self):
        if self.settings is None:
            self.settings = WorkerSettings.from_env()
        self.state.update(mode=self.settings.mode, symbol=self.settings.symbol, timeframe=self.settings.timeframe,
                          strategy=self.settings.strategy)
        if self.client is None:
            url = os.getenv("MT5_AGENT_URL", "")
            token = os.getenv("MT5_AGENT_TOKEN", "")
            if not url or len(token) < 32:
                self.status(False, "missing_agent_url_or_token")
                return False
            if os.getenv("RAILWAY_ENVIRONMENT_ID") and (urlsplit(url).scheme != "https" or
                    urlsplit(url).hostname in {"localhost", "127.0.0.1", "::1"}):
                raise ValueError("Railway requires a remote HTTPS agent")
            self.client = SignalClient(url, token)
        self.state.update(mode=self.settings.mode, symbol=self.settings.symbol, timeframe=self.settings.timeframe,
                          strategy=self.settings.strategy)
        return True

    async def recover(self):
        """Only a terminal journal outcome resolves a sent signal. Never repost it."""
        unsettled = False
        for key, prior, payload in self.store.pending():
            remote = await self.client.decision(key)
            if remote.get("decision_id") != key:
                raise BrokerError("Decision status identity mismatch")
            state = remote.get("state")
            if state in {"protected", "closed", "rejected"}:
                self.store.transition(key, state, "confirmed_by_agent_journal")
            else:
                self.store.transition(key, "unknown", "operator_reconciliation_required")
                unsettled = True
        return unsettled

    async def tick(self):
        async with self.lock:
            if not self.configuration():
                return
            snapshot = await self.client.execution_state()
            if snapshot.get("protocol") != 1 or not isinstance(snapshot.get("positions"), list):
                raise BrokerError("Agent upgrade required")
            health = snapshot["health"]
            if health.get("mode") != self.settings.mode or health.get("mode") not in {"paper", "demo", "live"}:
                self.status(False, "agent_mode_mismatch_or_live_blocked")
                return
            if not health.get("connected") or not health.get("execution_allowed"):
                self.status(False, "agent_execution_not_allowed")
                return
            identity, epoch = snapshot.get("identity"), snapshot.get("state_id")
            if not isinstance(identity, str) or not identity or not isinstance(epoch, str) or not epoch:
                raise BrokerError("Agent identity missing")
            binding = identity+":"+epoch
            old = self.store.meta("worker_agent_binding")
            if old and old != binding:
                self.status(False, "agent_identity_or_journal_changed")
                return
            if not old:
                self.store.set_meta("worker_agent_binding", binding)
            unresolved = await self.recover()
            if unresolved:
                self.status(False, "uncertain_signal_manual_reconciliation_required")
                return
            if snapshot["execution"].get("halted") or snapshot["execution"].get("unsettled"):
                self.status(False, "agent_halted_reconciliation_required")
                return
            cfg = self.settings
            rows = (await self.client.candles(cfg.symbol, cfg.timeframe, cfg.bars))["candles"]
            now = time.time()
            candles = closed_candles(rows, cfg, now)
            stream = cfg.symbol+":"+cfg.timeframe
            self.store.capture(stream, rows)
            model_ready=True
            if cfg.strategy=='ppo_cfd':
                from truetrade.cfd.learning import Learning
                if self.learning is None:
                    self.learning=Learning(self.store,cfg,Path(self.store.path).parent/'cfd')
                model_ready=await self.learning.service(self.client,rows,TIMEFRAMES[cfg.timeframe])
                self.state['learning']=self.learning.status
                self.state['learning_metrics']=self.learning.metrics
            bar = int(candles.timestamp[-1])
            self.state["last_closed_bar"] = bar
            if now - (bar+TIMEFRAMES[cfg.timeframe]) > cfg.max_bar_age:
                self.status(False, "stale_candles_or_market_closed")
                return
            market = await self.client.market(cfg.symbol)
            from truetrade.brokers.base import Quote
            Quote(**market["quote"]).validate()
            bar_deadline = bar+TIMEFRAMES[cfg.timeframe]+cfg.max_bar_age
            if time.time() >= bar_deadline:
                self.status(False, "stale_candles_or_market_closed")
                return
            if not model_ready:
                self.status(False,"no_qualified_ppo_model")
                return
            if self.store.seen(stream, bar):
                self.status(True, "waiting_for_next_closed_bar")
                return
            key = "bar_"+hashlib.sha256(f"{binding}:{stream}:{bar}".encode()).hexdigest()[:48]
            if snapshot["positions"]:
                self.store.record(key, stream, bar, "skipped", detail="account_has_open_position")
                self.status(True, "position_open_waiting_for_sl_tp")
                return
            model_sha=None
            if cfg.strategy=='ppo_cfd':
                side,atr,model_sha=self.learning.choose(rows)
                from truetrade.risk.manager import decimal as D
                atr=D(atr)
                self.state['model_sha256']=model_sha
            else:
                side, atr = choose(candles)
            if side is None:
                self.store.record(key, stream, bar, "hold", detail="policy_hold")
                self.status(True, "no_signal")
                return
            sig = make_signal(key, side, atr, market, cfg, time.time())
            sig = replace(sig, expected_identity=identity, expected_state_id=epoch, model_sha256=model_sha,
                          expires_at=min(sig.expires_at, bar_deadline))
            self.store.record(key, stream, bar, "sending", signal=sig, detail="persisted_before_post")
            try:
                result = await self.client.submit(sig)
                if result.get("result") not in {"protected", "duplicate_suppressed"}:
                    raise BrokerError("Unexpected execution response")
                decision = await self.client.decision(key)
                if decision.get("decision_id") != key or decision.get("state") not in {"protected", "closed", "rejected"}:
                    raise BrokerError("Journal outcome unverified")
                self.store.transition(key, decision["state"], "verified_agent_journal")
                self.store.append("decision_logs", {"decision_id": key, "strategy": cfg.strategy,
                                                   "signal": asdict(sig), "result": decision})
                self.status(True, "signal_processed")
            except OrderRejected:
                self.store.transition(key, "rejected", "known_pre_execution_rejection")
                self.status(False, "signal_rejected_by_execution_checks")
            except BaseException:
                self.store.transition(key, "unknown", "query_only_never_resend")
                self.status(False, "uncertain_signal_manual_reconciliation_required")
                raise

    async def cycle(self):
        try:
            async with asyncio.timeout(150):
                await self.tick()
        except asyncio.CancelledError:
            raise
        except (ValueError, KeyError, TypeError):
            self.status(False, "invalid_configuration_or_market_data")
        except Exception:
            if self.state.get("reason") != "uncertain_signal_manual_reconciliation_required":
                self.status(False, "agent_unavailable_or_read_failed")
        if self.learning:
            self.state["learning"]=self.learning.status
            self.state["learning_metrics"]=self.learning.metrics
        self.last_cycle = time.time()
        summary = {k:self.state[k] for k in ("worker", "ready", "trading", "reason", "strategy", "learning", "learning_metrics") if k in self.state}
        if summary != self.previous_status:
            emit("mt5_worker_status", summary)
            self.previous_status = summary

    async def run(self, once=False):
        while not self.stop.is_set():
            await self.cycle()
            if once:
                return self.snapshot()
            try:
                await asyncio.wait_for(self.stop.wait(), self.settings.poll_seconds if self.settings else 10)
            except TimeoutError:
                pass

    def snapshot(self):
        result = {**self.state, "last_cycle": self.last_cycle}
        if self.last_cycle is None or time.time()-self.last_cycle > 180:
            result.update(ready=False, trading="blocked", reason="worker_not_yet_ready_or_stalled")
        return result

    async def handle_http(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), 3)
            parts = line.decode("ascii").split()
            if len(parts) != 3 or parts[0] != "GET" or parts[1] not in {"/health", "/ready", "/status"}:
                code, body = "404 Not Found", {"error": "not_found"}
            else:
                body = self.snapshot()
                code = "503 Service Unavailable" if parts[1] == "/ready" and not body["ready"] else "200 OK"
            data = json.dumps(body, allow_nan=False).encode()
            writer.write(f"HTTP/1.1 {code}\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode()+data)
            await writer.drain()
        except (ValueError, OSError, TimeoutError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def serve(once=False):
    state = Path(os.getenv("STATE_DIR", "data"))
    if os.getenv("MT5_REQUIRE_VOLUME", "false").lower() == "true":
        mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
        if not mount or not state.resolve().is_relative_to(Path(mount).resolve()):
            raise ValueError("Persistent Railway volume required at STATE_DIR")
    state.mkdir(parents=True, exist_ok=True)
    with ProcessLease(state/"mt5-worker.lock"):
        store = WorkerStore(state/"mt5-worker.sqlite")
        worker = MT5Worker(store)
        server = None
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, worker.stop.set)
                except NotImplementedError:
                    pass
            if not once:
                server = await asyncio.start_server(worker.handle_http, "0.0.0.0", int(os.getenv("PORT", "8080")), limit=4096)
            return await worker.run(once)
        finally:
            if server:
                server.close()
                await server.wait_closed()
            if worker.learning:
                await worker.learning.close()
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(serve(args.once))
    except (ValueError, OSError, RuntimeError):
        raise SystemExit("MT5 worker startup failed; check state volume, file lease and configuration") from None


if __name__ == "__main__":
    main()
