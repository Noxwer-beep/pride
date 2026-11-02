"""Paths, targets and default cadences.

Pride runs unattended, so everything that decides *what it may touch* is data in a file,
not a constant buried in code: the sites it checks, the repos it tests, and the policy
that says which actions are allowed without asking.
"""

from __future__ import annotations

import os
from pathlib import Path

APP = "pride"
VERSION = "1.0.0"

HOME = Path.home()
PROJECTS = Path(os.environ.get("PRIDE_PROJECTS", HOME / "projects"))
VAULT = Path(os.environ.get("PRIDE_VAULT", HOME / "Obsidian"))
#: Where the homelab inventory and its state live. Config, not a constant inside an
#: agent — a test must not have to own the real ~/lab to exercise the watcher.
WARDEN_INVENTORY = Path(os.environ.get("PRIDE_WARDEN_INVENTORY", HOME / "lab" / "warden.toml"))
WARDEN_DATA = Path(os.environ.get("PRIDE_WARDEN_DATA", HOME / "lab" / ".data"))
ACCENT = "#c9f24d"

#: Sites the sentinel watches. Name → URL. These are read-only HEAD/GET probes.
SITES: dict[str, str] = {
    "portfolio": "https://noxwer-beep.github.io/portfolio/",
    "showcase": "https://noxwer-beep.github.io/showcase/",
    "blueprint": "https://noxwer-beep.github.io/blueprint/",
    "aurora": "https://noxwer-beep.github.io/aurora/",
    "ledger": "https://noxwer-beep.github.io/ledger/",
}

#: A few pages that must exist, not just the homes — these catch a broken build.
KEY_PAGES: dict[str, str] = {
    "portfolio/whisker": "https://noxwer-beep.github.io/portfolio/whisker.html",
    "portfolio/clowder": "https://noxwer-beep.github.io/portfolio/clowder.html",
    "portfolio/notes": "https://noxwer-beep.github.io/portfolio/metasploitable.html",
}

#: Repos whose test suites the auditor runs. Directory → command.
REPOS: dict[str, list[str]] = {
    "vantage": ["python3", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
    "warden": ["python3", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
    "bastet": ["python3", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
    "whisker": ["python3", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
    "clowder": ["python3", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
}

#: Cadence defaults, in seconds. The policy file can change them.
CADENCE = {
    "sentinel": 1800,    # 30 min — sites change when I push, not constantly
    "watcher": 900,      # 15 min — services
    "auditor": 3600,     # 1 h — test suites
    "scout": 21600,      # 6 h — scans are slow and loud
    "scribe": 86400,     # daily digest
}

#: Bounds that keep an unattended loop from becoming a load generator.
LIMITS = {
    "tick_seconds": 30,
    "task_timeout_s": 240,
    "max_tasks_per_cycle": 8,
    "max_attempts": 3,
    "scan_min_gap_hours": 6,
    "probe_workers": 8,
}


def data_dir() -> Path:
    env = os.environ.get("PRIDE_DATA_DIR")
    if env:
        return Path(env).expanduser()
    return HOME / ".local" / "share" / APP


def config_dir() -> Path:
    env = os.environ.get("PRIDE_CONFIG_DIR")
    if env:
        return Path(env).expanduser()
    return HOME / ".config" / APP


def policy_path() -> Path:
    return config_dir() / "policy.toml"


def ensure_dirs() -> Path:
    root = data_dir()
    (root / "logs").mkdir(parents=True, exist_ok=True)
    config_dir().mkdir(parents=True, exist_ok=True)
    return root


def scope_file() -> Path:
    """The authorised targets file, shared with Vantage's own scope format."""
    return HOME / "lab" / "scope.json"
