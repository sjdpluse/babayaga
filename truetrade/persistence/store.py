"""Durable execution journal, immutable audit events and retryable Supabase outbox."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4
from urllib.parse import urlsplit
from truetrade.exchange.client import Transport

TABLES = {"trades", "market_snapshots", "model_checkpoints", "risk_state", "decision_logs"}
TRANSITIONS = {"intent": {"submitted", "rejected", "unknown"},
               "submitted": {"protected", "unknown", "closed"},
               "protected": {"closed", "unknown"}, "unknown": {"protected", "closed", "rejected"},
               "rejected": set(), "closed": set()}


class Journal:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS execution_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS intents (
          id TEXT PRIMARY KEY, state TEXT NOT NULL, position_id TEXT, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
          id TEXT PRIMARY KEY, table_name TEXT NOT NULL, payload TEXT NOT NULL,
          created_at TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS transitions (
          seq INTEGER PRIMARY KEY, intent_id TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL);
        """)
        self.db.commit()

    def meta(self, key):
        row = self.db.execute("SELECT value FROM execution_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        with self.db:
            self.db.execute("INSERT INTO execution_meta(key,value) VALUES (?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def bind_broker(self, identity):
        old = self.meta("broker_identity")
        if old and old != identity:
            raise ValueError("Journal belongs to another broker/account/mode")
        if not old:
            if identity != "paper" and self.db.execute("SELECT 1 FROM intents LIMIT 1").fetchone():
                raise ValueError("Legacy journal cannot be rebound to a trading account")
            self.set_meta("broker_identity", identity)

    def create_intent(self, intent_id, payload):
        encoded = json.dumps(payload, sort_keys=True, allow_nan=False, default=str)
        with self.db:
            old = self.db.execute("SELECT payload FROM intents WHERE id=?", (intent_id,)).fetchone()
            if old:
                if old[0] != encoded: raise ValueError("Idempotency key reused with a different request")
                return False
            self.db.execute("INSERT INTO intents(id,state,payload) VALUES (?, 'intent', ?)", (intent_id, encoded))
        return True

    def transition(self, intent_id, state, position_id=None):
        with self.db:
            old = self.db.execute("SELECT state FROM intents WHERE id=?", (intent_id,)).fetchone()
            if not old or state not in TRANSITIONS[old[0]]:
                raise ValueError("Invalid execution state transition")
            self.db.execute("UPDATE intents SET state=?, position_id=coalesce(?,position_id) WHERE id=?", (state, position_id, intent_id))
            self.db.execute("INSERT INTO transitions(intent_id,state,created_at) VALUES (?,?,?)",
                            (intent_id, state, datetime.now(timezone.utc).isoformat()))

    def unsettled(self):
        return self.db.execute("SELECT id,state,position_id FROM intents WHERE state IN ('intent','submitted','unknown')").fetchall()

    def append(self, table, payload, event_id=None):
        if table not in TABLES: raise ValueError("Unknown audit table")
        identifier = event_id or str(uuid4())
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str)
        created = datetime.now(timezone.utc).isoformat()
        with self.db:
            old = self.db.execute("SELECT table_name,payload FROM events WHERE id=?", (identifier,)).fetchone()
            if old and old != (table, encoded): raise ValueError("Event ID conflict")
            self.db.execute("INSERT OR IGNORE INTO events(id,table_name,payload,created_at) VALUES (?,?,?,?)",
                            (identifier, table, encoded, created))
        return identifier

    def get(self, event_id):
        row = self.db.execute("SELECT payload FROM events WHERE id=?", (event_id,)).fetchone()
        if not row: raise KeyError(event_id)
        return json.loads(row[0])

    def close(self): self.db.close()


class SupabaseSink:
    def __init__(self, url, secret, transport=None):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"} or parsed.query or parsed.username:
            raise ValueError("SUPABASE_URL must be a HTTPS project origin")
        if not secret: raise ValueError("SUPABASE_SERVICE_KEY is missing")
        self.url, self.secret, self.transport = url.rstrip("/"), secret, transport or Transport()

    async def flush(self, journal, batch_size=100):
        rows = journal.db.execute("SELECT id,table_name,payload,created_at FROM events WHERE delivered=0 ORDER BY rowid LIMIT ?", (batch_size,)).fetchall()
        sent = 0
        for event_id, table, payload, created in rows:
            body = json.dumps({"id": event_id, "created_at": created, "payload": json.loads(payload)}, allow_nan=False).encode()
            headers = {"apikey": self.secret, "Content-Type": "application/json",
                       "Prefer": "resolution=ignore-duplicates,return=minimal"}
            # New sb_secret keys are API keys, not JWTs; do not put them in Bearer.
            if not self.secret.startswith("sb_secret_"):
                headers["Authorization"] = "Bearer " + self.secret
            try:
                response = await self.transport.send("POST", f"{self.url}/rest/v1/{table}?on_conflict=id", headers, body, 15)
            except (OSError, TimeoutError): break
            if not 200 <= response.status < 300: break
            with journal.db:
                journal.db.execute("UPDATE events SET delivered=1 WHERE id=?", (event_id,))
            sent += 1
            await asyncio.sleep(.05)
        return sent
