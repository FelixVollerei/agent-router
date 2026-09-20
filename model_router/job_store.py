"""Durable admission, client-scoped identity, replayable facts. No execution on read."""
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .job_contract import (PROTOCOL, SETTLEMENT_SCHEMA_VERSION, TERMINAL, canonical,
    stop_acknowledgement)


class JobStore:
    #: Columns added by the v2 round. Append-only on purpose: these are facts the caller supplies
    #: or the peer settles with, so an older peer reading the same database simply ignores them.
    #: No existing column, UNIQUE constraint or dedup predicate moves.
    ADDED_COLUMNS = {"protocol": "TEXT NOT NULL DEFAULT 'router.jobs/v1'", "work_revision": "TEXT",
        "approval": "TEXT", "settlement_schema_version": "INTEGER"}

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, client TEXT NOT NULL, task_id TEXT NOT NULL, idem TEXT NOT NULL,
                request_hash TEXT NOT NULL, request TEXT NOT NULL, state TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, result TEXT, run_id TEXT,
                UNIQUE(client,task_id), UNIQUE(client,idem));
            CREATE TABLE IF NOT EXISTS events(
                job_id TEXT NOT NULL REFERENCES jobs(id), seq INTEGER NOT NULL, event TEXT NOT NULL,
                PRIMARY KEY(job_id,seq));
        """)
        self._migrate()

    def _migrate(self):
        """Idempotent, additive schema migration: an existing database is upgraded in place."""
        existing = {row["name"] for row in self.db.execute("PRAGMA table_info(jobs)").fetchall()}
        for name, definition in self.ADDED_COLUMNS.items():
            if name not in existing:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
        self.db.commit()

    def _event(self, job_id, kind, data):
        seq = self.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE job_id=?", (job_id,)).fetchone()[0]
        if seq > 4096:
            raise ValueError("event_capacity_exhausted")
        item = {"sequence": seq, "kind": kind, "data": data, "time": time.time()}
        self.db.execute("INSERT INTO events VALUES(?,?,?)", (job_id, seq, canonical(item)))

    def admit(self, client, request, request_hash, *, protocol=PROTOCOL, work_revision=None, approval=None):
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute("SELECT * FROM jobs WHERE client=? AND (task_id=? OR idem=?)",
                (client, request["client_task_id"], request["idempotency_key"])).fetchall()
            if rows:
                if len(rows) != 1 or rows[0]["request_hash"] != request_hash:
                    raise ValueError("idempotency_conflict")
                return rows[0]["id"], True
            if self.db.execute("SELECT 1 FROM jobs WHERE state NOT IN ('verified','needs_human','blocked','failed','cancelled','timed_out') LIMIT 1").fetchone():
                raise ValueError("busy_or_quarantined_unknown")
            if self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] >= 4096:
                raise ValueError("job_capacity_exhausted")
            identifier, now = uuid.uuid4().hex, time.time()
            self.db.execute("INSERT INTO jobs(id,client,task_id,idem,request_hash,request,state,created,"
                "updated,result,run_id,protocol,work_revision,approval,settlement_schema_version) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                identifier, client, request["client_task_id"], request["idempotency_key"], request_hash,
                canonical(request), "accepted", now, now, None, None, protocol, work_revision,
                canonical(approval) if approval is not None else None, None))
            self._event(identifier, "accepted", {})
            return identifier, False

    def get(self, client, identifier):
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=? AND client=?", (identifier, client)).fetchone()
            if row is None:
                raise ValueError("job_not_found")
            result = dict(row)
            result["request"] = json.loads(result["request"])
            result["result"] = json.loads(result["result"]) if result["result"] else None
            return result

    def update(self, identifier, state, data=None, run_id=None, *, settlement_schema_version=None):
        with self.lock, self.db:
            row = self.db.execute("SELECT state FROM jobs WHERE id=?", (identifier,)).fetchone()
            if row is None or row[0] in TERMINAL:
                return
            data = data or {}
            self.db.execute("UPDATE jobs SET state=?,updated=?,result=?,run_id=COALESCE(?,run_id),"
                "settlement_schema_version=COALESCE(?,settlement_schema_version) WHERE id=?",
                (state, time.time(), canonical(data) if state in TERMINAL else None, run_id,
                 settlement_schema_version, identifier))
            self._event(identifier, state, data)

    def event(self, identifier, kind, data):
        with self.lock, self.db:
            # Reserve space for cancellation and the final fact, even on verbose providers.
            count = self.db.execute("SELECT COUNT(*) FROM events WHERE job_id=?", (identifier,)).fetchone()[0]
            if count >= 4000:
                raise ValueError("event_capacity_exhausted")
            self._event(identifier, kind, data)

    def events(self, client, identifier, after=0, limit=100):
        self.get(client, identifier)
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_event_cursor")
        with self.lock:
            maximum = self.db.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE job_id=?", (identifier,)).fetchone()[0]
            if after > maximum:
                raise ValueError("cursor_ahead_of_journal")
            rows = self.db.execute("SELECT event FROM events WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?", (identifier, after, limit)).fetchall()
        values, size = [], 0
        for row in rows:
            size += len(row[0].encode("utf-8"))
            if size > 120000:
                break
            values.append(json.loads(row[0]))
        cursor = values[-1]["sequence"] if values else after
        return {"events": values, "next_cursor": cursor, "has_more": cursor < maximum}

    def recover(self):
        with self.lock:
            rows = self.db.execute("SELECT id,protocol FROM jobs WHERE state IN ('accepted','running','cancel_requested')").fetchall()
        for row in rows:
            # A restart is exactly the case with no confirmed stop of any kind, so both facts stay
            # explicit rather than being inferred from the fact that nothing is running now.
            self.update(row[0], "unknown", {"reason": "service_restarted_without_confirmed_settlement",
                "local_stopped": None, "remote_stopped": None, "applied": False,
                "stop_acknowledgement": stop_acknowledgement("unknown", {"local_stopped": None})},
                settlement_schema_version=SETTLEMENT_SCHEMA_VERSION.get(row["protocol"], 1))

    def close(self):
        self.db.close()
