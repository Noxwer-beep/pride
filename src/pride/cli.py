"""Pride — a supervised pack of agents that runs on a cadence, not on a prompt.

    pride                     what the pack is doing
    pride cycle               one pass: observe, then act
    pride daemon              run until stopped (systemd user service)
    pride approvals           what is waiting for a human
    pride policy              what it may do unattended

Exit codes: 0 ok · 1 error · 2 usage · 3 missing dependency · 4 something is failing
"""

from __future__ import annotations

import argparse
import sys

from . import __version__, config, policy as policymod, ui
from . import agents as agentsmod
from . import runner as runnermod
from .store import Store


def _store() -> Store:
    return Store()


def _policy() -> policymod.Policy:
    return policymod.load()


def cmd_status(args: argparse.Namespace) -> int:
    store, policy = _store(), _policy()
    if args.json:
        ui.emit_json({
            "daemon": runnermod.daemon_running(),
            "agents": agentsmod.catalogue(),
            "policy": policy.to_dict(),
            "counts": store.counts(),
            "tasks": [dict(row) for row in store.tasks(limit=20)],
            "events": [dict(row) for row in store.events(limit=20)],
            "approvals": [dict(row) for row in store.approvals()],
        })
        return 0

    ui.banner(runnermod.daemon_running())
    ui.agents_table(store, policy)
    waiting = ui.approvals_table(store)
    ui.head("queue")
    ui.counts_line(store)
    ui.tasks_table(store, limit=args.top)
    ui.head("recent", "what the pack did")
    ui.events_table(store, limit=args.top)
    if waiting:
        return 4
    return 0


def cmd_cycle(args: argparse.Namespace) -> int:
    policy = _policy()
    runner = runnermod.Runner(_store(), policy, dry_run=args.dry_run,
                              workers=args.workers)
    result = runner.once(only=args.only)
    if args.json:
        ui.emit_json({"observed": result.observed, "done": result.done,
                      "failed": result.failed, "waiting": result.waiting})
        return 4 if result.failed else 0
    ui.ok(f"cycle: {result.summary()}")
    if result.failed:
        return 4
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    if runnermod.daemon_running() and not args.force:
        ui.fail(f"a daemon is already running (pid {runnermod.read_pid()}); --force to start another")
        return 2
    runner = runnermod.Runner(_store(), _policy(), dry_run=args.dry_run)
    ui.banner(True)
    ui.console.print(f"  tick {args.interval or config.LIMITS['tick_seconds']}s · "
                     f"dry run {'on' if runner.dry_run else 'off'}")
    ui.console.print()
    runnermod.write_pid()
    try:
        runner.run_forever(args.interval)
    finally:
        runnermod.pid_file().unlink(missing_ok=True)
    return 0


def cmd_agents(args: argparse.Namespace) -> int:
    store, policy = _store(), _policy()
    if args.json:
        ui.emit_json(agentsmod.catalogue())
        return 0
    ui.head("the pack", f"{len(agentsmod.AGENTS)} agents")
    ui.agents_table(store, policy)
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    store = _store()
    if args.json:
        ui.emit_json([dict(row) for row in store.tasks(args.state, args.top)])
        return 0
    ui.head("queue", args.state or "all states")
    ui.counts_line(store)
    ui.tasks_table(store, args.state, args.top)
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    store = _store()
    if args.json:
        ui.emit_json([dict(row) for row in store.events(args.top, args.level)])
        return 0
    ui.head("events", f"last {args.top}")
    ui.events_table(store, args.top)
    return 0


def cmd_approvals(args: argparse.Namespace) -> int:
    store = _store()
    if args.json:
        ui.emit_json([dict(row) for row in store.approvals(open_only=not args.all)])
        return 0
    count = ui.approvals_table(store)
    if not count:
        ui.ok("nothing waiting")
    return 0


def cmd_decide(args: argparse.Namespace, decision: str) -> int:
    store = _store()
    if store.decide(args.id, decision, getattr(args, "note", "") or ""):
        ui.ok(f"{args.id} → {decision}")
        return 0
    ui.fail(f"no open approval with id {args.id}")
    return 2


