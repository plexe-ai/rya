"""Trace export — ship completed run traces to an external observability backend.

Env-gated, best-effort (an export failure never fails the run):
- ``RYA_TRACE_WEBHOOK``  → POST the run summary to any URL (generic / self-hosted).
- ``LANGFUSE_HOST`` + ``LANGFUSE_PUBLIC_KEY`` + ``LANGFUSE_SECRET_KEY`` → push the
  run as a Langfuse trace + one event per step via the ingestion API.

The engine calls ``export_run`` once a run reaches a terminal status.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Optional

from .usage import run_usage


def _post(url: str, headers: dict, payload: dict, timeout: int = 10) -> int:
    req = urllib.request.Request(url, data=json.dumps(payload, default=str).encode(), method="POST",
                                 headers={"content-type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def run_summary(run: dict, env: Optional[dict] = None) -> dict:
    return {
        "runId": run["id"],
        "agent": run.get("agent"),
        "agentVersion": run.get("agentVersion"),
        "status": run.get("status"),
        "trigger": run.get("trigger"),
        "createdAt": run.get("createdAt"),
        "usage": run_usage(run, env),
        "error": run.get("error"),
        "trace": run.get("trace", []),
    }


def _langfuse(run: dict, env: dict) -> None:
    auth = base64.b64encode(f"{env['LANGFUSE_PUBLIC_KEY']}:{env['LANGFUSE_SECRET_KEY']}".encode()).decode()
    created = run.get("createdAt")
    batch = [{
        "id": f"{run['id']}-trace",
        "type": "trace-create",
        "timestamp": created,
        "body": {"id": run["id"], "name": run.get("agent"),
                 "metadata": {"status": run.get("status"), "usage": run_usage(run, env)}},
    }]
    for ev in run.get("trace", []):
        batch.append({
            "id": f"{run['id']}-{ev['seq']}",
            "type": "event-create",
            "timestamp": ev.get("ts", created),
            "body": {"id": f"{run['id']}-{ev['seq']}", "traceId": run["id"],
                     "name": ev.get("kind"), "metadata": ev.get("data")},
        })
    host = env["LANGFUSE_HOST"].rstrip("/")
    _post(f"{host}/api/public/ingestion", {"Authorization": f"Basic {auth}"}, {"batch": batch})


def export_run(run: dict, env: Optional[dict] = None) -> dict:
    env = env or os.environ
    results = {}
    url = env.get("RYA_TRACE_WEBHOOK")
    if url:
        try:
            _post(url, {}, run_summary(run, env))
            results["webhook"] = "sent"
        except Exception as e:  # never fail a run because export failed
            results["webhook"] = f"error: {e}"
    if env.get("LANGFUSE_HOST") and env.get("LANGFUSE_PUBLIC_KEY") and env.get("LANGFUSE_SECRET_KEY"):
        try:
            _langfuse(run, env)
            results["langfuse"] = "sent"
        except Exception as e:
            results["langfuse"] = f"error: {e}"
    return results
