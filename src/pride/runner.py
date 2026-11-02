"""The loop.

Two phases, and the split matters:

    observe   ask every due agent what needs doing, and put that in the queue
    act       take work off the queue and do it — in parallel, with a policy gate

Separating them means a crash while acting never loses the observation, and the queue is
the only place decisions live, so "what is it doing right now" has one answer.
"""

from __future__ import annotations

import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import agents as agentsmod
from . import config, policy as policymod
from .store import Store


@dataclass
class CycleResult:
    observed: int = 0
    done: int = 0
    failed: int = 0
    waiting: int = 0
    skipped: int = 0

    def summary(self) -> str:
        return (f"observed {self.observed}, done {self.done}, failed {self.failed}, "
                f"waiting {self.waiting}, skipped {self.skipped}")


class Runner:
    def __init__(self, store: Store, policy: policymod.Policy,
                 dry_run: bool = False, workers: int = 4) -> None:
        self.store = store
        self.policy = policy
        self.dry_run = dry_run or policy.dry_run
        self.workers = workers
        self.ctx = agentsmod.Context(store=store, policy=policy, dry_run=self.dry_run)
        self._stop = threading.Event()

    # ------------------------------------------------------------------ phases
    def observe(self, only: list[str] | None = None) -> int:
        """Queue work from every agent whose cadence has elapsed."""
        observed = 0
        self.last_ran: list[str] = []
        for name, agent in agentsmod.AGENTS.items():
            if only and name not in only:
                continue
            cadence = self.policy.cadence.get(name) or config.CADENCE.get(name, 3600)
            if only is None and not self.store.due(name, cadence):
                continue
            self.last_ran.append(name)
            try:
                tasks = agent.observe(self.ctx)
            except Exception as exc:  # noqa: BLE001 - an agent must not kill the loop
                self.store.event(name, "error", f"observe failed: {exc}")
                continue
            for task in tasks:
                if self.store.add_task(task):
                    observed += 1
        return observed

    def act(self, only: list[str] | None = None) -> CycleResult:
        result = CycleResult()
        if self.dry_run:
            # report without taking: a dry run that empties the queue makes the real
            # run do nothing, which is exactly backwards
            rows = self.store.peek(limit=config.LIMITS["max_tasks_per_cycle"])
            for row in rows:
                self.store.event(row["agent"], "info", f"would run: {row['title']}",
                                 row["id"])
            result.skipped = len(rows)
            return result
        rows = self.store.claim(limit=config.LIMITS["max_tasks_per_cycle"])
        if not rows:
            return result

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self._execute, dict(row)): dict(row) for row in rows}
            for future in as_completed(futures):
                row = futures[future]
                try:
                    outcome = future.result()
                except Exception as exc:  # noqa: BLE001
                    self.store.retry_later(row["id"], f"crashed: {exc}",
                                           config.LIMITS["max_attempts"])
                    result.failed += 1
                    continue
                if outcome == "done":
                    result.done += 1
                elif outcome == "failed":
                    result.failed += 1
                elif outcome == "waiting":
                    result.waiting += 1
                else:
                    result.skipped += 1
        return result

    # ------------------------------------------------------------------ one task
    def _execute(self, row: dict) -> str:
        agent = agentsmod.AGENTS.get(row["agent"])
        task_id = row["id"]
        if agent is None:
            self.store.finish(task_id, "failed", "unknown agent")
            return "failed"

        decision = self.policy.decide(agent.action)
        if decision == policymod.FORBID:
            self.store.finish(task_id, "rejected", f"{agent.action} is forbidden by policy")
            self.store.event(agent.name, "warn",
                             f"refused {agent.action} — forbidden by policy", task_id)
            return "skipped"

        if decision == policymod.ASK and self.store.approval_for(task_id) != "approve":
            if self.store.approval_for(task_id) == "reject":
                self.store.finish(task_id, "rejected", "approval was refused")
                return "skipped"
            self.store.request_approval(
                agentsmod.Task(agent=agent.name, kind=row["kind"], title=row["title"],
                               id=task_id, attempts=int(row["attempts"])),
                agent.action, note=f"agent {agent.name} wants to {agent.action}")
            self.store.event(agent.name, "warn",
                             f"waiting for approval: {agent.action}", task_id)
            return "waiting"

        task = agentsmod.Task(
            agent=agent.name, kind=row["kind"], title=row["title"],
            payload=_payload(row), id=task_id, attempts=int(row["attempts"]),
        )
        started = time.perf_counter()
        try:
            ok, message = agent.act(self.ctx, task)
        except Exception as exc:  # noqa: BLE001
            state = self.store.retry_later(task_id, f"{exc.__class__.__name__}: {exc}",
                                           config.LIMITS["max_attempts"])
            self.store.event(agent.name, "error", f"{row['title']}: {exc}", task_id)
            return "done" if state == "done" else "failed"

        elapsed = round((time.perf_counter() - started) * 1000)
        if ok:
            self.store.finish(task_id, "done", message)
            self.store.event(agent.name, "info", f"{row['title']}: {message}", task_id)
            return "done"
        state = self.store.retry_later(task_id, message, config.LIMITS["max_attempts"])
        self.store.event(agent.name, "warn", f"{row['title']}: {message} ({elapsed} ms)",
                         task_id)
        return "failed"

    # ------------------------------------------------------------------ cycles
    def once(self, only: list[str] | None = None) -> CycleResult:
        # a crash mid-task leaves the row 'running' forever; reclaim it, but only when
        # no daemon owns it
        if not daemon_running():
            self.store.resume_running()
        observed = self.observe(only)
        result = self.act(only)
        result.observed = observed
        for name in getattr(self, "last_ran", []):
            run_id = self.store.start_run(name)
            self.store.finish_run(run_id, observed, result.done, result.failed,
                                  result.summary())
        if result.observed or result.done or result.failed or result.waiting:
            self.store.event("runner", "info", f"cycle: {result.summary()}")
        self.prune()
        return result

    def prune(self, keep: int = 500) -> int:
        """Keep the queue readable: only the most recent finished tasks survive."""
        with self.store._conn() as connection:  # noqa: SLF001 - deliberate, same package
            cursor = connection.execute(
                "DELETE FROM tasks WHERE state IN ('done','failed','rejected') AND id NOT IN "
                "(SELECT id FROM tasks WHERE state IN ('done','failed','rejected') "
                "ORDER BY updated DESC LIMIT ?)", (keep,))
            connection.commit()
            return cursor.rowcount

    def run_forever(self, interval: int | None = None) -> None:
        interval = interval or config.LIMITS["tick_seconds"]
        resumed = self.store.resume_running()
        if resumed:
            self.store.event("runner", "warn",
                             f"resumed {resumed} task(s) left running by a previous exit")

        def stop(signum, frame):  # noqa: ARG001
            self._stop.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

        self.store.event("runner", "info", f"daemon started, tick {interval}s")
        while not self._stop.is_set():
            try:
                result = self.once()
                if result.observed or result.done or result.failed:
                    print(f"{datetime.now():%H:%M:%S}  {result.summary()}", flush=True)
            except Exception as exc:  # noqa: BLE001
                self.store.event("runner", "error", f"cycle crashed: {exc}")
                print(f"{datetime.now():%H:%M:%S}  cycle crashed: {exc}", flush=True)
            self._stop.wait(interval)
        self.store.event("runner", "info", "daemon stopped")


def _payload(row: dict) -> dict:
    import json

    try:
        return json.loads(row.get("payload") or "{}")
    except (ValueError, TypeError):
        return {}


def pid_file() -> Path:
    return config.ensure_dirs() / "pride.pid"


def write_pid() -> int:
    path = pid_file()
    path.write_text(str(__import__("os").getpid()))
    return int(path.read_text())


def read_pid() -> int | None:
    path = pid_file()
    if not path.exists():
        return None
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def daemon_running() -> bool:
    import os

    pid = read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
