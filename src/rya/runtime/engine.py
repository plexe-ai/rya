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
        version: Optional[dict] = None,
        environment: Optional[str] = None,
        broker=None,
        config=None,
    ) -> None:
        self.manifest = manifest
        self.agent = agent
        self.store = store
        # D18: the mediated IO client, threaded straight through to every ctx this
        # engine builds. The engine itself never calls it — it holds no credentials
        # to replace — but it is the one object that constructs `RuntimeContext`, so
        # it is where the posture has to be carried. `config` is here for the same
        # reason: under mediation the claimer resolves the RunConfig and hands down a
        # STRIPPED copy (no api keys, no secrets), and letting RuntimeContext resolve
        # its own from `os.environ` inside the sandbox would undo that.
        self.broker = broker
        self.config = config
        self.project_root = project_root
        self.tools = tools or default_tools()
        self.models = models or default_models()
        # D12: the immutable version this process serves. A worker is one process
        # per (workspace, agent, version), so the version is a property of the
        # PROCESS, not something re-resolved per run — which is exactly what makes
        # a run's pin sound. None = the working tree (`rya dev`, single-tenant
        # `rya serve`); those runs are unpinned and never block a retire.
        self.version = version or None
        self.environment = environment
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
        # §11.12: the workspace quota is an ADMISSION check — refuse to start a
        # run rather than aborting one in flight. A run killed mid-journal could
        # never replay to a terminal state, which trades a durability guarantee
        # for a billing nicety. Overshoot is therefore bounded by one run.
        # Sub-runs (parent_run_id) are exempt: the parent was already admitted, and
        # refusing its continuation would strand the parent's journal.
        if parent_run_id is None:
            from ..quotas import require_admission
            require_admission(self.store, kind="run")

        v = self.version or {}
        run = {
            "id": self.store.new_run_id(),
            "agent": self.manifest.name,
            # D12: the code identity a replay must be checked against. `agentVersion`
            # survives as a human LABEL only — it is the author-typed
            # `manifest.version` string, with no hash, immutability or uniqueness,
            # and nothing branches on it. `versionId`/`bundleHash` are the identity.
            "agentVersion": v.get("manifestVersion") or self.manifest.version,
            "versionId": v.get("id"),
            "bundleHash": v.get("bundleHash"),
            "sdkVersion": v.get("sdkVersion"),
            "environment": self.environment,
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

    def _adopt_run(self, run_id: str) -> Optional[dict]:
        """Take over a run the CONTROL PLANE created and queued (D21).

        `POST /agents/{id}/events` no longer executes; it writes a `queued` run —
        pinned to the promoted version, admitted against the quota — and enqueues
        it. The caller therefore holds a run id before any worker has touched the
        work, which is what makes `GET /runs/{id}` answer immediately and the pin
        auditable. This is where a worker picks that record up instead of minting
        a second one.

        The journal and trace are reset, not resumed. A queued run has never
        executed, and a *reclaimed* one is being re-run from scratch by contract
        (see the `turns` module docstring: crash-retry re-runs the handler fresh).
        The durable-resume case is an approval pause, which goes through
        `approve`, never here.
        """
        run = self.store.get_run(run_id)
        if run is None:
            return None
        run["status"] = "running"
        run["journal"] = {}
        run["trace"] = []
        run["error"] = None
        run["pendingApproval"] = None
        return run

    # ---- execution -----------------------------------------------------
    def run_event(self, type: str, payload: dict, source: str = "manual", identity=None,
                  on_trace=None, on_token=None, on_ui=None,
                  run_id: Optional[str] = None) -> dict:
        handler = self.agent.event_handler()
        if handler is None:
            raise RyaError(
                "E_HANDLER_NOT_FOUND",
                "No @agent.on_event handler is registered.",
                hint="Decorate a handler with @agent.on_event in your entrypoint.",
            )
        run = self._adopt_run(run_id) if run_id else None
        if run is not None:
            # The control plane's event, not a fresh one: re-minting it would give
            # the run a different event id on every reclaim, and `run["event"]` is
            # what a replay and the trace's `run.started` step are written against.
            event = run.get("event") or self.make_event(type, payload, source)
            run["event"] = event
            run["trace"].append({
                "seq": 0, "ts": now_iso(), "kind": "run.started",
                "label": "event", "data": {"event": event, "job": None},
            })
            self.store.save_run(run)
        else:
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
                                          oc.get("payload", {}), now_iso(),
                                          agent=self.manifest.name)
            except Exception:  # never let group bookkeeping kill the worker
                pass
        return result

    def due_jobs(self) -> list:
        """Pending jobs whose runAt is in the past **and that this agent may run**.

        The agent filter is D22 reaching the `jobs` primitive. Without it a worker
        counted every sibling agent's due job as its own claimable depth, so it
        never went idle (the scale-to-zero failure ``Worker.queue_depth`` is about)
        and it claimed work whose handler it does not have.
        """
        now = now_iso()
        mine = getattr(self.manifest, "name", None)
        return [j for j in self.store.list_jobs("pending")
                if (j.get("runAt") or "") <= now
                and (j.get("agent") is None or mine is None or j["agent"] == mine)]

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
                      self.tools, self.models, self.version, self.environment)

    def work_once(self, concurrency: int = 1) -> list:
        """Claim and run every currently-due job. Safe to run from N workers
        concurrently — claim_due_job is atomic (Postgres SKIP LOCKED).

        ``concurrency`` > 1 runs jobs in parallel threads, each on a cloned
        engine with its own store connection; claims are serialized under a
        lock so the file backend stays correct too."""
        mine = getattr(self.manifest, "name", None)
        if concurrency <= 1:
            ran = []
            while True:
                job = self.store.claim_due_job(mine)
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
                        job = eng.store.claim_due_job(mine)
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

    def _context_for(self, run: dict, identity=None, on_trace=None, on_token=None,
                     on_ui=None) -> RuntimeContext:
        """Build the run's ``ctx``. One constructor for both the handler path and
        the approval path, so the approval path cannot drift out of governance."""
        return RuntimeContext(
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
            broker=self.broker,
            config=self.config,
        )

    def _execute(self, run: dict, handler, arg, identity=None, on_trace=None, on_token=None, on_ui=None) -> dict:
        ctx = self._context_for(run, identity, on_trace, on_token, on_ui)

        # Record who this run was for, so observability backends can attribute it
        # (Langfuse `userId`) without each agent having to plumb it through.
        if identity is not None and getattr(identity, "sub", None):
            run["userId"] = identity.sub

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
            if e.code == "E_CONNECTION_EXPIRED":
                # A distinct, non-generic outcome: the connection expired mid-turn,
                # so the run needs the user to reconnect (log in again) and retry —
                # not a bug: surface a clean reconnect prompt instead of a failure.
                run["status"] = "needs_reconnect"
                run["error"] = e.to_dict()["error"]
                ctx._trace("run.needs_reconnect", e.code, {"message": e.message, "hint": e.hint})
            else:
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
        if run["status"] in ("completed", "failed", "rejected", "needs_reconnect"):
            try:
                from ..observability.export import export_run
                from ..sdk.context import load_env
                export_run(run, load_env(self.project_root))
            except Exception:
                pass
        return run

    # ---- approvals -----------------------------------------------------
    @staticmethod
    def _identity_of(run: dict):
        """Rehydrate the verified Identity a run was started under, so a resume
        (and the approved action it executes) keeps per-user scoping."""
        ident = run.get("identity")
        if not ident:
            return None
        from ..auth import Identity
        return Identity(sub=ident["sub"], claims=ident)

    def _find_approval_entry(self, run: dict, approval_id: str):
        for entry in run["journal"].values():
            if entry.get("kind") == "approval" and entry.get("result", {}).get("approvalId") == approval_id:
                return entry
        return None

    def _execute_action(self, run: dict, action: dict, identity=None):
        """Run a human-approved action through the GOVERNED path.

        This used to be a second, parallel dispatch implementation — no
        permission check, no arg-pin resolution, no ``guard.scrub``, and a
        credential lookup that omitted the ``owner`` argument so it was not even
        per-user scoped. PLATFORM_DESIGN §7 names that as one of the two rows
        "not true of today's code" and §11.1 makes closing it the first work.

        Everything is now resolved by ``ctx.tools.prepare(..., approved=True)``:
        the same permission resolution (a kill switch flipped while the approval
        was pending still wins), the same pins, the same scope intersection, the
        same scrub. A channel send goes through the same outbound gate.
        """
        tool = (action or {}).get("tool")
        if not tool:
            return None
        inp = action.get("input", {})
        ctx = self._context_for(run, identity)

        # A channel send (email.send / slack.send / <channel>.send) is not a
        # manifest tool, so it has its own governed seam rather than the tool one.
        parts = tool.split(".")
        channel = parts[0]
        is_channel = len(parts) == 2 and parts[1] == "send" and (
            channel in ("email", "slack", "webhook")
            or any(getattr(c, "type", None) == channel for c in self.manifest.channels))
        if is_channel:
            return _run_coro(ctx.channels.send_approved(channel, inp))
        return _run_coro(ctx.tools.call_approved(tool, inp))

    def approve(self, approval_id: str, on_trace=None, on_token=None, on_ui=None,
                actor: Optional[dict] = None) -> dict:
        from ..turns import APPROVING

        approval = self.store.get_approval(approval_id)
        if approval is None:
            raise RyaError("E_APPROVAL_NOT_FOUND", f"Approval '{approval_id}' not found.")
        # `approving` is a pending approval whose DECISION the control plane has
        # already recorded and handed to this process (turns.enqueue_resume). It
        # is accepted here for exactly that reason and is not a state a caller can
        # reach any other way; `pending` remains the only entry point.
        if approval["status"] not in ("pending", APPROVING):
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

        # Restore the run's identity BEFORE executing the action, not just before
        # the resume: the approved action's scoped-credential resolution is
        # per-user (connection ∩ user scopes), and running it with identity=None
        # would silently fall through to a workspace-shared credential.
        identity = self._identity_of(run)

        # Execute the embedded action now that a human approved it — through the
        # governed path (§11.1), so permission, pins, scope intersection and the
        # id-secrecy scrub all apply exactly as they do to ctx.tools.call.
        action = approval.get("action") or {}
        action_result = self._execute_action(run, action, identity=identity)

        approval["status"] = "approved"
        approval["resolvedAt"] = now_iso()
        approval["resolvedBy"] = actor
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
            "label": approval["title"],
            "data": {"approvalId": approval_id, "actionResult": action_result, "actor": actor},
        })
        run["status"] = "running"
        run["pendingApproval"] = None
        self.store.save_run(run)

        # Resume by replaying the handler against the now-resolved journal, under
        # the identity restored above so per-user scoping holds on resume.
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
        return self._execute(run, handler, arg, identity=identity,
                             on_trace=on_trace, on_token=on_token, on_ui=on_ui)

    def reject(self, approval_id: str, actor: Optional[dict] = None) -> dict:
        """Delegates to ``turns.reject_approval``.

        Rejecting executes no action and replays no handler — it marks two records
        and appends a trace step — so unlike ``approve`` it never needed an
        engine. Moving the body out is what lets the api keep answering rejections
        itself while handing approvals to a worker (D21).
        """
        from ..turns import reject_approval

        return reject_approval(self.store, approval_id, actor=actor)

