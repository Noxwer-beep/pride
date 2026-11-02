"""The five agents.

Each one is a pair of functions and a policy action, which is the whole trick: an agent
that cannot describe its work as "observe, then act" is an agent that will surprise you
at 03:00.

    sentinel  are the sites up, and is TLS about to expire
    watcher   are the homelab services still answering
    auditor   do the five repos still pass their tests
    scout     has the authorised target changed since the last scan
    scribe    what happened in the last day, written down
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config, policy as policymod
from .store import Store, Task, age


@dataclass
class Context:
    store: Store
    policy: policymod.Policy
    dry_run: bool = False

    def log(self, agent: str, level: str, message: str, task_id: str = "") -> None:
        self.store.event(agent, level, message, task_id)


# ---------------------------------------------------------------- helpers

def run(cmd: list[str], cwd: Path | None = None, timeout: int = 120,
        env: dict | None = None) -> tuple[int, str]:
    """Run a command, never raise, always return something to log."""
    import os

    merged = {**os.environ, **(env or {})}
    try:
        proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True,
                              text=True, timeout=timeout, env=merged)
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except (OSError, ValueError) as exc:
        return 127, str(exc)


def probe(url: str, timeout: int = 15) -> dict:
    """A read-only HTTP probe. No cookies, no auth, one request."""
    started = time.perf_counter()
    request = urllib.request.Request(
        url, method="GET",
        headers={"User-Agent": "pride/1.0 (+homelab sentinel)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(2048)
            return {
                "ok": 200 <= response.status < 400,
                "status": response.status,
                "ms": round((time.perf_counter() - started) * 1000),
                "bytes": len(body),
                "tls_days": tls_days(url),
            }
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "ms": 0, "bytes": 0,
                "error": f"HTTP {exc.code}", "tls_days": tls_days(url)}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "status": 0, "ms": 0, "bytes": 0,
                "error": str(exc)[:120], "tls_days": None}


def tls_days(url: str) -> int | None:
    """Days until the certificate expires. None for plain HTTP or a failed handshake."""
    if not url.startswith("https://"):
        return None
    host = url.split("://", 1)[1].split("/", 1)[0].split(":")[0]
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=10) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                expires = tls.getpeercert().get("notAfter", "")
        when = datetime.strptime(expires, "%b %d %H:%M:%S %Y %Z")
        return (when - datetime.utcnow()).days
    except (OSError, ssl.SSLError, ValueError, KeyError):
        return None


# ---------------------------------------------------------------- agents

class Agent:
    name = "agent"
    description = ""
    action = ""          # the policy action this agent's work needs
    timeout = config.LIMITS["task_timeout_s"]

    def observe(self, ctx: Context) -> list[Task]:
        raise NotImplementedError

    def act(self, ctx: Context, task: Task) -> tuple[bool, str]:
        raise NotImplementedError


class Sentinel(Agent):
    """The public sites: status, latency, and when the certificate expires."""

    name = "sentinel"
    description = "watch the five sites and three key pages"
    action = "http.probe"

    def observe(self, ctx: Context) -> list[Task]:
        targets = {**config.SITES, **config.KEY_PAGES}
        return [
            Task(agent=self.name, kind="probe", title=f"probe {name}",
                 payload={"name": name, "url": url}, priority=3,
                 fingerprint=f"probe:{url}")
            for name, url in targets.items()
        ]

    def act(self, ctx: Context, task: Task) -> tuple[bool, str]:
        url = task.payload["url"]
        result = probe(url)
        if not result["ok"]:
            return False, f"{url} → {result.get('error') or result['status']}"
        days = result.get("tls_days")
        suffix = f", tls {days}d" if days is not None else ""
        if days is not None and days < 14:
            return True, f"{url} {result['status']} in {result['ms']}ms — certificate expires in {days}d"
        return True, f"{url} {result['status']} in {result['ms']}ms{suffix}"


class Watcher(Agent):
    """The homelab: run Warden and compare the verdict with the last one."""

    name = "watcher"
    description = "check the service inventory"
    action = "warden.check"

    def observe(self, ctx: Context) -> list[Task]:
        return [Task(agent=self.name, kind="check", title="warden check",
                     priority=2, fingerprint="warden:check")]

    def act(self, ctx: Context, task: Task) -> tuple[bool, str]:
        warden = config.PROJECTS / "warden" / "warden"
        if not warden.exists():
            return False, "warden launcher not found"
        inventory = Path.home() / "lab" / "warden.toml"
        argv = [str(warden), "check", "--plain", "--no-save"]
        if inventory.exists():
            argv += ["--inventory", str(inventory)]
        code, output = run(argv, cwd=warden.parent, timeout=self.timeout,
                           env={"WARDEN_DATA_DIR": str(Path.home() / "lab" / ".data")})
        match = re.search(r"(\d+) services?\s*·\s*(\d+) ok(?:\s*·\s*(\d+) warn)?"
                          r"(?:\s*·\s*(\d+) fail)?", output)
        if not match:
            return False, f"could not read warden output (exit {code})"
        services, ok = int(match.group(1)), int(match.group(2))
        warn = int(match.group(3) or 0)
        fail = int(match.group(4) or 0)
        summary = f"{services} services · {ok} ok · {warn} warn · {fail} fail"
        if fail:
            failing = re.findall(r"^(\S+)\s+\S+\s+✗ fail", output, re.MULTILINE)
            return False, summary + (f" — {', '.join(failing[:4])}" if failing else "")
        return True, summary


class Auditor(Agent):
    """The repos: does the test suite still pass, and how many tests is that."""

    name = "auditor"
    description = "run the test suites"
    action = "tests.run"
    timeout = 300

    def observe(self, ctx: Context) -> list[Task]:
        tasks = []
        for repo in config.REPOS:
            if (config.PROJECTS / repo / "tests").is_dir():
                tasks.append(Task(agent=self.name, kind="tests", title=f"tests {repo}",
                                  payload={"repo": repo}, priority=6,
                                  fingerprint=f"tests:{repo}"))
        return tasks

    def act(self, ctx: Context, task: Task) -> tuple[bool, str]:
        repo = task.payload["repo"]
        directory = config.PROJECTS / repo
        code, output = run(config.REPOS[repo], cwd=directory, timeout=self.timeout)
        ran = re.search(r"Ran (\d+) tests?", output)
        count = ran.group(1) if ran else "?"
        if code == 0 and re.search(r"^OK", output, re.MULTILINE):
            return True, f"{repo}: {count} tests pass"
        failures = output.count("FAIL:") + output.count("ERROR:")
        tail = output.strip().splitlines()[-1][:80] if output.strip() else f"exit {code}"
        return False, f"{repo}: {failures} failing of {count} — {tail}"


class Scout(Agent):
    """The authorised target: a quick scan, but only if the policy and scope agree."""

    name = "scout"
    description = "quick scan of the authorised target"
    action = "vantage.scan"
    timeout = 900

    def observe(self, ctx: Context) -> list[Task]:
        last = ctx.store.last_done(self.name, "scan")
        if last and _hours(last) < config.LIMITS["scan_min_gap_hours"]:
            return []
        tasks = []
        for target in ctx.policy.authorized_targets:
            if not ctx.policy.may_scan(target):
                continue
            tasks.append(Task(agent=self.name, kind="scan", title=f"scan {target}",
                              payload={"target": target}, priority=7,
                              fingerprint=f"scan:{target}"))
        return tasks

    def act(self, ctx: Context, task: Task) -> tuple[bool, str]:
        target = task.payload["target"]
        scope = config.scope_file()
        if not scope.exists():
            return False, f"no scope file at {scope} — refusing to scan"
        scout = config.PROJECTS / "vantage" / "vantage"
        if not scout.exists():
            return False, "vantage launcher not found (scope is fine)"
        code, output = run(
            [str(scout), "scan", target, "--scope", str(scope), "--authorized",
             "--strategy", "quick", "--plain"],
            cwd=scout.parent, timeout=self.timeout)
        findings = (re.search(r"findings\s+(\d+)", output)
                    or re.search(r"(\d+)\s+findings?", output)
                    or re.search(r"(\d+)\s+finding\(s\)", output))
        ports = re.search(r"(\d+)\s+open ports?", output)
        if code != 0:
            return False, f"scan exited {code}: {output.strip().splitlines()[-1][:90]}"
        port_count = int(ports.group(1)) if ports else -1
        if port_count == 0:
            # a box with nothing open and a box that is switched off look identical in
            # the numbers, and only one of them is good news
            return False, (f"{target}: 0 open ports — unreachable or filtered, "
                           "not a clean result. Check the host is up.")
        return True, (f"{target}: {port_count if port_count >= 0 else '?'} ports, "
                      f"{findings.group(1) if findings else '?'} findings")


class Scribe(Agent):
    """The record: one note a day, built from the event log."""

    name = "scribe"
    description = "write the daily digest into the vault"
    action = "vault.write"

    def observe(self, ctx: Context) -> list[Task]:
        return [Task(agent=self.name, kind="digest", title="daily digest",
                     priority=9, fingerprint=f"digest:{date.today().isoformat()}")]

    def act(self, ctx: Context, task: Task) -> tuple[bool, str]:
        events = ctx.store.events(limit=500)
        cutoff = datetime.now() - timedelta(days=1)
        today = [e for e in events
                 if (dt := _parse(e["at"])) is not None and dt >= cutoff]
        problems = [e for e in today if e["level"] in ("warn", "error")]
        by_agent: dict[str, int] = {}
        for event in today:
            by_agent[event["agent"]] = by_agent.get(event["agent"], 0) + 1

        vault = config.VAULT / "Engineering" / "Operations"
        path = vault / f"Pride digest {date.today().isoformat()}.md"

        lines = [
            "---",
            f"date: {date.today().isoformat()}",
            "tags: [ops, pride, digest]",
            "---",
            "",
            f"# Pride digest — {date.today().isoformat()}",
            "",
            "Written by the pack, unattended. Counts cover the last 24 hours.",
            "",
            "| agent | events |",
            "|---|---|",
        ]
        for agent, count in sorted(by_agent.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {agent} | {count} |")
        lines += ["", f"**{len(problems)} problem(s)**", ""]
        if problems:
            lines += ["| at | agent | what |", "|---|---|---|"]
            for event in problems[:40]:
                lines.append(f"| {event['at'][11:16]} | {event['agent']} | "
                             f"{event['message'][:120]} |")
            lines.append("")
        else:
            lines += ["Nothing needed attention."]
            lines.append("")
        lines += ["## Everything else", ""]
        for event in [e for e in today if e["level"] == "info"][:40]:
            lines.append(f"- `{event['at'][11:16]}` **{event['agent']}** {event['message'][:150]}")
        lines.append("")

        if ctx.dry_run:
            return True, f"would write {path.name} ({len(today)} events)"
        vault.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines))
        return True, f"wrote {path.name} ({len(today)} events, {len(problems)} problems)"


def _hours(stamp: str) -> float:
    parsed = _parse(stamp)
    if parsed is None:
        return 9_999.0
    return (datetime.now() - parsed).total_seconds() / 3600


def _parse(stamp: str):
    try:
        return datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None


AGENTS: dict[str, Agent] = {
    agent.name: agent for agent in (Sentinel(), Watcher(), Auditor(), Scout(), Scribe())
}


def catalogue() -> list[dict]:
    return [{"name": a.name, "description": a.description, "action": a.action,
             "cadence": config.CADENCE.get(a.name, 3600)}
            for a in AGENTS.values()]