def cmd_policy(args: argparse.Namespace) -> int:
    policy = _policy()
    if args.json:
        ui.emit_json(policy.to_dict())
        return 0
    ui.policy_table(policy)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    path = config.policy_path()
    if path.exists() and not args.force:
        ui.fail(f"{path} already exists (--force to overwrite)")
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(policymod.DEFAULT_POLICY)
    ui.ok(f"wrote {path}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    store, policy = _store(), _policy()
    ui.head("doctor")
    rows = [
        ("pride", __version__),
        ("data", str(store.path)),
        ("policy", policy.source),
        ("scope file", str(config.scope_file()) if config.scope_file().exists()
         else "missing — scout will refuse to scan"),
        ("daemon", f"running (pid {runnermod.read_pid()})" if runnermod.daemon_running()
         else "stopped"),
        ("flask", "yes" if _module("flask") else "missing — no dashboard"),
        ("agents", ", ".join(agentsmod.AGENTS)),
    ]
    for key, value in rows:
        ui.console.print(f"  {key:10} {value}")
    ui.console.print()
    return 0


def _module(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


def cmd_web(args: argparse.Namespace) -> int:
    try:
        from .web.app import serve
    except ModuleNotFoundError:
        ui.fail("the dashboard needs flask:  pip install flask")
        return 3
    serve(host=args.host, port=args.port, refresh=args.refresh)
    return 0


def cmd_cat(args: argparse.Namespace) -> int:
    ui.cat()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pride",
        description="A supervised pack of agents: watch, test, scan, write.",
    )
    parser.add_argument("--version", action="version", version=f"pride {__version__}")
    sub = parser.add_subparsers(dest="command")

    status = sub.add_parser("status", help="what the pack is doing (default)")
    status.add_argument("--json", action="store_true")
    status.add_argument("--top", type=int, default=10)
    status.set_defaults(func=cmd_status)

    cycle = sub.add_parser("cycle", help="one pass: observe, then act")
    cycle.add_argument("--only", nargs="+", choices=list(agentsmod.AGENTS))
    cycle.add_argument("--dry-run", action="store_true")
    cycle.add_argument("--workers", type=int, default=4)
    cycle.add_argument("--json", action="store_true")
    cycle.set_defaults(func=cmd_cycle)

    daemon = sub.add_parser("daemon", help="run until stopped")
    daemon.add_argument("--interval", type=int)
    daemon.add_argument("--dry-run", action="store_true")
    daemon.add_argument("--force", action="store_true")
    daemon.set_defaults(func=cmd_daemon)

    agents = sub.add_parser("agents", help="what the pack is made of")
    agents.add_argument("--json", action="store_true")
    agents.set_defaults(func=cmd_agents)

    tasks = sub.add_parser("tasks", help="the queue")
    tasks.add_argument("--state", choices=["queued", "running", "done", "failed",
                                           "rejected", "awaiting_approval"])
    tasks.add_argument("--top", type=int, default=20)
    tasks.add_argument("--json", action="store_true")
    tasks.set_defaults(func=cmd_tasks)

    events = sub.add_parser("events", help="what happened")
    events.add_argument("--top", type=int, default=25)
    events.add_argument("--level", choices=["info", "warn", "error"])
    events.add_argument("--json", action="store_true")
    events.set_defaults(func=cmd_events)

    approvals = sub.add_parser("approvals", help="what is waiting for a human")
    approvals.add_argument("--all", action="store_true")
    approvals.add_argument("--json", action="store_true")
    approvals.set_defaults(func=cmd_approvals)

    approve = sub.add_parser("approve", help="let a queued action run")
    approve.add_argument("id")
    approve.add_argument("--note", default="")
    approve.set_defaults(func=lambda a: cmd_decide(a, "approve"))

    reject = sub.add_parser("reject", help="throw a queued action away")
    reject.add_argument("id")
    reject.add_argument("--note", default="")
    reject.set_defaults(func=lambda a: cmd_decide(a, "reject"))

    policy = sub.add_parser("policy", help="what may run unattended")
    policy.add_argument("--json", action="store_true")
    policy.set_defaults(func=cmd_policy)

    init = sub.add_parser("init", help="write a starter policy file")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    doctor = sub.add_parser("doctor", help="is everything wired up")
    doctor.set_defaults(func=cmd_doctor)

    web = sub.add_parser("web", help="the dashboard")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8790)
    web.add_argument("--refresh", type=int, default=20)
    web.set_defaults(func=cmd_web)

    cat = sub.add_parser("cat", help="the cat")
    cat.set_defaults(func=cmd_cat)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["status"]
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 0
    try:
        return func(args)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
