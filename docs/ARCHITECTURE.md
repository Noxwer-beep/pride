# Architecture

Five agents, one queue, one policy file, one SQLite database. That is the whole system.

```
                    ┌──────────────── pride daemon (systemd --user) ────────────────┐
                    │                                                              │
   every 60s tick   │   observe ──► queue ──► policy gate ──► act (4 workers)      │
                    │      │           │            │               │              │
                    └──────┼───────────┼────────────┼───────────────┼──────────────┘
                           │           │            │               │
        ┌──────────────────┴──┐   ┌────┴─────┐  ┌───┴────────┐  ┌───┴──────────────┐
        │ sentinel  (30m)     │   │  tasks   │  │ allow      │  │ vantage  ./vantage│
        │ watcher   (15m)     │   │  events  │  │ ask        │  │ warden   ./warden │
        │ auditor   (1h)      │   │  runs    │  │ forbid     │  │ tests    unittest │
        │ scout     (6h)      │   │ approvals│  └────────────┘  │ http     urllib   │
        │ scribe    (24h)     │   │  pride.db│                  │ vault    markdown │
        └─────────────────────┘   └──────────┘                  └───────────────────┘
```

## Phases, and why they are separate

**observe** asks every due agent what needs doing and writes it to the queue.
**act** takes work off the queue and executes it, in parallel, behind the policy gate.

The split buys three things:

1. A crash while acting never loses the observation — the queue still holds the work.
2. "What is it doing right now" has exactly one answer, and it is a database row.
3. A policy decision cannot be bypassed by an agent, because the gate sits between the
   queue and the outcome, not inside the agent.

## The data model

| table | holds | written by |
|---|---|---|
| `tasks` | one row per unit of work: agent, kind, payload, state, attempts, result, fingerprint | observe, act |
| `events` | append-only log: at, agent, level, message — what you read at 08:00 | everything |
| `runs` | one row per agent pass, so "did it run" has an answer | the runner |
| `approvals` | actions parked until a human decides | the policy gate |

`fingerprint` is what keeps the queue sane: an open task with the same fingerprint is
never queued twice, which is how a 30-minute site probe does not become eight duplicate
rows an hour.

## The policy gate

```
        ┌─────────┐  allow   ┌──────────┐
task ──►│ policy  ├─────────►│ execute  │
        │ .decide │  ask     └──────────┘
        │(action) ├─────────► approvals (parked, state = awaiting_approval)
        │         │ forbid  ┌───────────────────────────────────────┐
        └─────────┘────────►│ rejected + a warn line in the log      │
                            └───────────────────────────────────────┘
```

Rules, in order:

1. An action that is not in `policy.ACTIONS` is **forbidden** — an agent cannot invent
   a capability.
2. `forbid` beats `allow`, even if a name appears in both lists.
3. `require_approval` parks the task; the decision is stored, so an approved task runs on
   the next pass and a rejected one never runs.
4. A missing or unparseable policy file falls back to the *built-in* defaults, never to
   "allow everything".

## Why the agents are deterministic

An agent here is `observe()` and `act()`. Neither is allowed to be creative:

| agent | observe | act |
|---|---|---|
| sentinel | one task per URL | HTTP GET, record status/latency/TLS days |
| watcher | one task | run `warden check`, regex the verdict, name failures |
| auditor | one task per repo | run the suite, parse "Ran N tests" and OK/FAILED |
| scout | one task per authorised target, if the last scan is older than 6h | run `vantage scan --strategy quick` |
| scribe | one task per day | render the event log into a vault note |

The payoff is testability: every agent is tested against a real local HTTP server, a real
shell script standing in for the tool, and a temp vault. No mocking of the thing being
tested.

## Failure handling

- **Claimed before running.** A task goes `queued → running` before the work starts.
- **Crash recovery.** On start (and on every manual cycle when no daemon is running),
  anything still `running` is returned to the queue.
- **Three attempts.** After the third failure the task becomes `failed` with the reason,
  which is visible in `pride tasks --state failed`.
- **Silent cycles stay silent.** A cycle only logs when it changed something, so an empty
  log means "nothing to report" — a statement you can trust.

## Deliberate omissions

No LLM. No self-modification. No remediation. No network egress except the read-only
site probes. Those are not missing features; they are the boundary that lets this run
unattended on a machine with my tools on it.
