"""Terminal rendering for Pride."""

from __future__ import annotations

import json
import sys

from . import __version__, config, policy as policymod
from . import agents as agentsmod
from .store import Store, age

try:
    from rich import box
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    RICH = True
except ModuleNotFoundError:  # pragma: no cover
    RICH = False

    class _Fake:
        def print(self, *args, **kwargs):
            print(*[str(a) for a in args])

    Console = _Fake  # type: ignore

console = Console(width=None if sys.stdout.isatty() else 118)
err_console = Console(stderr=True)

CAT = [
    r"   /\_/\     ",
    r"  ( o.o )    ",
    r"   > ^ <     ",
]

LEVEL_STYLE = {
    "info": Text("info", style="dim"),
    "warn": Text("warn", style="bold #f0c45a"),
    "error": Text("error", style="bold #ff5c5c"),
}
STATE_STYLE = {
    "done": Text("done", style="#8bd67a"),
    "queued": Text("queued", style="dim"),
    "running": Text("running", style="#c9f24d"),
    "failed": Text("failed", style="bold #ff5c5c"),
    "rejected": Text("rejected", style="#ff9474"),
    "awaiting_approval": Text("waiting", style="bold #f0c45a"),
}


def banner(daemon: bool = False) -> None:
    if not RICH:
        console.print("pride — the pack")
        return
    mark = Text()
    for index, line in enumerate(CAT):
        if index:
            mark.append("\n")
        mark.append(line, style=config.ACCENT)
    side = Text()
    side.append("PRIDE", style="bold")
    side.append(f"  {__version__}\n", style="dim")
    side.append("five agents, one queue, no prompt\n", style="dim")
    side.append("daemon " + ("running" if daemon else "stopped"), style="dim")
    grid = Table.grid(padding=(0, 3))
    grid.add_row(mark, side)
    console.print()
    console.print(grid)
    console.print()


def head(text: str, extra: str = "") -> None:
    if RICH:
        line = Text(text.upper(), style="bold")
        if extra:
            line.append(f"  {extra}", style="dim")
        console.print(line)
    else:
        console.print(f"{text.upper()}  {extra}")
    console.print()


def level(value: str):
    return LEVEL_STYLE.get(value, Text(value)) if RICH else value


def state(value: str):
    return STATE_STYLE.get(value, Text(value)) if RICH else value


def agents_table(store: Store, policy: policymod.Policy) -> None:
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False)
    for heading in ("AGENT", "DOES", "CADENCE", "LAST RUN", "STATE"):
        table.add_column(heading, overflow="fold")
    for agent in agentsmod.AGENTS.values():
        cadence = policy.cadence.get(agent.name) or config.CADENCE.get(agent.name, 3600)
        decision = policy.decide(agent.action)
        table.add_row(
            agent.name, agent.description, f"{cadence // 60}m",
            age(store.last_run_for(agent.name)),
            state("running") if decision == policymod.ALLOW else Text(decision, style="bold #f0c45a"),
        )
    console.print(table)
    console.print()


def tasks_table(store: Store, state_filter: str | None = None, limit: int = 15) -> None:
    rows = store.tasks(state_filter, limit)
    if not rows:
        console.print("  [dim]queue is empty[/]" if RICH else "  queue is empty")
        console.print()
        return
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False)
    for heading in ("STATE", "AGENT", "TASK", "UPDATED", "RESULT"):
        table.add_column(heading, overflow="fold")
    for row in rows:
        table.add_row(state(row["state"]), row["agent"], row["title"][:34],
                      age(row["updated"]), (row["result"] or "")[:64])
    console.print(table)
    console.print()


def events_table(store: Store, limit: int = 20) -> None:
    rows = store.events(limit)
    if not rows:
        console.print("  [dim]nothing has happened yet[/]" if RICH else "  nothing yet")
        console.print()
        return
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False)
    for heading in ("WHEN", "LEVEL", "AGENT", "WHAT"):
        table.add_column(heading, overflow="fold")
    for row in rows:
        table.add_row(row["at"][11:19], level(row["level"]), row["agent"],
                      row["message"][:86])
    console.print(table)
    console.print()


def approvals_table(store: Store) -> int:
    rows = store.approvals()
    if not rows:
        return 0
    head("waiting for you", f"{len(rows)} action(s)")
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False)
    for heading in ("ID", "AGENT", "ACTION", "TASK", "ASKED"):
        table.add_column(heading, overflow="fold")
    for row in rows:
        table.add_row(row["id"], row["agent"] or "?", row["action"],
                      (row["title"] or "")[:40], age(row["requested"]))
    console.print(table)
    console.print("  [dim]pride approve <id> · pride reject <id>[/]" if RICH else "")
    console.print()
    return len(rows)


def counts_line(store: Store) -> None:
    counts = store.counts()
    parts = [f"{n} {state_}" for state_, n in sorted(counts.items())]
    console.print("  " + (" · ".join(parts) if parts else "no tasks yet"))
    console.print()


def policy_table(policy: policymod.Policy) -> None:
    head("policy", policy.source)
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False)
    for heading in ("ACTION", "DECISION", "MEANS"):
        table.add_column(heading, overflow="fold")
    for action, means in policymod.ACTIONS.items():
        decision = policy.decide(action)
        table.add_row(action,
                      Text(decision, style={"allow": "#8bd67a",
                                            "require_approval": "bold #f0c45a",
                                            "forbid": "bold #ff5c5c"}[decision]),
                      means)
    console.print(table)
    console.print()
    if policy.dry_run:
        console.print("  [bold #f0c45a]dry run is on[/] — nothing will be executed")
        console.print()
    console.print(f"  authorised targets: {', '.join(policy.authorized_targets) or 'none'}")
    console.print()


def emit_json(payload) -> None:
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


def fail(message: str) -> None:
    err_console.print(f"[bold #ff5c5c]x[/] {message}" if RICH else f"x {message}")


def ok(message: str) -> None:
    console.print(f"  [bold #8bd67a]✓[/] {message}" if RICH else f"  ok {message}")


def cat() -> None:
    console.print()
    for line in CAT:
        console.print(f"  [white]{line}[/]" if RICH else f"  {line}")
    console.print()
    console.print("  [dim]a group of lions is a pride; a group of cats is a clowder[/]"
                  if RICH else "  pride")
    console.print()
