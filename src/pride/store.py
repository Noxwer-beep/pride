"""The queue, the ledger of what happened, and the approval gate.

SQLite because it is already on the machine, it handles two threads fine, and the
whole state of an unattended system should fit in one file you can copy.

Tables
    tasks       work to do: what, which agent, how many attempts, what came of it
    events      append-only history — the thing you read at 08:00
    runs        one row per agent cycle, so "did it run" has an answer
    approvals   actions that were not allowed to run unattended
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    agent       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    state       TEXT NOT NULL DEFAULT 'queued',
    priority    INTEGER NOT NULL DEFAULT 5,
    attempts    INTEGER NOT NULL DEFAULT 0,
    created     TEXT NOT NULL,
    updated     TEXT NOT NULL,
    result      TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state, priority);
CREATE INDEX IF NOT EXISTS idx_tasks_fingerprint ON tasks(fingerprint);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    agent   TEXT NOT NULL,
    level   TEXT NOT NULL,
    message TEXT NOT NULL,
    task_id TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_at ON events(at DESC);

CREATE TABLE IF NOT EXISTS runs (
    id        TEXT PRIMARY KEY,
    agent     TEXT NOT NULL,
    started   TEXT NOT NULL,
    finished  TEXT,
    observed  INTEGER DEFAULT 0,
    done      INTEGER DEFAULT 0,
    failed    INTEGER DEFAULT 0,
    summary   TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS approvals (
    id        TEXT PRIMARY KEY,
    task_id   TEXT NOT NULL,
    requested TEXT NOT NULL,
    action    TEXT NOT NULL,
    decided   TEXT,
    decision  TEXT,
    note      TEXT DEFAULT ''
);
"""

_write_lock = threading.Lock()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def hours_since(stamp: str) -> float:
    if not stamp:
        return 9_999.0
    try:
        return (datetime.now() - datetime.fromisoformat(stamp)).total_seconds() / 3600
    except ValueError:
        return 9_999.0


@dataclass
class Task:
    agent: str
    kind: str
    title: str
    payload: dict = field(default_factory=dict)
    priority: int = 5
    fingerprint: str = ""
    id: str = ""
    state: str = "queued"
    attempts: int = 0
    result: str = ""

    def to_row(self) -> dict:
        return {
            "id": self.id or uuid.uuid4().hex[:12],
            "agent": self.agent, "kind": self.kind, "title": self.title[:200],
            "payload": json.dumps(self.payload), "state": self.state,
            "priority": self.priority, "attempts": self.attempts,
            "created": now(), "updated": now(), "result": self.result[:2000],
            "fingerprint": self.fingerprint,
        }


