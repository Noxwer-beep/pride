"""The queue and the policy — the two things that must never be ambiguous."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pride import policy as policymod  # noqa: E402
from pride.store import Store, Task  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pride-test-"))
        self.env = mock.patch.dict(os.environ, {
            "PRIDE_DATA_DIR": str(self.tmp / "data"),
            "PRIDE_CONFIG_DIR": str(self.tmp / "config"),
        })
        self.env.start()
        self.store = Store()

    def tearDown(self) -> None:
        self.env.stop()


class Queue(Base):
    def test_add_and_claim(self) -> None:
        task_id = self.store.add_task(Task(agent="sentinel", kind="probe", title="probe a"))
        self.assertIsNotNone(task_id)
        rows = self.store.claim()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "running")
        self.assertEqual(rows[0]["attempts"], 1)

    def test_fingerprint_prevents_duplicates_while_open(self) -> None:
        first = self.store.add_task(Task(agent="sentinel", kind="probe", title="probe a",
                                         fingerprint="probe:a"))
        second = self.store.add_task(Task(agent="sentinel", kind="probe", title="probe a",
                                          fingerprint="probe:a"))
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_fingerprint_allows_a_new_task_after_the_first_finishes(self) -> None:
        task_id = self.store.add_task(Task(agent="sentinel", kind="probe", title="probe a",
                                           fingerprint="probe:a"))
        self.store.finish(task_id, "done", "ok")
        again = self.store.add_task(Task(agent="sentinel", kind="probe", title="probe a",
                                         fingerprint="probe:a"))
        self.assertIsNotNone(again)

    def test_priority_orders_the_queue(self) -> None:
        self.store.add_task(Task(agent="scribe", kind="digest", title="low", priority=9))
        self.store.add_task(Task(agent="watcher", kind="check", title="high", priority=1))
        rows = self.store.claim()
        self.assertEqual(rows[0]["title"], "high")

    def test_retry_gives_up_after_max_attempts(self) -> None:
        task_id = self.store.add_task(Task(agent="sentinel", kind="probe", title="x"))
        self.store.claim()
        self.assertEqual(self.store.retry_later(task_id, "boom", 3), "queued")
        self.store.claim()
        self.assertEqual(self.store.retry_later(task_id, "boom", 3), "queued")
        self.store.claim()
        self.assertEqual(self.store.retry_later(task_id, "boom", 3), "failed")

    def test_resume_running_recovers_from_a_crash(self) -> None:
        self.store.add_task(Task(agent="auditor", kind="tests", title="tests"))
        self.store.claim()
        self.assertEqual(self.store.resume_running(), 1)
        self.assertEqual(self.store.counts().get("queued"), 1)

    def test_peek_does_not_claim(self) -> None:
        self.store.add_task(Task(agent="sentinel", kind="probe", title="x"))
        self.assertEqual(len(self.store.peek()), 1)
        self.assertEqual(self.store.peek()[0]["state"], "queued")

    def test_events_are_append_only_and_ordered(self) -> None:
        self.store.event("sentinel", "info", "first")
        self.store.event("watcher", "warn", "second")
        rows = self.store.events(limit=5)
        self.assertEqual(rows[0]["message"], "second")
        self.assertEqual(rows[1]["message"], "first")

    def test_events_can_be_filtered_by_level(self) -> None:
        self.store.event("sentinel", "info", "quiet")
        self.store.event("sentinel", "error", "loud")
        rows = self.store.events(level="error")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message"], "loud")

    def test_due_respects_cadence(self) -> None:
        self.assertTrue(self.store.due("sentinel", 60))       # never ran
        run_id = self.store.start_run("sentinel")
        self.store.finish_run(run_id, 1, 1, 0, "ok")
        self.assertFalse(self.store.due("sentinel", 3600))    # just ran
        self.assertTrue(self.store.due("sentinel", 0))


class Approvals(Base):
    def park(self) -> tuple[str, str]:
        task_id = self.store.add_task(Task(agent="scout", kind="scan", title="scan lab"))
        approval = self.store.request_approval(
            Task(agent="scout", kind="scan", title="scan lab", id=task_id), "vantage.scan")
        return task_id, approval

    def test_requesting_moves_the_task_out_of_the_queue(self) -> None:
        task_id, _ = self.park()
        self.assertEqual(self.store.counts().get("awaiting_approval"), 1)
        self.assertEqual(self.store.approval_for(task_id), "")

    def test_approve_requeues(self) -> None:
        task_id, approval = self.park()
        self.assertTrue(self.store.decide(approval, "approve"))
        self.assertEqual(self.store.approval_for(task_id), "approve")
        self.assertEqual(self.store.counts().get("queued"), 1)

    def test_reject_marks_the_task(self) -> None:
        task_id, approval = self.park()
        self.store.decide(approval, "reject")
        self.assertEqual(self.store.approval_for(task_id), "reject")
        self.assertEqual(self.store.counts().get("rejected"), 1)

    def test_a_decision_cannot_be_made_twice(self) -> None:
        _, approval = self.park()
        self.assertTrue(self.store.decide(approval, "approve"))
        self.assertFalse(self.store.decide(approval, "reject"))

    def test_unknown_approval(self) -> None:
        self.assertFalse(self.store.decide("nope", "approve"))


class PolicyFile(unittest.TestCase):
    def write(self, text: str) -> Path:
        path = Path(tempfile.mkdtemp(prefix="pride-policy-")) / "policy.toml"
        path.write_text(text)
        return path

    def test_defaults_are_safe(self) -> None:
        policy = policymod.Policy()
        self.assertEqual(policy.decide("http.probe"), policymod.ALLOW)
        self.assertEqual(policy.decide("vantage.scan"), policymod.ASK)
        self.assertEqual(policy.decide("git.push"), policymod.FORBID)

    def test_unlisted_or_unknown_actions_ask_or_refuse(self) -> None:
        policy = policymod.Policy()
        self.assertEqual(policy.decide("something.new"), policymod.FORBID)
        policy.allow.add("something.new")
        self.assertEqual(policy.decide("something.new"), policymod.FORBID)  # not in ACTIONS

    def test_file_overrides(self) -> None:
        path = self.write('''
dry_run = true
authorized = ["10.0.0.5"]

[actions]
allow = ["http.probe"]
require_approval = ["tests.run"]
forbid = ["git.push"]

[cadence]
sentinel = 60
''')
        policy = policymod.load(path)
        self.assertTrue(policy.dry_run)
        self.assertEqual(policy.decide("http.probe"), policymod.ALLOW)
        self.assertEqual(policy.decide("tests.run"), policymod.ASK)
        self.assertEqual(policy.decide("warden.check"), policymod.ASK)   # unlisted
        self.assertEqual(policy.cadence["sentinel"], 60)
        self.assertEqual(policy.authorized_targets, ["10.0.0.5"])
        self.assertTrue(policy.may_scan("10.0.0.5"))
        self.assertFalse(policy.may_scan("10.0.0.6"))

    def test_forbid_beats_allow(self) -> None:
        path = self.write('''
[actions]
allow = ["git.push", "http.probe"]
forbid = ["git.push"]
''')
        policy = policymod.load(path)
        self.assertEqual(policy.decide("git.push"), policymod.FORBID)

    def test_broken_file_falls_back_to_defaults(self) -> None:
        path = self.write("this is not toml = = =")
        policy = policymod.load(path)
        self.assertIn("built-in defaults", policy.source)
        self.assertEqual(policy.decide("git.push"), policymod.FORBID)

    def test_missing_file_falls_back_to_defaults(self) -> None:
        policy = policymod.load(Path("/nonexistent/policy.toml"))
        self.assertEqual(policy.decide("vantage.scan"), policymod.ASK)


if __name__ == "__main__":
    unittest.main()
