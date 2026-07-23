"""Local runtime engine.

Loads the agent entrypoint, creates runs, executes handlers, and drives the
pause/resume lifecycle. There is no daemon: the engine operates on the
filesystem store, so a run started by ``rya events send`` can be resumed by a
separate ``rya approvals approve`` invocation.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import sys
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from ..approvals import ApprovalRejected, PausedForApproval
from ..errors import RyaError
from ..manifest.schema import Manifest
from ..models.registry import ModelRegistry, default_registry as default_models
from ..sdk.agent import Agent, _DEFINED_AGENTS
from ..sdk.context import Event, RuntimeContext
from ..store import Store, now_iso
from ..tools.registry import ToolRegistry, default_registry as default_tools


@dataclass
class Job:
    id: str
    handler: str
    payload: dict


def _run_coro(coro):
    """Run a coroutine to completion whether or not we're already in an event loop.

    The CLI calls the engine from sync code (no loop), but the MCP server and the
    control-plane API call it from *inside* a running loop, where ``asyncio.run``
    raises. In that case we run the coroutine on a fresh loop in a worker thread
    and propagate its result/exception (including PausedForApproval).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict = {}

    def worker():
        try:
            box["result"] = asyncio.run(coro)
        except BaseException as exc:  # propagate PausedForApproval / errors
            box["error"] = exc

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")


def load_agent(manifest: Manifest, project_root: Path) -> Agent:
    """Import the manifest entrypoint and return the agent it defines."""
    entry = (project_root / manifest.entrypoint).resolve()
    if not entry.is_file():
        raise RyaError(
            "E_ENTRYPOINT_NOT_FOUND",
            f"Entrypoint '{manifest.entrypoint}' not found at {entry}.",
            hint="Fix `entrypoint:` in rya.agent.yaml or create the file.",
        )

    # Make the project importable for the entrypoint's own relative imports.
    for p in (str(project_root), str(entry.parent)):
        if p not in sys.path:
            sys.path.insert(0, p)

    _DEFINED_AGENTS.clear()
    mod_name = f"rya_user_agent_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(mod_name, entry)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RyaError("E_ENTRYPOINT_NOT_FOUND", f"Could not load entrypoint {entry}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # surface import-time errors with a stable code
        raise RyaError(
            "E_RUNTIME",
            f"Failed to import entrypoint {manifest.entrypoint}: {exc}",
            hint="Fix the import/syntax error in the agent module.",
        )

    if not _DEFINED_AGENTS:
        raise RyaError(
            "E_AGENT_NOT_DEFINED",
            f"Entrypoint '{manifest.entrypoint}' did not call define_agent().",
            hint="Add `agent = define_agent()` at module scope in your entrypoint.",
        )

    agent = _DEFINED_AGENTS[-1]
    if agent.name is None:
        agent.name = manifest.name
    return agent


