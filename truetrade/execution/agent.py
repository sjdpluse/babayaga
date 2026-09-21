"""Authenticated TLS execution agent, one process and one MT5 thread."""
import asyncio
import hmac
import json
import os
from dataclasses import asdict
from pathlib import Path
import ssl
from uuid import uuid4
from truetrade.brokers.base import Signal, BrokerError, OrderRejected
from truetrade.brokers.mt5_config import MT5Settings
from truetrade.config import RiskLimits
from truetrade.execution.engine import ExecutionEngine, ExecutionHalted
from truetrade.persistence.store import Journal
from truetrade.risk.manager import RiskManager, RiskRejected


class ProcessLease:
    """Prevent two local agents sharing a journal, including on Windows."""
    def __init__(self, path):
        self.path, self.file = path, None

    def __enter__(self):
        self.file = open(self.path, "a+b")
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("Another agent owns this state directory") from None
        return self

    def __exit__(self, *args):
        self.file.close()


class Agent:
    def __init__(self, engine, token, allowed_symbols):
        if len(token) < 32:
            raise ValueError("MT5_AGENT_TOKEN must contain at least 32 characters")
        if not allowed_symbols:
            raise ValueError("Agent symbol allowlist required")
        self.engine, self.token = engine, token
        self.allowed_symbols = set(allowed_symbols)
        if not engine.journal.meta("agent_state_id"):
            engine.journal.set_meta("agent_state_id", str(uuid4()))

    async def dispatch(self, method, path, headers, body):
        if not hmac.compare_digest(headers.get("authorization", "").encode(), ("Bearer "+self.token).encode()):
            return 401, {"error": "unauthorized"}
        try:
            if method == "GET" and path == "/health":
                return 200, await self.engine.broker.health()
            if method == "GET" and path == "/status":
                return 200, self.engine.status()
            if method == "GET" and path == "/execution-state":
                async with self.engine.lock:
                    return 200, {"protocol": 1, "identity": self.engine.broker.identity,
                                 "state_id": self.engine.journal.meta("agent_state_id"),
                                 "health": await self.engine.broker.health(),
                                 "execution": self.engine.status(),
                                 "positions": await self.engine.broker.open_positions()}
            if method == "GET" and path.startswith("/outcome/"):
                key=path.removeprefix("/outcome/")
                row=self.engine.journal.db.execute("SELECT state,position_id,payload FROM intents WHERE id=?",(key,)).fetchone()
                if not row or row[0]!='closed':
                    return 409, {"error":"outcome_not_closed"}
                payload=json.loads(row[2]); outcome=await self.engine.broker.closed_outcome(row[1])
                return 200, {"decision_id":key,"outcome":outcome,"plan":payload["plan"],
                             "signal":json.loads(payload["request"])}
            if method == "GET" and path.startswith("/decisions/"):
                return 200, self.engine.status(path.removeprefix("/decisions/"))
            if method != "POST":
                return 404, {"error": "not_found"}
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError()
            if path == "/signals":
                signal = Signal(**data)
                if signal.symbol not in self.allowed_symbols:
                    raise OrderRejected("Symbol outside agent allowlist")
                return 200, await self.engine.submit(signal)
            if path == "/reconcile" and not data:
                return 200, await self.engine.reconcile()
            if path == "/market" and set(data) == {"symbol"}:
                if data["symbol"] not in self.allowed_symbols:
                    raise OrderRejected("Symbol outside agent allowlist")
                info = await self.engine.broker.symbol_info(data["symbol"])
                quote = await self.engine.broker.quote(info.symbol)
                return 200, {"symbol": asdict(info), "quote": asdict(quote)}
            if path == "/research-contract" and set(data)=={"symbol"}:
                if data["symbol"] not in self.allowed_symbols:
                    raise OrderRejected("Symbol outside agent allowlist")
                return 200, await self.engine.broker.research_contract(data["symbol"])
            if path == "/history" and set(data)=={"symbol","timeframe","count","start"}:
                if data["symbol"] not in self.allowed_symbols:
                    raise OrderRejected("Symbol outside agent allowlist")
                return 200, {"candles":await self.engine.broker.candles(data["symbol"],data["timeframe"],data["count"],data["start"])}
            if path == "/candles" and set(data) == {"symbol", "timeframe", "count"}:
                if data["symbol"] not in self.allowed_symbols:
                    raise OrderRejected("Symbol outside agent allowlist")
                return 200, {"candles": await self.engine.broker.candles(data["symbol"], data["timeframe"], data["count"])}
            return 404, {"error": "not_found"}
        except ExecutionHalted:
            return 409, {"error": "execution_halted", **self.engine.status()}
        except (OrderRejected, RiskRejected) as error:
            return 422, {"error": "execution_rejected", "reason": str(error)}
        except (ValueError, TypeError, KeyError):
            return 400, {"error": "invalid_request"}
        except BrokerError as error:
            return 503, {"error": "broker_unavailable", "reason": str(error)}

    async def handle(self, reader, writer):
        code, result = 400, {"error": "invalid_request"}
        try:
            async with asyncio.timeout(10):
                raw = await reader.readuntil(b"\r\n\r\n")
                if len(raw) > 8192:
                    raise ValueError()
                lines = raw.decode("ascii").split("\r\n")
                method, path, version = lines[0].split(" ")
                if version != "HTTP/1.1":
                    raise ValueError()
                headers = {}
                for line in lines[1:]:
                    if not line:
                        continue
                    key, value = line.split(":", 1)
                    key = key.lower()
                    if key in headers:
                        raise ValueError()
                    headers[key] = value.strip()
                if "transfer-encoding" in headers:
                    raise ValueError()
                size = int(headers.get("content-length", "0"))
                if not 0 <= size <= 16384:
                    raise ValueError()
                body = await reader.readexactly(size)
            code, result = await self.dispatch(method, path, headers, body)
        except (ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass
        except Exception:
            code, result = 500, {"error": "internal_error"}
        try:
            data = json.dumps(result, default=str, allow_nan=False).encode()
            head = (f"HTTP/1.1 {code} Response\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(data)}\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n").encode()
            writer.write(head + data)
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def monitor(engine):
    while True:
        async with engine.lock:
            try:
                await engine._audit_protected()
            except Exception:
                engine._halt("Periodic position verification failed")
        await asyncio.sleep(5)


def tls_context(host):
    context = None
    cert, key = os.getenv("MT5_AGENT_TLS_CERT"), os.getenv("MT5_AGENT_TLS_KEY")
    if bool(cert) != bool(key):
        raise ValueError("Both TLS certificate and key are required")
    if cert and key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
    if host not in {"127.0.0.1", "::1", "localhost"} and context is None:
        raise ValueError("Non-loopback agent binding requires TLS")
    return context


async def serve():
    from truetrade.brokers.mt5_server_time import ServerTimeNormalizedMT5Broker
    from truetrade.brokers.mt5_paper import MT5PaperBroker
    settings = MT5Settings.from_env()
    state = Path(os.getenv("MT5_STATE_DIR", "data/mt5-"+settings.mode))
    state.mkdir(parents=True, exist_ok=True)
    host = os.getenv("MT5_AGENT_HOST", "127.0.0.1")
    port = int(os.getenv("MT5_AGENT_PORT", "8787"))
    token = os.getenv("MT5_AGENT_TOKEN", "")
    if len(token) < 32:
        raise ValueError("MT5_AGENT_TOKEN must contain at least 32 characters")
    context = tls_context(host)
    with ProcessLease(state/"agent.lock"):
        journal = Journal(state/"journal.sqlite")
        source = ServerTimeNormalizedMT5Broker(settings, journal)
        watchdog = None
        try:
            await source.connect()
            broker = MT5PaperBroker(source) if settings.mode == "paper" else source
            engine = ExecutionEngine(broker, RiskManager(RiskLimits.from_env()), journal)
            if settings.mode == "paper" and journal.db.execute("SELECT 1 FROM intents WHERE state='protected' LIMIT 1").fetchone():
                raise RuntimeError("Paper inventory is in-memory; use a fresh paper state directory")
            agent = Agent(engine, token, os.getenv("MT5_ALLOWED_SYMBOLS", "XAUUSD").split(","))
            server = await asyncio.start_server(agent.handle, host, port, ssl=context, limit=8192)
            watchdog = asyncio.create_task(monitor(engine))
            print(json.dumps({"agent": "started", "mode": settings.mode, "halted": engine.status()["halted"]}))
            async with server:
                await server.serve_forever()
        finally:
            if watchdog:
                watchdog.cancel()
                try:
                    await watchdog
                except asyncio.CancelledError:
                    pass
            await source.shutdown()
            journal.close()


def main():
    try:
        asyncio.run(serve())
    except (BrokerError, ValueError, RuntimeError):
        raise SystemExit("MT5 agent startup failed; check terminal identity, settings and state") from None


if __name__ == "__main__":
    main()
