"""Railway worker and minimal status endpoints. No unauthenticated trading controls."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import time
from truetrade.config import Settings
from truetrade.diagnostics import emit
from truetrade.exchange.client import DemoContractUnverified, ExchangeClient, ExchangeError
from truetrade.exchange.collector import collect
from truetrade.persistence.store import Journal, SupabaseSink


class Worker:
    def __init__(self, settings):
        self.settings = settings
        self.state = {"process":"running","mode":settings.mode,"trading":"blocked",
                      "reason":"demo_api_contract_unverified","training":"idle"}
        self.stop = asyncio.Event()
        self.journal = Journal(Path(settings.state_dir)/"journal.sqlite")

    async def handle_http(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), 3)
            parts = line.decode("ascii",errors="replace").split()
            path = parts[1] if len(parts) > 1 else ""
            method = parts[0] if parts else ""
            if method != "GET" or path not in {"/health", "/ready", "/status"}:
                status, body = "404 Not Found", {"error":"not_found"}
            elif path == "/ready":
                status, body = "503 Service Unavailable", {"ready_for_exchange_demo":False,"reason":self.state["reason"]}
            else:
                status, body = "200 OK", self.state
            raw = json.dumps(body).encode()
            writer.write(f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(raw)}\r\nConnection: close\r\n\r\n".encode()+raw)
            await writer.drain()
        except (OSError, asyncio.TimeoutError): pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def run(self, once=False):
        if self.settings.mode == "demo":
            raise DemoContractUnverified("Demo exchange adapter is unverified; this release does not send orders")
        if self.settings.api_key and self.settings.api_secret:
            from scripts.check_connection import check
            try:
                preflight = await check(self.settings)
                self.state["connection"] = preflight
            except (ValueError, ExchangeError) as error:
                self.state["connection"] = {"status":"blocked", "reason":str(error)}
        else:
            self.state["connection"] = {"status":"credentials_not_configured"}
        emit("connection_preflight", self.state["connection"])
        if self.settings.mode == "collect" and self.state["connection"].get("futures_markets") != "ok":
            raise ValueError("Connection preflight failed; inspect the redacted startup diagnostic")
        sink = SupabaseSink(self.settings.supabase_url,self.settings.supabase_key) if self.settings.supabase_url and self.settings.supabase_key else None
        next_capture = 0.
        capture_interval = max(60, int(os.getenv("COLLECT_INTERVAL_SECONDS","3600")))
        next_train = 0.
        train_interval = max(60, int(os.getenv("RETRAIN_SECONDS","21600")))
        while not self.stop.is_set():
            if self.settings.mode == "collect" and time.monotonic() >= next_capture:
                mapping = os.getenv("API_MAPPING_PATH", "")
                if not mapping: raise ValueError("API_MAPPING_PATH is required for collect mode")
                conf = json.loads(Path(mapping).read_text())
                seconds = int(conf["resolution_seconds"])
                end = int(time.time())//seconds*seconds
                start = end-int(os.getenv("HISTORY_BARS","10000"))*seconds
                self.state["collection"] = "running"
                datasets = await collect(ExchangeClient(self.settings),mapping,
                    Path(self.settings.state_dir)/"captures",start,end,self.journal)
                self.state["collection"] = "complete"
                next_capture = time.monotonic()+capture_interval
                if os.getenv("ENABLE_TRAINING","false").lower() == "true" and time.monotonic() >= next_train:
                    from truetrade.rl.trainer import train_dataset
                    self.state["training"] = "running"
                    for dataset in datasets:
                        path, manifest = await asyncio.to_thread(train_dataset,dataset,
                            Path(self.settings.state_dir)/"models",int(os.getenv("TRAIN_EPISODES","2000")))
                        self.journal.append("model_checkpoints", {"path":str(path),"manifest":manifest})
                    self.state["training"] = "complete"
                    next_train = time.monotonic()+train_interval
            if sink:
                sent = await sink.flush(self.journal)
                self.state["audit_records_synced_last_cycle"] = sent
            if once: return self.state
            try: await asyncio.wait_for(self.stop.wait(), timeout=30)
            except asyncio.TimeoutError: pass


async def serve(once=False):
    worker = Worker(Settings.from_env())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM,signal.SIGINT):
        try: loop.add_signal_handler(sig,worker.stop.set)
        except NotImplementedError: pass
    server = None
    try:
        if not once:
            server = await asyncio.start_server(worker.handle_http,"0.0.0.0",int(os.getenv("PORT","8080")),limit=4096)
        emit("worker_start", worker.state)
        return await worker.run(once)
    finally:
        if server:
            server.close(); await server.wait_closed()
        worker.journal.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--once",action="store_true")
    p.add_argument("--explain",metavar="EVENT_ID")
    a = p.parse_args()
    if a.explain:
        journal = Journal(Path(Settings.from_env().state_dir)/"journal.sqlite")
        try: print(json.dumps(journal.get(a.explain),ensure_ascii=False,indent=2))
        finally: journal.close()
        return
    try: asyncio.run(serve(a.once))
    except (ValueError,DemoContractUnverified) as e:
        emit("worker_blocked", {"status":"blocked","reason":str(e)})
        raise SystemExit(2) from None


if __name__ == "__main__": main()