class Engine:
    def __init__(
        self,
        manifest: Manifest,
        agent: Agent,
        store: Store,
        project_root: Path,
        tools: Optional[ToolRegistry] = None,
        models: Optional[ModelRegistry] = None,
    ) -> None:
        self.manifest = manifest
        self.agent = agent
        self.store = store
        self.project_root = project_root
        self.tools = tools or default_tools()
        self.models = models or default_models()
        self.store.ensure()

    # ---- run creation --------------------------------------------------
    def make_event(self, type: str, payload: dict, source: str = "manual") -> dict:
        return {
            "id": "evt_" + uuid.uuid4().hex[:12],
            "type": type,
            "source": source,
            "agentId": self.manifest.name,
            "payload": payload,
            "createdAt": now_iso(),
        }

    def _new_run(self, trigger: str, event: Optional[dict], job: Optional[dict] = None,
                 parent_run_id: Optional[str] = None) -> dict:
        run = {
            "id": self.store.new_run_id(),
            "agent": self.manifest.name,
            "agentVersion": self.manifest.version,
            "trigger": trigger,
            "status": "running",
            "event": event,
            "job": job,
            "journal": {},
            "trace": [],
            "pendingApproval": None,
            "error": None,
            "scheduledJobs": [],
            "parentRunId": parent_run_id,
            "createdAt": now_iso(),
        }
        run["trace"].append({
            "seq": 0, "ts": now_iso(), "kind": "run.started",
            "label": trigger, "data": {"event": event, "job": job},
        })
        self.store.save_run(run)
        return run

    # ---- execution -----------------------------------------------------
    def run_event(self, type: str, payload: dict, source: str = "manual", identity=None,
                  on_trace=None, on_token=None, on_ui=None) -> dict:
        handler = self.agent.event_handler()
        if handler is None:
            raise RyaError(
                "E_HANDLER_NOT_FOUND",
                "No @agent.on_event handler is registered.",
                hint="Decorate a handler with @agent.on_event in your entrypoint.",
            )
        event = self.make_event(type, payload, source)
        run = self._new_run("event", event)
        if identity is not None:
            run["identity"] = identity.to_dict() if hasattr(identity, "to_dict") else identity
        return self._execute(run, handler, Event.from_dict(event), identity=identity,
                             on_trace=on_trace, on_token=on_token, on_ui=on_ui)

    def run_job(self, job_id: str) -> dict:
        job = self.store.get_job(job_id)
        if job is None:
            raise RyaError("E_JOB_NOT_FOUND", f"Job '{job_id}' not found.")
        handler = self.agent.job_handler(job["handler"])
        if handler is None:
            raise RyaError(
                "E_HANDLER_NOT_FOUND",
                f"No @agent.job('{job['handler']}') handler registered.",
                hint=f"Decorate a handler with @agent.job('{job['handler']}').",
            )
        run = self._new_run("job", None, job=job, parent_run_id=job.get("parentRunId"))
        job["status"] = "running"
        job["resultRunId"] = run["id"]
        self.store.save_job(job)
        result = self._execute(run, handler, Job(job["id"], job["handler"], job.get("payload", {})))

        # Retry with exponential backoff (pattern from openclaw): on failure,
        # increment attempts and reschedule with a future runAt until maxAttempts,
        # then mark failed. `rya jobs run --due` picks up jobs whose runAt is past.
        if result["status"] == "completed":
            job["status"] = "done"
            job["lastError"] = None
        elif result["status"] == "waiting_approval":
            # A human gate is a pause, not a failure - retrying would duplicate
            # the approval request. The approval resume finishes the run.
            job["status"] = "waiting_approval"
            job["lastError"] = None
        else:
            job["attempts"] = job.get("attempts", 0) + 1
            job["lastError"] = (result.get("error") or {}).get("message")
            if job["attempts"] < job.get("maxAttempts", 3):
                backoff_s = min(30, 2 ** (job["attempts"] - 1))  # 1,2,4,…,30s
                job["runAt"] = (datetime.now(timezone.utc) + timedelta(seconds=backoff_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
                job["status"] = "pending"
            else:
                job["status"] = "failed"
        self.store.save_job(job)
        # Job-group fan-in: exactly-once on_complete when the whole group is done.
        if job.get("groupId") and job["status"] in ("done", "failed"):
            try:
                out = self.store.group_job_done(job["groupId"], success=(job["status"] == "done"))
                if out and out.get("fire"):
                    oc = out["onComplete"]
                    self.store.create_job(oc.get("parentRunId"), oc["handler"],
                                          oc.get("payload", {}), now_iso())
            except Exception:  # never let group bookkeeping kill the worker
                pass
        return result

    def due_jobs(self) -> list:
        """Pending jobs whose runAt is in the past (ready to run)."""
        now = now_iso()
        return [j for j in self.store.list_jobs("pending") if (j.get("runAt") or "") <= now]

    def _clone(self) -> "Engine":
        """A sibling engine on a FRESH store connection - required for running
        jobs in threads (a psycopg connection must not be shared across them)."""
        st = self.store
        if hasattr(st, "dsn"):
            from ..store_postgres import PostgresStore
            new_store = PostgresStore(st.dsn, st.workspace_id, getattr(st, "user_id", None))
        else:
            new_store = type(st)(st.root)
            new_store.ensure()
        return Engine(self.manifest, self.agent, new_store, self.project_root,
                      self.tools, self.models)

    def work_once(self, concurrency: int = 1) -> list:
        """Claim and run every currently-due job. Safe to run from N workers
        concurrently — claim_due_job is atomic (Postgres SKIP LOCKED).

        ``concurrency`` > 1 runs jobs in parallel threads, each on a cloned
        engine with its own store connection; claims are serialized under a
        lock so the file backend stays correct too."""
        if concurrency <= 1:
            ran = []
            while True:
                job = self.store.claim_due_job()
                if not job:
                    break
                result = self.run_job(job["id"])
                ran.append({"jobId": job["id"], "runId": result["id"], "status": result["status"]})
            return ran

        results: list = []
        claim_lock = threading.Lock()
        res_lock = threading.Lock()

        def loop():
            eng = self._clone()
            try:
                while True:
                    with claim_lock:
                        job = eng.store.claim_due_job()
                    if not job:
                        return
                    result = eng.run_job(job["id"])
                    with res_lock:
                        results.append({"jobId": job["id"], "runId": result["id"],
                                        "status": result["status"]})
            finally:
                if hasattr(eng.store, "close"):
                    eng.store.close()

        threads = [threading.Thread(target=loop) for _ in range(concurrency)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        return results

    def dead_letter(self) -> list:
        """Jobs that exhausted their retries (the dead-letter queue)."""
        return self.store.list_jobs("failed")

    def retry_job(self, job_id: str) -> dict:
        """Requeue a dead-lettered job: reset attempts and make it due now."""
        job = self.store.get_job(job_id)
        if job is None:
            raise RyaError("E_JOB_NOT_FOUND", f"Job '{job_id}' not found.")
        if job.get("status") != "failed":
            raise RyaError("E_VALIDATION", f"Job '{job_id}' is '{job.get('status')}', not failed.",
                           hint="Only dead-lettered (failed) jobs can be retried.")
        job["attempts"] = 0
        job["status"] = "pending"
        job["runAt"] = now_iso()
        self.store.save_job(job)
        return job

    def run_cron(self, trigger_id: str) -> dict:
        trigger = next((t for t in self.manifest.triggers if t.id == trigger_id), None)
        if trigger is None:
            raise RyaError("E_HANDLER_NOT_FOUND", f"No trigger '{trigger_id}' in manifest.")
        handler = self.agent.cron_handler(trigger_id) or self.agent.job_handler(trigger.handler)
        if handler is None:
            raise RyaError(
                "E_HANDLER_NOT_FOUND",
                f"No handler registered for trigger '{trigger_id}' (handler '{trigger.handler}').",
                hint=f"Decorate a handler with @agent.cron('{trigger_id}') or @agent.job('{trigger.handler}').",
            )
        event = self.make_event(f"cron.{trigger_id}", {}, source="cron")
        run = self._new_run("cron", event)
        return self._execute(run, handler, Event.from_dict(event))

    def _execute(self, run: dict, handler, arg, identity=None, on_trace=None, on_token=None, on_ui=None) -> dict:
        ctx = RuntimeContext(
            store=self.store,
            manifest=self.manifest,
            run=run,
            tools=self.tools,
            models=self.models,
            project_root=self.project_root,
            identity=identity,
            agent=self.agent,
            on_trace=on_trace,
            on_token=on_token,
            on_ui=on_ui,
        )

        async def invoke():
            res = handler(ctx, arg)
            if inspect.isawaitable(res):
                return await res
            return res

        timeout = self.manifest.timeout_seconds

        async def invoke_guarded():
            if timeout:
                return await asyncio.wait_for(invoke(), timeout)
            return await invoke()

        out_box = {}
        try:
            out_box["value"] = _run_coro(invoke_guarded())
        except PausedForApproval as p:
            run["status"] = "waiting_approval"
            run["pendingApproval"] = p.approval_id
        except (asyncio.TimeoutError, TimeoutError):
            run["status"] = "failed"
            run["error"] = {"code": "E_TIMEOUT", "message": f"run exceeded {timeout}s timeout"}
            ctx._trace("run.failed", "E_TIMEOUT", {"timeoutSeconds": timeout})
        except ApprovalRejected as r:
            run["status"] = "rejected"
            run["error"] = {"code": "E_APPROVAL_REJECTED", "approvalId": r.approval_id}
            ctx._trace("run.rejected", "approval rejected", {"approvalId": r.approval_id})
        except RyaError as e:
            run["status"] = "failed"
            run["error"] = e.to_dict()["error"]
            ctx._trace("run.failed", e.code, {"message": e.message, "hint": e.hint})
        except Exception as e:  # pragma: no cover - defensive
            run["status"] = "failed"
            run["error"] = {"code": "E_RUNTIME", "message": str(e)}
            ctx._trace("run.failed", "E_RUNTIME", {"message": str(e)})
        else:
            run["status"] = "completed"
            run["pendingApproval"] = None
            # The handler's return value is part of the run record: the console
            # and API consumers read it as run.output (serialized default=str).
            run["output"] = out_box.get("value")
            ctx._trace("run.completed", "ok", {})

        self.store.save_run(run)

        # Export the finished run to any configured observability backend
        # (Langfuse / OTLP / webhook). Best-effort: never let export break a run.
        if run["status"] in ("completed", "failed", "rejected"):
            try:
                from ..observability.export import export_run
                from ..sdk.context import load_env
                export_run(run, load_env(self.project_root))
            except Exception:
                pass
        return run

    # ---- approvals -----------------------------------------------------
    def _find_approval_entry(self, run: dict, approval_id: str):
        for entry in run["journal"].values():
            if entry.get("kind") == "approval" and entry.get("result", {}).get("approvalId") == approval_id:
                return entry
        return None

    def _execute_action(self, action: dict):
        """Run a human-approved action through the real seam where one exists:
        a channel send goes to the real provider (Resend/Slack when configured),
        an HTTP tool makes a real request (with its scoped credential injected and
        the Action Guard applied), and only otherwise falls back to the registry."""
        tool = (action or {}).get("tool")
        if not tool:
            return None
        inp = action.get("input", {})
        # 1. Channel send (email.send / slack.send / <channel>.send) → real seam.
        parts = tool.split(".")
        channel = parts[0]
        is_channel = len(parts) == 2 and parts[1] == "send" and (
            channel in ("email", "slack", "webhook")
            or any(getattr(c, "type", None) == channel for c in self.manifest.channels))
        if is_channel:
            from ..providers.channels import send as channel_send
            from ..sdk.context import load_env
            return channel_send(channel, inp, load_env(self.project_root))
        # 2. HTTP tool declared with a url → real request, scoped-credential-injected.
        decl = next((t for t in self.manifest.tools if t.id == tool), None)
        url = getattr(decl, "url", None) if decl is not None else None
        if url:
            from ..sdk.context import _http_tool
            secret = None
            provider = getattr(decl, "provider", None)
            if provider and hasattr(self.store, "get_connection"):
                conn = self.store.get_connection(provider)
                secret = conn.get("secret") if conn else None
            return _http_tool(url, inp, auth_secret=secret)
        # 3. The agent's own @agent.tool implementation (async or sync) - this
        # is the normal case for in-process tools like a gated DB write.
        fn = self.agent.tool_handler(tool) if hasattr(self.agent, "tool_handler") else None
        if fn is not None:
            out = fn(inp)
            if inspect.iscoroutine(out):
                out = asyncio.run(out)
            return out
        # 4. Registry fallback (mock in the local slice).
        spec = self.tools.get(tool)
        return spec.fn(inp) if spec is not None else None

    def approve(self, approval_id: str, on_trace=None, on_token=None, on_ui=None) -> dict:
        approval = self.store.get_approval(approval_id)
        if approval is None:
            raise RyaError("E_APPROVAL_NOT_FOUND", f"Approval '{approval_id}' not found.")
        if approval["status"] != "pending":
            raise RyaError(
                "E_APPROVAL_NOT_PENDING",
                f"Approval '{approval_id}' is '{approval['status']}', not pending.",
                hint="Only pending approvals can be approved.",
            )
        run = self.store.get_run(approval["runId"])
        if run is None:
            raise RyaError("E_RUN_NOT_FOUND", f"Run '{approval['runId']}' not found.")
        if run["status"] != "waiting_approval":
            raise RyaError(
                "E_RUN_NOT_PAUSED",
                f"Run '{run['id']}' is '{run['status']}', not waiting_approval.",
            )

        # Execute the embedded action (a tool call) now that a human approved it,
        # through the REAL seam where one exists (channel send / HTTP tool), not
        # just the mock registry.
        action = approval.get("action") or {}
        action_result = self._execute_action(action)

        approval["status"] = "approved"
        approval["resolvedAt"] = now_iso()
        approval["actionResult"] = action_result
        self.store.save_approval(approval)

        entry = self._find_approval_entry(run, approval_id)
        if entry is not None:
            entry["status"] = "approved"
            entry["result"] = {
                "approvalId": approval_id,
                "status": "approved",
                "actionResult": action_result,
            }
        run["trace"].append({
            "seq": len(run["trace"]), "ts": now_iso(), "kind": "approval.approved",
            "label": approval["title"], "data": {"approvalId": approval_id, "actionResult": action_result},
        })
        run["status"] = "running"
        run["pendingApproval"] = None
        self.store.save_run(run)

        # Resume by replaying the handler against the now-resolved journal,
        # restoring the run's identity so per-user scoping holds on resume.
        # Memoized (pre-approval) steps neither re-trace nor re-stream, so the
        # relays only carry the POST-approval continuation.
        # A run paused inside a JOB must resume through its job handler with its
        # Job argument - resuming a job through the event handler replays the
        # wrong function against the journal.
        if run.get("trigger") == "job" and run.get("job"):
            j = run["job"]
            handler = self.agent.job_handler(j["handler"])
            arg = Job(j["id"], j["handler"], j.get("payload", {}))
        else:
            handler = self.agent.event_handler()
            arg = Event.from_dict(run["event"]) if run.get("event") else None
        identity = None
        if run.get("identity"):
            from ..auth import Identity
            ident = run["identity"]
            identity = Identity(sub=ident["sub"], claims=ident)
        return self._execute(run, handler, arg, identity=identity,
                             on_trace=on_trace, on_token=on_token, on_ui=on_ui)

    def reject(self, approval_id: str) -> dict:
        approval = self.store.get_approval(approval_id)
        if approval is None:
            raise RyaError("E_APPROVAL_NOT_FOUND", f"Approval '{approval_id}' not found.")
        if approval["status"] != "pending":
            raise RyaError(
                "E_APPROVAL_NOT_PENDING",
                f"Approval '{approval_id}' is '{approval['status']}', not pending.",
            )
        run = self.store.get_run(approval["runId"])
        if run is None:
            raise RyaError("E_RUN_NOT_FOUND", f"Run '{approval['runId']}' not found.")

        approval["status"] = "rejected"
        approval["resolvedAt"] = now_iso()
        self.store.save_approval(approval)

        entry = self._find_approval_entry(run, approval_id)
        if entry is not None:
            entry["status"] = "rejected"
        run["status"] = "rejected"
        run["error"] = {"code": "E_APPROVAL_REJECTED", "approvalId": approval_id}
        run["pendingApproval"] = None
        run["trace"].append({
            "seq": len(run["trace"]), "ts": now_iso(), "kind": "approval.rejected",
            "label": approval["title"], "data": {"approvalId": approval_id},
        })
        self.store.save_run(run)
        return run
