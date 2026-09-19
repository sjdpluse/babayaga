"""Transactional bar cursor and signal outbox alongside the existing audit journal."""
from dataclasses import asdict
import json
from truetrade.persistence.store import Journal


class WorkerStore(Journal):
    def __init__(self, path):
        super().__init__(path)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS worker_bars (
            stream TEXT NOT NULL, timestamp INTEGER NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(stream,timestamp));
        CREATE TABLE IF NOT EXISTS worker_decisions (
            id TEXT PRIMARY KEY, stream TEXT NOT NULL, bar_time INTEGER NOT NULL,
            state TEXT NOT NULL, payload TEXT NOT NULL, detail TEXT NOT NULL,
            UNIQUE(stream,bar_time));
        """)
        self.db.commit()

    def capture(self, stream, rows):
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO worker_bars VALUES (?,?,?)",
                                [(stream,r["time"],json.dumps(r,allow_nan=False)) for r in rows])

    def seen(self, stream, bar_time):
        row = self.db.execute("SELECT max(bar_time) FROM worker_decisions WHERE stream=?", (stream,)).fetchone()
        return row[0] is not None and bar_time <= row[0]

    def record(self, key, stream, bar_time, state, signal=None, detail=""):
        payload = json.dumps(asdict(signal) if signal else {}, default=str, sort_keys=True, allow_nan=False)
        with self.db:
            self.db.execute("INSERT INTO worker_decisions VALUES (?,?,?,?,?,?)",
                            (key,stream,bar_time,state,payload,detail))

    def transition(self, key, state, detail=""):
        with self.db:
            self.db.execute("UPDATE worker_decisions SET state=?,detail=? WHERE id=?", (state,detail,key))

    def pending(self):
        return self.db.execute("SELECT id,state,payload FROM worker_decisions WHERE state IN ('sending','unknown','protected')").fetchall()

    def last_decision(self):
        row = self.db.execute("SELECT id,state,bar_time FROM worker_decisions ORDER BY rowid DESC LIMIT 1").fetchone()
        return dict(zip(("id","state","bar_time"),row)) if row else None
