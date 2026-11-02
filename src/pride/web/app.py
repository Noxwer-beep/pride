"""The pack, in a browser.

Same data as `pride status`: agents with their cadence and last run, the queue, the
event log, and the approvals that are waiting. Re-read on every request — there is no
cache to go stale, and the daemon is writing to the same database.
"""

from __future__ import annotations

from flask import Flask, jsonify, render_template

from .. import __version__, agents as agentsmod, config, policy as policymod
from .. import runner as runnermod
from ..store import Store, age


def create_app(refresh: int = 20) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["REFRESH"] = refresh

    @app.context_processor
    def inject() -> dict:
        return {"version": __version__, "age": age, "accent": config.ACCENT}

    @app.get("/")
    def index():
        store, policy = Store(), policymod.load()
        agents = []
        for agent in agentsmod.AGENTS.values():
            cadence = policy.cadence.get(agent.name) or config.CADENCE.get(agent.name, 3600)
            agents.append({
                "name": agent.name, "description": agent.description,
                "action": agent.action, "decision": policy.decide(agent.action),
                "cadence": cadence, "last": age(store.last_run_for(agent.name)),
                "due": store.due(agent.name, cadence),
            })
        return render_template(
            "index.html",
            agents=agents,
            counts=store.counts(),
            tasks=store.tasks(limit=12),
            events=store.events(limit=25),
            approvals=store.approvals(),
            policy=policy,
            daemon=runnermod.daemon_running(),
            refresh=refresh,
        )

    @app.get("/api/status")
    def api_status():
        store, policy = Store(), policymod.load()
        return jsonify({
            "daemon": runnermod.daemon_running(),
            "agents": agentsmod.catalogue(),
            "policy": policy.to_dict(),
            "counts": store.counts(),
            "tasks": [dict(row) for row in store.tasks(limit=25)],
            "events": [dict(row) for row in store.events(limit=25)],
            "approvals": [dict(row) for row in store.approvals()],
        })

    @app.get("/api/health")
    def api_health():
        return jsonify({"ok": True, "version": __version__})

    return app


def serve(host: str = "127.0.0.1", port: int = 8790, refresh: int = 20) -> None:
    app = create_app(refresh)
    app.run(host=host, port=port, debug=False, use_reloader=False)
