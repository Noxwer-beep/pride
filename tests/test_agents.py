"""The agents and the loop, against real servers, real scripts and a temp vault."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pride import agents as agentsmod  # noqa: E402
from pride import config, policy as policymod, runner as runnermod  # noqa: E402
from pride.store import Store, Task  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib naming
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the test output clean
        pass


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pride-agents-"))
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.vault = self.tmp / "vault"
        self.env = mock.patch.dict(os.environ, {
            "PRIDE_DATA_DIR": str(self.tmp / "data"),
            "PRIDE_CONFIG_DIR": str(self.tmp / "config"),
        })
        self.env.start()
        self.patches = [
            mock.patch.object(config, "PROJECTS", self.projects),
            mock.patch.object(config, "VAULT", self.vault),
            mock.patch.object(config, "scope_file", lambda: self.tmp / "scope.json"),
        ]
        for patch in self.patches:
            patch.start()
        self.store = Store()
        self.policy = policymod.Policy()
        self.ctx = agentsmod.Context(store=self.store, policy=self.policy)

    def tearDown(self) -> None:
        for patch in self.patches:
            patch.stop()
        self.env.stop()


class Probe(Base):
    def test_real_client(self) -> None:
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{self.server.server_port}/"
            result = agentsmod.probe(url)
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], 200)
            self.assertIsNone(result["tls_days"])       # plain http has no certificate
        finally:
            self.server.shutdown()
            self.server.server_close()

    def test_closed_port_is_a_failure_not_a_crash(self) -> None:
        result = agentsmod.probe("http://127.0.0.1:9/", timeout=2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 0)

    def test_tls_days_ignores_plain_http(self) -> None:
        self.assertIsNone(agentsmod.tls_days("http://example.com"))


class SentinelAgent(Base):
    def test_observe_covers_sites_and_key_pages(self) -> None:
        tasks = agentsmod.Sentinel().observe(self.ctx)
        self.assertEqual(len(tasks), len(config.SITES) + len(config.KEY_PAGES))
        self.assertTrue(all(t.agent == "sentinel" for t in tasks))
        self.assertTrue(all(t.fingerprint.startswith("probe:") for t in tasks))

    def test_act_reports_a_dead_url(self) -> None:
        ok, message = agentsmod.Sentinel().act(
            self.ctx, Task(agent="sentinel", kind="probe", title="x",
                           payload={"url": "http://127.0.0.1:9/"}))
        self.assertFalse(ok)
        self.assertIn("127.0.0.1:9", message)


class WatcherAgent(Base):
    def install_warden(self, output: str, exit_code: int = 0) -> None:
        folder = self.projects / "warden"
        folder.mkdir(parents=True, exist_ok=True)
        launcher = folder / "warden"
        launcher.write_text(f"#!/bin/sh\ncat <<'OUT'\n{output}\nOUT\nexit {exit_code}\n")
        launcher.chmod(0o755)

    def test_reads_the_summary(self) -> None:
        self.install_warden("16 services · 13 ok · 1 warn · 2 fail  4.0s")
        ok, message = agentsmod.Watcher().act(self.ctx, Task(agent="watcher", kind="check",
                                                             title="warden check"))
        self.assertFalse(ok)                      # failures present
        self.assertIn("16 services · 13 ok · 1 warn · 2 fail", message)

    def test_healthy_homelab_passes(self) -> None:
        self.install_warden("16 services · 16 ok  2.1s")
        ok, message = agentsmod.Watcher().act(self.ctx, Task(agent="watcher", kind="check",
                                                             title="warden check"))
        self.assertTrue(ok)
        self.assertIn("16 ok", message)

    def test_inventory_is_passed_when_it_exists(self) -> None:
        seen = self.tmp / "seen.txt"
        folder = self.projects / "warden"
        folder.mkdir(parents=True, exist_ok=True)
        launcher = folder / "warden"
        launcher.write_text(f"#!/bin/sh\necho \"$@\" > {seen}\n"
                            "echo '16 services · 16 ok  1.2s'\n")
        launcher.chmod(0o755)
        inventory = self.tmp / "warden.toml"
        inventory.write_text("# empty inventory\n")
        with mock.patch.object(config, "WARDEN_INVENTORY", inventory), \
             mock.patch.object(config, "WARDEN_DATA", self.tmp / "warden-data"):
            ok, _ = agentsmod.Watcher().act(self.ctx, Task(agent="watcher", kind="check",
                                                           title="warden check"))
        self.assertIn("--inventory", seen.read_text())
        self.assertIn(str(inventory), seen.read_text())

    def test_unparseable_output_is_a_failure(self) -> None:
        self.install_warden("something else entirely")
        ok, message = agentsmod.Watcher().act(self.ctx, Task(agent="watcher", kind="check",
                                                             title="warden check"))
        self.assertFalse(ok)
        self.assertIn("could not read", message)

    def test_missing_launcher(self) -> None:
        ok, message = agentsmod.Watcher().act(self.ctx, Task(agent="watcher", kind="check",
                                                             title="warden check"))
        self.assertFalse(ok)
        self.assertIn("not found", message)


class AuditorAgent(Base):
    def make_repo(self, name: str, body: str) -> None:
        repo = self.projects / name
        (repo / "tests").mkdir(parents=True, exist_ok=True)
        (repo / "tests" / "__init__.py").write_text("")
        (repo / "tests" / "test_x.py").write_text(body)

    def test_passing_suite(self) -> None:
        self.make_repo("clowder", "import unittest\nclass T(unittest.TestCase):\n"
                                  "    def test_ok(self):\n        self.assertTrue(True)\n")
        ok, message = agentsmod.Auditor().act(self.ctx, Task(agent="auditor", kind="tests",
                                                             title="tests clowder",
                                                             payload={"repo": "clowder"}))
        self.assertTrue(ok, message)
        self.assertIn("1 tests pass", message)

    def test_failing_suite(self) -> None:
        self.make_repo("bastet", "import unittest\nclass T(unittest.TestCase):\n"
                                 "    def test_bad(self):\n        self.assertEqual(1, 2)\n")
        ok, message = agentsmod.Auditor().act(self.ctx, Task(agent="auditor", kind="tests",
                                                             title="tests bastet",
                                                             payload={"repo": "bastet"}))
        self.assertFalse(ok)
        self.assertIn("failing", message)

    def test_observe_only_picks_existing_repos(self) -> None:
        self.make_repo("clowder", "")
        titles = [t.title for t in agentsmod.Auditor().observe(self.ctx)]
        self.assertIn("tests clowder", titles)
        self.assertNotIn("tests vantage", titles)


class ScoutAgent(Base):
    def test_refuses_without_a_scope_file(self) -> None:
        ok, message = agentsmod.Scout().act(
            self.ctx, Task(agent="scout", kind="scan", title="scan",
                           payload={"target": "10.0.0.5"}))
        self.assertFalse(ok)
        self.assertIn("refusing to scan", message)

    def test_zero_ports_is_not_success(self) -> None:
        (self.tmp / "scope.json").write_text('{"networks": ["10.0.0.5/32"]}')
        folder = self.projects / "vantage"
        folder.mkdir(parents=True, exist_ok=True)
        launcher = folder / "vantage"
        launcher.write_text(
            "#!/bin/sh\n"
            "echo '  findings   none'\n"
            "echo '     hosts   1 up · 0 open ports'\n"
            "echo 'scan id 20260101-000000-aaaa'\n"
        )
        launcher.chmod(0o755)
        ok, message = agentsmod.Scout().act(
            self.ctx, Task(agent="scout", kind="scan", title="scan",
                           payload={"target": "10.0.0.5"}))
        self.assertFalse(ok)
        self.assertIn("unreachable or filtered", message)

    def test_a_real_result_is_success(self) -> None:
        (self.tmp / "scope.json").write_text('{"networks": ["10.0.0.5/32"]}')
        folder = self.projects / "vantage"
        folder.mkdir(parents=True, exist_ok=True)
        launcher = folder / "vantage"
        launcher.write_text(
            "#!/bin/sh\n"
            "echo '  findings   33'\n"
            "echo '     hosts   1 up · 23 open ports'\n"
        )
        launcher.chmod(0o755)
        ok, message = agentsmod.Scout().act(
            self.ctx, Task(agent="scout", kind="scan", title="scan",
                           payload={"target": "10.0.0.5"}))
        self.assertTrue(ok, message)
        self.assertIn("23 ports, 33 findings", message)

    def test_observe_needs_an_authorised_target(self) -> None:
        self.assertEqual(agentsmod.Scout().observe(self.ctx), [])
        self.policy.authorized_targets = ["10.0.0.5"]
        titles = [t.title for t in agentsmod.Scout().observe(self.ctx)]
        self.assertEqual(titles, ["scan 10.0.0.5"])


class ScribeAgent(Base):
    def test_writes_a_digest(self) -> None:
        self.store.event("sentinel", "info", "all sites fine")
        self.store.event("watcher", "warn", "target-ntp is down")
        ok, message = agentsmod.Scribe().act(self.ctx, Task(agent="scribe", kind="digest",
                                                            title="digest"))
        self.assertTrue(ok, message)
        notes = list((self.vault / "Engineering" / "Operations").glob("Pride digest*.md"))
        self.assertEqual(len(notes), 1)
        body = notes[0].read_text()
        self.assertIn("target-ntp is down", body)
        self.assertIn("**1 problem(s)**", body)

    def test_dry_run_writes_nothing(self) -> None:
        self.ctx.dry_run = True
        ok, message = agentsmod.Scribe().act(self.ctx, Task(agent="scribe", kind="digest",
                                                            title="digest"))
        self.assertTrue(ok)
        self.assertIn("would write", message)
        self.assertFalse((self.vault / "Engineering" / "Operations").exists())


class RunnerBehaviour(Base):
    def runner(self, **kwargs) -> runnermod.Runner:
        return runnermod.Runner(self.store, self.policy, **kwargs)

    def test_dry_run_reports_without_taking_work(self) -> None:
        self.store.add_task(Task(agent="sentinel", kind="probe", title="probe"))
        result = self.runner(dry_run=True).act()
        self.assertEqual(result.skipped, 1)
        self.assertEqual(self.store.counts().get("queued"), 1)

    def test_forbidden_action_is_refused_without_running(self) -> None:
        self.policy.forbid.add("http.probe")
        self.policy.allow.discard("http.probe")
        self.store.add_task(Task(agent="sentinel", kind="probe", title="probe",
                                 payload={"url": "http://127.0.0.1:9/"}))
        result = self.runner().act()
        self.assertEqual(result.skipped, 1)
        self.assertEqual(self.store.counts().get("rejected"), 1)

    def test_action_needing_approval_parks_the_task(self) -> None:
        self.policy.ask.add("http.probe")
        self.policy.allow.discard("http.probe")
        self.store.add_task(Task(agent="sentinel", kind="probe", title="probe",
                                 payload={"url": "http://127.0.0.1:9/"}))
        result = self.runner().act()
        self.assertEqual(result.waiting, 1)
        self.assertEqual(len(self.store.approvals()), 1)

    def test_approved_task_runs_on_the_next_pass(self) -> None:
        self.policy.ask.add("http.probe")
        self.policy.allow.discard("http.probe")
        task_id = self.store.add_task(Task(agent="sentinel", kind="probe", title="probe",
                                           payload={"url": "http://127.0.0.1:9/"}))
        self.runner().act()
        approval = self.store.approvals()[0]["id"]
        self.store.decide(approval, "approve")
        self.store.resume_running()
        self.runner().act()
        row = [t for t in self.store.tasks(limit=50) if t["id"] == task_id][0]
        # it ran and reported a verdict: the probe failed, so the task retries
        self.assertIn("127.0.0.1:9", row["result"])
        self.assertGreaterEqual(int(row["attempts"]), 2)

    def test_rejected_task_never_runs(self) -> None:
        self.policy.ask.add("http.probe")
        self.policy.allow.discard("http.probe")
        self.store.add_task(Task(agent="sentinel", kind="probe", title="probe",
                                 payload={"url": "http://127.0.0.1:9/"}))
        self.runner().act()
        self.store.decide(self.store.approvals()[0]["id"], "reject")
        self.runner().act()
        self.assertEqual(self.store.counts().get("rejected"), 1)

    def test_observe_deduplicates_across_cycles(self) -> None:
        first = self.runner().observe(only=["scribe"])
        second = self.runner().observe(only=["scribe"])
        self.assertEqual(first, 1)     # the day's digest is queued
        self.assertEqual(second, 0)    # the same one is not queued twice


class Cli(Base):
    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        from pride import cli, ui
        from rich.console import Console

        buffer = io.StringIO()
        fresh = Console(file=buffer, width=140, force_terminal=False)
        previous, previous_err = ui.console, ui.err_console
        ui.console = ui.err_console = fresh
        try:
            code = cli.main(argv)
        finally:
            ui.console, ui.err_console = previous, previous_err
        return code, buffer.getvalue()

    def test_status_json(self) -> None:
        from pride import cli

        buffer = io.StringIO()
        previous, sys.stdout = sys.stdout, buffer
        try:
            code = cli.main(["status", "--json"])
        finally:
            sys.stdout = previous
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(len(payload["agents"]), 5)
        self.assertIn("policy", payload)

    def test_agents_and_policy(self) -> None:
        code, output = self.run_cli(["agents"])
        self.assertEqual(code, 0)
        for name in ("sentinel", "watcher", "auditor", "scout", "scribe"):
            self.assertIn(name, output)
        code, output = self.run_cli(["policy"])
        self.assertEqual(code, 0)
        self.assertIn("git.push", output)

    def test_init_writes_a_policy(self) -> None:
        code, output = self.run_cli(["init"])
        self.assertEqual(code, 0)
        self.assertTrue(config.policy_path().exists())
        # running it again without --force refuses
        code, output = self.run_cli(["init"])
        self.assertEqual(code, 2)

    def test_approve_unknown_id(self) -> None:
        code, output = self.run_cli(["approve", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("no open approval", output)

    def test_cycle_dry_run(self) -> None:
        code, output = self.run_cli(["cycle", "--dry-run", "--only", "sentinel"])
        self.assertIn(code, (0, 4))


if __name__ == "__main__":
    unittest.main()