class Store:
    def __init__(self, path: Path | None = None) -> None:
        root = config.ensure_dirs()
        self.path = Path(path) if path else root / "pride.db"
        self._local = threading.local()
        with self._conn() as connection:
            connection.executescript(SCHEMA)
            connection.execute("PRAGMA journal_mode=WAL")

    def _conn(self) -> sqlite3.Connection:
        connection = getattr(self._local, "conn", None)
        if connection is None:
            connection = sqlite3.connect(self.path, timeout=15)
            connection.row_factory = sqlite3.Row
            self._local.conn = connection
        return connection

    # ---------------------------------------------------------------- tasks
    def add_task(self, task: Task) -> str | None:
        """Insert unless an open task with the same fingerprint already exists."""
        with _write_lock:
            connection = self._conn()
            if task.fingerprint:
                existing = connection.execute(
                    "SELECT id FROM tasks WHERE fingerprint = ? AND state IN "
                    "('queued','running','awaiting_approval') LIMIT 1",
                    (task.fingerprint,),
                ).fetchone()
                if existing:
                    return None
            row = task.to_row()
            connection.execute(
                "INSERT INTO tasks (id, agent, kind, title, payload, state, priority,"
                " attempts, created, updated, result, fingerprint) "
                "VALUES (:id, :agent, :kind, :title, :payload, :state, :priority,"
                " :attempts, :created, :updated, :result, :fingerprint)", row,
            )
            connection.commit()
            return row["id"]

    def peek(self, limit: int = 20) -> list[sqlite3.Row]:
        """Queued work, looked at but not taken. Dry runs use this."""
        return self._conn().execute(
            "SELECT * FROM tasks WHERE state = 'queued' "
            "ORDER BY priority ASC, created ASC LIMIT ?", (limit,)).fetchall()

    def claim(self, limit: int = 8) -> list[sqlite3.Row]:
        """Take queued work, oldest-and-most-important first. Marks it running."""
        with _write_lock:
            connection = self._conn()
            rows = connection.execute(
                "SELECT * FROM tasks WHERE state = 'queued' "
                "ORDER BY priority ASC, created ASC LIMIT ?", (limit,),
            ).fetchall()
            if not rows:
                return []
            ids = [row["id"] for row in rows]
            connection.executemany(
                "UPDATE tasks SET state='running', attempts=attempts+1, updated=? "
                "WHERE id=?", [(now(), task_id) for task_id in ids])
            connection.commit()
            marks = ",".join("?" * len(ids))
            return connection.execute(
                f"SELECT * FROM tasks WHERE id IN ({marks}) "
                "ORDER BY priority ASC, created ASC", ids).fetchall()

    def finish(self, task_id: str, state: str, result: str = "") -> None:
        with _write_lock:
            connection = self._conn()
            connection.execute(
                "UPDATE tasks SET state=?, result=?, updated=? WHERE id=?",
                (state, result[:2000], now(), task_id),
            )
            connection.commit()

    def retry_later(self, task_id: str, error: str, max_attempts: int) -> str:
        """Back to the queue, or give up and say why."""
        with _write_lock:
            connection = self._conn()
            row = connection.execute(
                "SELECT attempts FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            attempts = int(row["attempts"]) if row else max_attempts
            state = "failed" if attempts >= max_attempts else "queued"
            connection.execute(
                "UPDATE tasks SET state=?, result=?, updated=? WHERE id=?",
                (state, error[:2000], now(), task_id),
            )
            connection.commit()
            return state

    def resume_running(self) -> int:
        """A crashed daemon leaves tasks 'running' forever; put them back."""
        with _write_lock:
            connection = self._conn()
            cursor = connection.execute(
                "UPDATE tasks SET state='queued', updated=? WHERE state='running'", (now(),)
            )
            connection.commit()
            return cursor.rowcount

    def tasks(self, state: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
        query = "SELECT * FROM tasks"
        params: tuple = ()
        if state:
            query += " WHERE state = ?"
            params = (state,)
        query += " ORDER BY updated DESC LIMIT ?"
        return self._conn().execute(query, (*params, limit)).fetchall()

    def last_done(self, agent: str, kind: str) -> str:
        row = self._conn().execute(
            "SELECT updated FROM tasks WHERE agent=? AND kind=? AND state='done' "
            "ORDER BY updated DESC LIMIT 1", (agent, kind),
        ).fetchone()
        return str(row["updated"]) if row else ""

    # ---------------------------------------------------------------- events
    def event(self, agent: str, level: str, message: str, task_id: str = "") -> None:
        with _write_lock:
            connection = self._conn()
            connection.execute(
                "INSERT INTO events (at, agent, level, message, task_id) VALUES (?,?,?,?,?)",
                (now(), agent, level, message[:2000], task_id),
            )
            connection.commit()

    def events(self, limit: int = 40, level: str | None = None) -> list[sqlite3.Row]:
        if level:
            return self._conn().execute(
                "SELECT * FROM events WHERE level=? ORDER BY id DESC LIMIT ?",
                (level, limit)).fetchall()
        return self._conn().execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def counts(self) -> dict[str, int]:
        connection = self._conn()
        out = {}
        for row in connection.execute(
            "SELECT state, COUNT(*) AS n FROM tasks GROUP BY state"
        ):
            out[row["state"]] = row["n"]
        return out

    # ---------------------------------------------------------------- runs
    def start_run(self, agent: str) -> str:
        run_id = f"{agent}-{uuid.uuid4().hex[:8]}"
        with _write_lock:
            connection = self._conn()
            connection.execute(
                "INSERT INTO runs (id, agent, started) VALUES (?,?,?)",
                (run_id, agent, now()))
            connection.commit()
        return run_id

    def finish_run(self, run_id: str, observed: int, done: int, failed: int,
                   summary: str) -> None:
        with _write_lock:
            connection = self._conn()
            connection.execute(
                "UPDATE runs SET finished=?, observed=?, done=?, failed=?, summary=? "
                "WHERE id=?", (now(), observed, done, failed, summary[:500], run_id))
            connection.commit()

    def last_runs(self, limit: int = 12) -> list[sqlite3.Row]:
        return self._conn().execute(
            "SELECT * FROM runs ORDER BY started DESC LIMIT ?", (limit,)).fetchall()

    def last_run_for(self, agent: str) -> str:
        row = self._conn().execute(
            "SELECT started FROM runs WHERE agent=? ORDER BY started DESC LIMIT 1",
            (agent,)).fetchone()
        return str(row["started"]) if row else ""

    def due(self, agent: str, cadence_s: int) -> bool:
        last = self.last_run_for(agent)
        if not last:
            return True
        return hours_since(last) * 3600 >= cadence_s

    # ---------------------------------------------------------------- approvals
    def request_approval(self, task: "Task", action: str, note: str = "") -> str:
        """Park an existing task until a human says yes.

        It does not insert a task: the task is already in the queue with its payload
        intact, it just is not allowed to move.
        """
        approval_id = uuid.uuid4().hex[:10]
        with _write_lock:
            connection = self._conn()
            connection.execute(
                "INSERT INTO approvals (id, task_id, requested, action, note) "
                "VALUES (?,?,?,?,?)", (approval_id, task.id, now(), action, note))
            connection.execute(
                "UPDATE tasks SET state='awaiting_approval', updated=? WHERE id=?",
                (now(), task.id))
            connection.commit()
        return approval_id

    def approvals(self, open_only: bool = True) -> list[sqlite3.Row]:
        query = ("SELECT a.*, t.title, t.agent FROM approvals a "
                 "LEFT JOIN tasks t ON t.id = a.task_id")
        if open_only:
            query += " WHERE a.decided IS NULL"
        return self._conn().execute(query + " ORDER BY a.requested DESC").fetchall()

    def approval_for(self, task_id: str) -> str:
        """The newest decision on a task: 'approve', 'reject' or '' for undecided."""
        row = self._conn().execute(
            "SELECT decision FROM approvals WHERE task_id=? ORDER BY requested DESC LIMIT 1",
            (task_id,)).fetchone()
        return str(row["decision"] or "") if row else ""

    def decide(self, approval_id: str, decision: str, note: str = "") -> bool:
        with _write_lock:
            connection = self._conn()
            row = connection.execute(
                "SELECT task_id FROM approvals WHERE id=? AND decided IS NULL",
                (approval_id,)).fetchone()
            if row is None:
                return False
            connection.execute(
                "UPDATE approvals SET decided=?, decision=?, note=? WHERE id=?",
                (now(), decision, note, approval_id))
            state = "queued" if decision == "approve" else "rejected"
            connection.execute(
                "UPDATE tasks SET state=?, updated=? WHERE id=?",
                (state, now(), row["task_id"]))
            connection.commit()
        return True


def age(stamp: str) -> str:
    """'4m ago' — used by both the CLI and the dashboard."""
    if not stamp:
        return "never"
    try:
        seconds = (datetime.now() - datetime.fromisoformat(stamp)).total_seconds()
    except ValueError:
        return stamp
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h ago"
    return f"{seconds / 86400:.0f}d ago"


def since(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None


def later(minutes: int) -> str:
    return (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")
