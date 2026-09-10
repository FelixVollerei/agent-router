"""Only abstract outcome metadata is stored here; evidence stays in the local run."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class ExperienceStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY, run_id TEXT, attempt INTEGER, created_at TEXT,
            domain TEXT, task_type TEXT, model TEXT, reasoning TEXT, strategy TEXT,
            success INTEGER, runtime REAL, api_cost REAL, codex_usage REAL, tokens INTEGER,
            escalation INTEGER, verifier TEXT, human_intervention INTEGER,
            deadline_class TEXT, context_size INTEGER, outcome_kind TEXT,
            UNIQUE(run_id, attempt, strategy))""")
        existing = {r[1] for r in self.db.execute("PRAGMA table_info(attempts)")}
        for column in ("environment", "provider", "actual_model"):
            if column not in existing:
                self.db.execute(f"ALTER TABLE attempts ADD COLUMN {column} TEXT")
        self.db.commit()

    def record(self, *, run_id, attempt, profile, model, strategy, success, runtime,
               escalation, verifier, human_intervention=False, api_cost=None,
               codex_usage=None, tokens=None, outcome_kind="engineering"):
        with self.db:
            self.db.execute("""INSERT INTO attempts
                (run_id, attempt, created_at, domain, task_type, model, reasoning, strategy,
                 success, runtime, api_cost, codex_usage, tokens, escalation, verifier,
                 human_intervention, deadline_class, context_size, outcome_kind, environment, provider, actual_model)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                run_id, attempt, datetime.now(timezone.utc).isoformat(), profile.domain,
                profile.task_type, model.id, model.reasoning, strategy, int(success), runtime,
                api_cost, codex_usage, tokens, int(escalation), verifier, int(human_intervention),
                profile.deadline_class(), profile.estimated_context_size, outcome_kind,
                profile.environment, model.provider, model.model))

    def stats(self, profile, model, strategy="execute"):
        row = self.db.execute("""SELECT count(*), coalesce(sum(success),0), avg(runtime),
            avg(api_cost), avg(escalation), avg(human_intervention) FROM attempts
            WHERE domain=? AND task_type=? AND model=? AND reasoning=? AND strategy=?
            AND environment=? AND provider=? AND actual_model=?
            AND outcome_kind='engineering'""",
            (profile.domain, profile.task_type, model.id, model.reasoning, strategy,
             profile.environment, model.provider, model.model)).fetchone()
        n, successes, runtime, cost, escalation, human = row
        # Small, explicit prior strength. Empirical evidence dominates as n grows.
        return dict(attempts=n, successes=successes,
                    success_rate=successes / n if n else None,
                    adjusted_success=(successes + 4 * model.prior_success) / (n + 4),
                    avg_runtime=runtime, avg_cost=cost, escalation_rate=escalation,
                    human_intervention_rate=human)

    def export(self):
        self.db.row_factory = sqlite3.Row
        rows = [dict(x) for x in self.db.execute("SELECT * FROM attempts ORDER BY id")]
        self.db.row_factory = None
        return rows

    def close(self):
        self.db.close()
