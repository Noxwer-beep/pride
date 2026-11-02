"""What the pack may do without asking.

An unattended system is not dangerous because it is fast — it is dangerous because
nobody is reading. So every action an agent can take has a name, and each name is
declared in the policy file as one of:

    allow             run it now
    require_approval  queue it, tell me, wait
    forbid            never, and say so in the log

The default when a policy file is missing or an action is unlisted is `require_approval`,
because the safe default for a thing nobody is watching is to ask.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 has no tomllib
    tomllib = None  # type: ignore

from . import config

ALLOW, ASK, FORBID = "allow", "require_approval", "forbid"

#: The complete vocabulary. An agent cannot invent an action name.
ACTIONS = {
    "warden.check": "read service inventory and run the checks",
    "http.probe": "HEAD/GET a public URL",
    "tests.run": "run a repo's test suite",
    "vault.write": "append a note to the Obsidian vault",
    "vantage.scan": "scan an authorised target",
    "git.push": "push to a remote",
    "shell.any": "arbitrary shell (never allowed)",
}

DEFAULT = {
    ALLOW: ["warden.check", "http.probe", "tests.run", "vault.write"],
    ASK: ["vantage.scan"],
    FORBID: ["git.push", "shell.any"],
}


@dataclass
class Policy:
    allow: set[str] = field(default_factory=lambda: set(DEFAULT[ALLOW]))
    ask: set[str] = field(default_factory=lambda: set(DEFAULT[ASK]))
    forbid: set[str] = field(default_factory=lambda: set(DEFAULT[FORBID]))
    cadence: dict[str, int] = field(default_factory=dict)
    authorized_targets: list[str] = field(default_factory=list)
    dry_run: bool = False
    source: str = "built-in defaults"

    def decide(self, action: str) -> str:
        """allow · require_approval · forbid. Unlisted means ask."""
        if action in self.forbid or action not in ACTIONS:
            return FORBID
        if action in self.allow:
            return ALLOW
        if action in self.ask:
            return ASK
        return ASK

    def may_scan(self, target: str) -> bool:
        return target in self.authorized_targets

    def to_dict(self) -> dict:
        return {
            "allow": sorted(self.allow), "require_approval": sorted(self.ask),
            "forbid": sorted(self.forbid), "dry_run": self.dry_run,
            "authorized_targets": self.authorized_targets, "source": self.source,
        }


def load(path: Path | None = None) -> Policy:
    """Read policy.toml; fall back to the safe defaults, never to 'allow everything'."""
    policy = Policy()
    policy_path = Path(path) if path else config.policy_path()
    if not policy_path.exists():
        return policy
    if tomllib is None:  # pragma: no cover
        policy.source = "built-in defaults (no tomllib)"
        return policy
    try:
        payload = tomllib.loads(policy_path.read_text())
    except (OSError, ValueError) as exc:
        policy.source = f"built-in defaults ({exc.__class__.__name__} reading policy)"
        return policy

    actions = payload.get("actions") or {}
    policy.allow = set(actions.get("allow") or DEFAULT[ALLOW])
    policy.ask = set(actions.get("require_approval") or DEFAULT[ASK])
    policy.forbid = set(actions.get("forbid") or DEFAULT[FORBID])
    # a forbidden action cannot be re-allowed by also listing it under allow
    policy.allow -= policy.forbid

    policy.cadence = {k: int(v) for k, v in (payload.get("cadence") or {}).items()}
    policy.authorized_targets = [str(t) for t in (payload.get("authorized") or [])]
    policy.dry_run = bool(payload.get("dry_run", False))
    policy.source = str(policy_path)
    return policy


DEFAULT_POLICY = '''# Pride policy — what the pack may do while you are not watching.
# Every action is either allowed outright, queued for approval, or forbidden.
# Anything not listed here is treated as "ask", which is the point.

dry_run = false

# Scanning is only legal against these, and each must also be in the scope file.
authorized = ["10.10.20.101"]

[actions]
allow = [
  "warden.check",      # read the service inventory and check it
  "http.probe",        # HEAD/GET the public sites
  "tests.run",         # run a repo's test suite
  "vault.write",       # append to the Obsidian vault
  "bastet.case",       # open or update a case
]
require_approval = [
  "vantage.scan",      # scanning is loud: ask first
]
forbid = [
  "git.push",          # nothing leaves the machine without me
  "shell.any",         # no arbitrary command execution, ever
]

# Seconds per agent cycle. Omit an agent to keep its default.
[cadence]
sentinel = 1800
watcher = 900
auditor = 3600
scout = 21600
scribe = 86400

'''
