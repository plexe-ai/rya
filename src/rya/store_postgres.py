"""Postgres-backed state store, workspace-scoped for multi-tenancy.

Same method surface as ``FileStore`` (duck-typed), so the engine/CLI/MCP/API are
unchanged. Two isolation mechanisms work together:

1. **App-layer scoping** — every query filters by ``workspace_id`` (always on;
   the functional multi-tenancy).
2. **Row-Level Security** (installed by ``tenancy.setup``) — Postgres itself
   enforces ``workspace_id = current_setting('app.workspace_id')`` so even a
   query bug or a raw ``SELECT *`` cannot leak across tenants.

Single-tenant / OSS local use just leaves ``workspace_id="default"`` — identical
behaviour, everything lives in the ``default`` workspace. Requires the
[postgres] extra.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .errors import RyaError
from .store import now_iso, _new_id

try:
    import psycopg
    from psycopg.types.json import Json
except ImportError as exc:  # pragma: no cover - optional dependency
    raise ImportError(
        "PostgresStore requires the [postgres] extra. Install with: pip install 'rya[postgres]'"
    ) from exc


_SCHEMA = """
CREATE TABLE IF NOT EXISTS rya_runs (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    owner TEXT,
    agent TEXT,
    created_at TEXT,
    data JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS rya_approvals (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    status TEXT,
    created_at TEXT,
    data JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS rya_jobs (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    status TEXT,
    run_at TEXT,
    data JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS rya_queue (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    type TEXT,
    status TEXT,
    run_at TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    concurrency_key TEXT,
    lease_expires_at TEXT,
    data JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queue_claim ON rya_queue (workspace_id, status, run_at);
CREATE TABLE IF NOT EXISTS rya_stream (
    turn_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    seq INTEGER NOT NULL,
    kind TEXT,
    data JSONB,
    ts TEXT,
    PRIMARY KEY (workspace_id, turn_id, seq)
);
CREATE TABLE IF NOT EXISTS rya_memory (
    workspace_id TEXT NOT NULL DEFAULT 'default',
    scope TEXT NOT NULL,
    data JSONB NOT NULL,
    PRIMARY KEY (workspace_id, scope)
);
CREATE TABLE IF NOT EXISTS rya_sessions (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    owner TEXT,
    agent TEXT,
    channel TEXT,
    external_id TEXT,
    last_message_at TEXT,
    data JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS rya_messages (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts TEXT,
    data JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS rya_connections (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    owner TEXT,
    provider TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    data JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS rya_files (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    name TEXT NOT NULL,
    content_type TEXT,
    size BIGINT,
    sha256 TEXT,
    tags JSONB NOT NULL DEFAULT '{}'::jsonb,
    content BYTEA NOT NULL,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_ws ON rya_files (workspace_id, created_at DESC);
CREATE TABLE IF NOT EXISTS rya_job_groups (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    remaining INTEGER NOT NULL,
    fired BOOLEAN NOT NULL DEFAULT FALSE,
    failed BOOLEAN NOT NULL DEFAULT FALSE,
    on_complete JSONB NOT NULL
);
-- ---------------------------------------------------------------------------
-- Platform state (PLATFORM_DESIGN D7, D10, D11, D12, §6).
-- ---------------------------------------------------------------------------
-- D10: the commit path needs an APPEND, not a rewrite of rya_runs.data. Entries
-- are revisioned instead of updated in place, so an approval's pending ->
-- approved transition adds a row and the prior one stays auditable.
CREATE TABLE IF NOT EXISTS rya_journal (
    workspace_id TEXT NOT NULL DEFAULT 'default',
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    content_key TEXT,
    kind TEXT,
    label TEXT,
    status TEXT,
    data JSONB NOT NULL,
    appended_at TEXT,
    PRIMARY KEY (workspace_id, run_id, seq, revision)
);
CREATE INDEX IF NOT EXISTS idx_journal_run ON rya_journal (workspace_id, run_id, seq, revision DESC);
-- D10: billing needs a ledger. observability/usage.py derives money from
-- run["trace"], which is a redacted, rewritable debugging artifact.
CREATE TABLE IF NOT EXISTS rya_meter (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    run_id TEXT,
    ts TEXT NOT NULL,
    kind TEXT,
    agent TEXT,
    agent_version TEXT,
    model TEXT,
    input_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    data JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_meter_ws_ts ON rya_meter (workspace_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_meter_run ON rya_meter (workspace_id, run_id);
-- D7 / §11.2: kill switches, guard policy and per-environment config are
-- privileged platform state, not an ordinary memory scope a bundle can write.
CREATE TABLE IF NOT EXISTS rya_policy (
    workspace_id TEXT NOT NULL DEFAULT 'default',
    key TEXT NOT NULL,
    value JSONB,
    version INTEGER NOT NULL DEFAULT 1,
    actor TEXT,
    changed_at TEXT,
    PRIMARY KEY (workspace_id, key)
);
-- §12 risk 7: "who reviewed this allowlist change" is a feature. Append-only.
CREATE TABLE IF NOT EXISTS rya_policy_log (
    id BIGSERIAL PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    key TEXT NOT NULL,
    value JSONB,
    previous JSONB,
    version INTEGER NOT NULL,
    actor TEXT,
    changed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_policy_log ON rya_policy_log (workspace_id, key, id DESC);
-- D12: deployments are immutable, content-hashed and pinned per run.
CREATE TABLE IF NOT EXISTS rya_versions (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    agent TEXT NOT NULL,
    bundle_hash TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active',
    created_at TEXT,
    retired_at TEXT,
    data JSONB NOT NULL
);
-- The immutability + uniqueness `agentVersion` never had: one row per content.
CREATE UNIQUE INDEX IF NOT EXISTS idx_versions_hash ON rya_versions (workspace_id, agent, bundle_hash);
CREATE INDEX IF NOT EXISTS idx_versions_agent ON rya_versions (workspace_id, agent, created_at DESC);
-- §9: promotion-gate evidence. Filed against a version id, which is 1:1 with
-- content (idx_versions_hash), so a gate cannot be satisfied by checks run
-- against a different tree. Append-only: a failed check or an override must stay
-- visible after a later passing one, so there is no UPDATE path.
CREATE TABLE IF NOT EXISTS rya_version_attestations (
    id BIGSERIAL PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    version_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    ok BOOLEAN,
    actor TEXT,
    created_at TEXT,
    data JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_attest_version ON rya_version_attestations (workspace_id, version_id, id);
-- D11: an environment holds ONE current version pointer; promote and rollback
-- are the same pointer flip, so the prior pointer is retained in data.history.
CREATE TABLE IF NOT EXISTS rya_environments (
    workspace_id TEXT NOT NULL DEFAULT 'default',
    name TEXT NOT NULL,
    agent TEXT NOT NULL,
    current_version_id TEXT,
    updated_at TEXT,
    data JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (workspace_id, name, agent)
);
-- §6: queue.claim takes a bare worker_id that is never registered or validated.
CREATE TABLE IF NOT EXISTS rya_workers (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    agent TEXT,
    version_id TEXT,
    bundle_hash TEXT,
    status TEXT NOT NULL DEFAULT 'alive',
    started_at TEXT,
    last_heartbeat_at TEXT,
    data JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_workers_key ON rya_workers (workspace_id, agent, version_id, status);
CREATE INDEX IF NOT EXISTS idx_conn_lookup ON rya_connections (workspace_id, provider, status);
CREATE INDEX IF NOT EXISTS idx_runs_ws ON rya_runs (workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_approvals_ws ON rya_approvals (workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_jobs_ws ON rya_jobs (workspace_id, run_at);
CREATE INDEX IF NOT EXISTS idx_sessions_ws ON rya_sessions (workspace_id, last_message_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_ext ON rya_sessions (workspace_id, agent, channel, external_id);
CREATE INDEX IF NOT EXISTS idx_messages_session ON rya_messages (workspace_id, session_id, seq);
"""


class PostgresStore:
    def __init__(self, dsn: str, workspace_id: str = "default", user_id: Optional[str] = None) -> None:
        self.dsn = dsn
        self.workspace_id = workspace_id
        self.user_id = user_id
        self._conn = psycopg.connect(dsn, autocommit=True)
        # Set the RLS scoping variables for this session. Harmless if RLS is not
        # installed; required for tenant + per-user enforcement when it is.
        with self._conn.cursor() as cur:
            cur.execute("SELECT set_config('app.workspace_id', %s, false)", (workspace_id,))
            cur.execute("SELECT set_config('app.user_id', %s, false)", (user_id or "",))

    def ensure(self) -> None:
        try:
            with self._conn.cursor() as cur:
                cur.execute(_SCHEMA)
        except psycopg.errors.InsufficientPrivilege:
            # Restricted data-plane role (rya_app) in multi-tenant mode — tables
            # are already provisioned by the admin via tenancy.setup().
            pass

    def close(self) -> None:  # pragma: no cover - lifecycle helper
        self._conn.close()

    def describe(self) -> dict:
        try:
            info = self._conn.info
            return {"backend": "postgres", "host": info.host, "dbname": info.dbname,
                    "workspace": self.workspace_id}
        except Exception:  # pragma: no cover
            return {"backend": "postgres", "workspace": self.workspace_id}

    @property
    def _ws(self) -> str:
        return self.workspace_id

    # ---- job groups (fan-in): atomic decrement + exactly-once fire ---------
    def create_job_group(self, on_complete: dict, count: int) -> dict:
        gid = _new_id("grp")
        with self._conn.cursor() as cur:
            cur.execute("INSERT INTO rya_job_groups (id, workspace_id, remaining, on_complete) "
                        "VALUES (%s, %s, %s, %s)", (gid, self._ws, count, Json(on_complete)))
        return {"id": gid, "remaining": count, "onComplete": on_complete}

    def group_job_done(self, group_id: str, success: bool = True) -> Optional[dict]:
        with self._conn.cursor() as cur:
            if not success:
                cur.execute("UPDATE rya_job_groups SET failed = TRUE WHERE id = %s AND workspace_id = %s",
                            (group_id, self._ws))
                return {"fire": False}
            cur.execute("UPDATE rya_job_groups SET remaining = remaining - 1 "
                        "WHERE id = %s AND workspace_id = %s RETURNING remaining, failed, on_complete",
                        (group_id, self._ws))
            row = cur.fetchone()
            if row is None:
                return None
            remaining, failed, on_complete = row
            if remaining > 0 or failed:
                return {"fire": False, "onComplete": on_complete}
            # exactly-once claim
            cur.execute("UPDATE rya_job_groups SET fired = TRUE "
                        "WHERE id = %s AND workspace_id = %s AND fired = FALSE", (group_id, self._ws))
            return {"fire": cur.rowcount == 1, "onComplete": on_complete}

    # ---- files (uploaded documents; immutable once saved) ---------------
    def save_file(self, name: str, content: bytes, content_type: Optional[str] = None,
                  tags: Optional[dict] = None) -> dict:
        import hashlib
        meta = {"id": _new_id("file"), "name": name,
                "contentType": content_type or "application/octet-stream",
                "size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                "tags": tags or {}, "createdAt": now_iso()}
        from . import files_s3
        if files_s3.bucket():
            if content:  # presigned uploads register metadata first; bytes arrive via S3 PUT
                files_s3.put_bytes(meta["id"], content, meta["contentType"])
            meta["tags"] = {**meta["tags"], "_storage": "s3"}
            content = b""
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO rya_files (id, workspace_id, name, content_type, size, sha256, tags, content, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (meta["id"], self._ws, name, meta["contentType"], meta["size"],
                 meta["sha256"], Json(meta["tags"]), content, meta["createdAt"]))
        return meta

    def _file_meta_row(self, r) -> dict:
        return {"id": r[0], "name": r[1], "contentType": r[2], "size": r[3],
                "sha256": r[4], "tags": r[5], "createdAt": r[6]}

    def get_file(self, file_id: str) -> Optional[dict]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT id, name, content_type, size, sha256, tags, created_at "
                        "FROM rya_files WHERE id=%s AND workspace_id=%s", (file_id, self._ws))
            r = cur.fetchone()
        return self._file_meta_row(r) if r else None

    def read_file(self, file_id: str) -> Optional[bytes]:
        meta = self.get_file(file_id)
        if meta and (meta.get("tags") or {}).get("_storage") == "s3":
            from . import files_s3
            return files_s3.get_bytes(file_id)
        with self._conn.cursor() as cur:
            cur.execute("SELECT content FROM rya_files WHERE id=%s AND workspace_id=%s",
                        (file_id, self._ws))
            r = cur.fetchone()
        return bytes(r[0]) if r else None

    def list_files(self, tags: Optional[dict] = None) -> List[dict]:
        with self._conn.cursor() as cur:
            if tags:
                cur.execute("SELECT id, name, content_type, size, sha256, tags, created_at "
                            "FROM rya_files WHERE workspace_id=%s AND tags @> %s::jsonb "
                            "ORDER BY created_at DESC", (self._ws, Json(tags)))
            else:
                cur.execute("SELECT id, name, content_type, size, sha256, tags, created_at "
                            "FROM rya_files WHERE workspace_id=%s ORDER BY created_at DESC", (self._ws,))
            rows = cur.fetchall()
        return [self._file_meta_row(r) for r in rows]

    # ---- runs ----------------------------------------------------------
    def new_run_id(self) -> str:
        return _new_id("run")

    def save_run(self, run: dict) -> None:
        run["updatedAt"] = now_iso()
        run.setdefault("workspaceId", self._ws)
        # owner = the user who triggered this run (from JWT identity), or None for
        # agent/system runs that are shared within the workspace.
        owner = self.user_id or (run.get("identity") or {}).get("sub")
        with self._conn.cursor() as cur:
            cur.execute(
                """INSERT INTO rya_runs (id, workspace_id, owner, agent, created_at, data)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET agent=EXCLUDED.agent,
                       created_at=EXCLUDED.created_at, data=EXCLUDED.data
                   WHERE rya_runs.workspace_id = EXCLUDED.workspace_id""",
                (run["id"], self._ws, owner, run.get("agent"), run.get("createdAt"), Json(run)),
            )

    def get_run(self, run_id: str) -> Optional[dict]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT data FROM rya_runs WHERE id=%s AND workspace_id=%s", (run_id, self._ws))
            row = cur.fetchone()
            return row[0] if row else None

    def list_runs(self, agent: Optional[str] = None) -> List[dict]:
        with self._conn.cursor() as cur:
            if agent is None:
                cur.execute("SELECT data FROM rya_runs WHERE workspace_id=%s ORDER BY created_at DESC", (self._ws,))
            else:
                cur.execute("SELECT data FROM rya_runs WHERE workspace_id=%s AND agent=%s ORDER BY created_at DESC",
                            (self._ws, agent))
            return [r[0] for r in cur.fetchall()]

    def run_counts(self, since: Optional[str] = None) -> Dict[str, int]:
        """Runs per status, counted in the database rather than in Python.

        The quota check (§11.12) runs on the admission path, so it must not pull
        every run document back to count them. Status lives in the JSONB blob
        rather than a column, which is a D10 residual: the decomposition covered
        the journal, not the run header.
        """
        clauses = ["workspace_id=%s"]
        params: List[Any] = [self._ws]
        if since is not None:
            clauses.append("created_at >= %s")
            params.append(since)
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT COALESCE(data->>'status', '?'), count(*) FROM rya_runs "
                        f"WHERE {' AND '.join(clauses)} GROUP BY 1", tuple(params))
            return {r[0]: r[1] for r in cur.fetchall()}

    # ---- approvals -----------------------------------------------------
    def create_approval(self, run_id: str, title: str, body: str, action: dict) -> dict:
        approval = {
            "id": _new_id("apr"), "runId": run_id, "title": title, "body": body,
            "action": action, "status": "pending", "createdAt": now_iso(),
            "resolvedAt": None, "actionResult": None,
        }
        self.save_approval(approval)
        return approval

    def get_approval(self, approval_id: str) -> Optional[dict]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT data FROM rya_approvals WHERE id=%s AND workspace_id=%s", (approval_id, self._ws))
            row = cur.fetchone()
            return row[0] if row else None

    def save_approval(self, approval: dict) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """INSERT INTO rya_approvals (id, workspace_id, status, created_at, data)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET status=EXCLUDED.status,
                       created_at=EXCLUDED.created_at, data=EXCLUDED.data
                   WHERE rya_approvals.workspace_id = EXCLUDED.workspace_id""",
                (approval["id"], self._ws, approval.get("status"), approval.get("createdAt"), Json(approval)),
            )

    def list_approvals(self, status: Optional[str] = None) -> List[dict]:
        with self._conn.cursor() as cur:
            if status is None:
                cur.execute("SELECT data FROM rya_approvals WHERE workspace_id=%s ORDER BY created_at DESC", (self._ws,))
            else:
                cur.execute("SELECT data FROM rya_approvals WHERE workspace_id=%s AND status=%s ORDER BY created_at DESC",
                            (self._ws, status))
            return [r[0] for r in cur.fetchall()]

    # ---- jobs ----------------------------------------------------------
    def create_job(self, run_id: str, handler: str, payload: dict, run_at: str,
                   max_attempts: int = 3, group_id: Optional[str] = None) -> dict:
        job = {
            "id": _new_id("job"), "parentRunId": run_id, "handler": handler,
            "payload": payload, "status": "pending", "runAt": run_at,
            "attempts": 0, "maxAttempts": max_attempts, "lastError": None,
            "createdAt": now_iso(), "resultRunId": None, "groupId": group_id,
        }
        self.save_job(job)
        return job

    def get_job(self, job_id: str) -> Optional[dict]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT data FROM rya_jobs WHERE id=%s AND workspace_id=%s", (job_id, self._ws))
            row = cur.fetchone()
            return row[0] if row else None

    def save_job(self, job: dict) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """INSERT INTO rya_jobs (id, workspace_id, status, run_at, data)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET status=EXCLUDED.status,
                       run_at=EXCLUDED.run_at, data=EXCLUDED.data
                   WHERE rya_jobs.workspace_id = EXCLUDED.workspace_id""",
                (job["id"], self._ws, job.get("status"), job.get("runAt"), Json(job)),
            )

    def list_jobs(self, status: Optional[str] = None) -> List[dict]:
        with self._conn.cursor() as cur:
            if status is None:
                cur.execute("SELECT data FROM rya_jobs WHERE workspace_id=%s ORDER BY run_at ASC", (self._ws,))
            else:
                cur.execute("SELECT data FROM rya_jobs WHERE workspace_id=%s AND status=%s ORDER BY run_at ASC",
                            (self._ws, status))
            return [r[0] for r in cur.fetchall()]

    def claim_due_job(self) -> Optional[dict]:
        """Atomically claim one due pending job. FOR UPDATE SKIP LOCKED makes this
        safe across N concurrent workers — no two ever grab the same job."""
        with self._conn.cursor() as cur:
            cur.execute(
                """WITH c AS (
                       SELECT id FROM rya_jobs
                       WHERE workspace_id=%s AND status='pending' AND run_at <= %s
                       ORDER BY run_at FOR UPDATE SKIP LOCKED LIMIT 1)
                   UPDATE rya_jobs j SET status='claimed', data = jsonb_set(j.data, '{status}', '"claimed"')
                   FROM c WHERE j.id = c.id
                   RETURNING j.data""",
                (self._ws, now_iso()),
            )
            row = cur.fetchone()
            return row[0] if row else None

    # ---- queue: durable jobs for EXTERNAL workers ------------------------
    # Same duck-typed surface as FileStore. Claiming is truly atomic here:
    # FOR UPDATE SKIP LOCKED means N concurrent workers never grab the same job.
    def queue_save(self, job: dict) -> None:
        job["updatedAt"] = now_iso()
        with self._conn.cursor() as cur:
            cur.execute(
                """INSERT INTO rya_queue
                       (id, workspace_id, type, status, run_at, priority, concurrency_key,
                        lease_expires_at, data)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET type=EXCLUDED.type, status=EXCLUDED.status,
                       run_at=EXCLUDED.run_at, priority=EXCLUDED.priority,
                       concurrency_key=EXCLUDED.concurrency_key,
                       lease_expires_at=EXCLUDED.lease_expires_at, data=EXCLUDED.data
                   WHERE rya_queue.workspace_id = EXCLUDED.workspace_id""",
                (job["id"], self._ws, job.get("type"), job.get("status"), job.get("runAt"),
                 int(job.get("priority") or 0), job.get("concurrencyKey"),
                 job.get("leaseExpiresAt"), Json(job)),
            )

    def queue_get(self, job_id: str) -> Optional[dict]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT data FROM rya_queue WHERE id=%s AND workspace_id=%s",
                        (job_id, self._ws))
            row = cur.fetchone()
            return row[0] if row else None

    def queue_list(self, status: Optional[str] = None, type: Optional[str] = None) -> List[dict]:
        q = "SELECT data FROM rya_queue WHERE workspace_id=%s"
        args: list = [self._ws]
        if status is not None:
            q += " AND status=%s"
            args.append(status)
        if type is not None:
            q += " AND type=%s"
            args.append(type)
        q += " ORDER BY priority DESC, run_at ASC, (data->>'seq') ASC"
        with self._conn.cursor() as cur:
            cur.execute(q, tuple(args))
            return [r[0] for r in cur.fetchall()]

    def queue_reap(self, now: str) -> None:
        """Expired-lease running jobs: retryable ones go back to pending, exhausted
        ones dead-letter. Two conditional UPDATEs, each atomic."""
        with self._conn.cursor() as cur:
            cur.execute(
                """UPDATE rya_queue SET status='pending', run_at=%s, lease_expires_at=NULL,
                       data = data || jsonb_build_object(
                           'status','pending','workerId',NULL,'leaseExpiresAt',NULL,
                           'runAt',%s::text,'lastError','lease expired')
                   WHERE workspace_id=%s AND status='running' AND lease_expires_at <= %s
                     AND COALESCE((data->>'attempts')::int, 0)
                         < COALESCE((data->>'maxAttempts')::int, 1)""",
                (now, now, self._ws, now),
            )
            cur.execute(
                """UPDATE rya_queue SET status='failed', lease_expires_at=NULL,
                       data = data || jsonb_build_object(
                           'status','failed','workerId',NULL,'leaseExpiresAt',NULL,
                           'deadLetter',true,'completedAt',%s::text,
                           'error', COALESCE(data->>'error','lease expired'))
                   WHERE workspace_id=%s AND status='running' AND lease_expires_at <= %s
                     AND COALESCE((data->>'attempts')::int, 0)
                         >= COALESCE((data->>'maxAttempts')::int, 1)""",
                (now, self._ws, now),
            )

    def queue_claim_one(self, worker_id: str, now: str, lease_expires_at: str,
                        types: Optional[List[str]] = None) -> Optional[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                """WITH running AS (
                       SELECT concurrency_key AS k, count(*) AS n FROM rya_queue
                       WHERE workspace_id=%s AND status='running' AND concurrency_key IS NOT NULL
                       GROUP BY concurrency_key),
                   cand AS (
                       SELECT q.id FROM rya_queue q
                       LEFT JOIN running r ON r.k = q.concurrency_key
                       WHERE q.workspace_id=%s AND q.status='pending' AND q.run_at <= %s
                         AND (%s::text[] IS NULL OR q.type = ANY(%s::text[]))
                         AND (q.concurrency_key IS NULL OR COALESCE(r.n, 0) <
                              COALESCE(NULLIF((q.data->>'concurrencyLimit')::int, 0), 2147483647))
                       -- Fairness first (§6: "one workspace must not starve
                       -- another"): the least-busy concurrency key wins the next
                       -- slot. Jobs sharing a key all see the same r.n, so
                       -- priority/age ordering WITHIN a key is unchanged; this
                       -- only decides between different keys. Mirrors
                       -- store._fair_order, which documents the trade-off.
                       ORDER BY COALESCE(r.n, 0) ASC,
                                q.priority DESC, q.run_at ASC, (q.data->>'seq') ASC
                       FOR UPDATE OF q SKIP LOCKED LIMIT 1)
                   UPDATE rya_queue q SET status='running', lease_expires_at=%s,
                       data = q.data || jsonb_build_object(
                           'status','running','workerId',%s::text,'leaseExpiresAt',%s::text,
                           'attempts', COALESCE((q.data->>'attempts')::int, 0) + 1,
                           'startedAt', COALESCE(q.data->>'startedAt', %s::text),
                           'updatedAt', %s::text)
                   FROM cand WHERE q.id = cand.id
                   RETURNING q.data""",
                (self._ws, self._ws, now, types, types, lease_expires_at, worker_id,
                 lease_expires_at, now, now),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def queue_counts(self) -> dict:
        with self._conn.cursor() as cur:
            cur.execute("SELECT status, count(*) FROM rya_queue WHERE workspace_id=%s GROUP BY status",
                        (self._ws,))
            return {r[0]: r[1] for r in cur.fetchall()}

    # ---- durable turn stream buffer --------------------------------------
    def stream_append(self, turn_id: str, frames: List[dict]) -> int:
        with self._conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(seq), -1) FROM rya_stream WHERE workspace_id=%s AND turn_id=%s",
                        (self._ws, turn_id))
            base = cur.fetchone()[0] + 1
            for i, f in enumerate(frames):
                cur.execute(
                    """INSERT INTO rya_stream (turn_id, workspace_id, seq, kind, data, ts)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (turn_id, self._ws, base + i, f["kind"], Json(f.get("data")), now_iso()),
                )
        return base + len(frames)

    def stream_read(self, turn_id: str, after_seq: int = -1) -> List[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                """SELECT seq, kind, data, ts FROM rya_stream
                   WHERE workspace_id=%s AND turn_id=%s AND seq > %s ORDER BY seq""",
                (self._ws, turn_id, after_seq),
            )
            return [{"seq": r[0], "kind": r[1], "data": r[2], "ts": r[3]} for r in cur.fetchall()]

    # ---- memory --------------------------------------------------------
    def load_memory(self, scope: str) -> Dict[str, Any]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT data FROM rya_memory WHERE workspace_id=%s AND scope=%s", (self._ws, scope))
            row = cur.fetchone()
            return row[0] if row else {"kv": {}, "collections": {}}

    def save_memory(self, scope: str, data: Dict[str, Any]) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """INSERT INTO rya_memory (workspace_id, scope, data) VALUES (%s, %s, %s)
                   ON CONFLICT (workspace_id, scope) DO UPDATE SET data=EXCLUDED.data""",
                (self._ws, scope, Json(data)),
            )

    def list_memory_scopes(self) -> List[str]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT scope FROM rya_memory WHERE workspace_id=%s ORDER BY scope", (self._ws,))
            return [r[0] for r in cur.fetchall()]

    # ---- conversations: sessions + messages ----------------------------
    def create_session(self, agent: str, channel: str, external_id: Optional[str],
                       owner: Optional[str] = None, title: Optional[str] = None) -> dict:
        session = {
            "id": _new_id("ses"), "agent": agent, "channel": channel,
            "externalId": external_id, "owner": owner or self.user_id,
            "title": title or "Conversation", "status": "open",
            "createdAt": now_iso(), "lastMessageAt": now_iso(), "messageCount": 0,
        }
        with self._conn.cursor() as cur:
            cur.execute(
                """INSERT INTO rya_sessions
                   (id, workspace_id, owner, agent, channel, external_id, last_message_at, data)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (session["id"], self._ws, session["owner"], agent, channel, external_id,
                 session["lastMessageAt"], Json(session)),
            )
        return session

    def get_session(self, sid: str) -> Optional[dict]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT data FROM rya_sessions WHERE id=%s AND workspace_id=%s", (sid, self._ws))
            row = cur.fetchone()
            if not row:
                return None
            session = dict(row[0])
            session["messages"] = self.list_messages(sid)
            return session

    def list_sessions(self, agent: Optional[str] = None) -> List[dict]:
        with self._conn.cursor() as cur:
            if agent is None:
                cur.execute("SELECT data FROM rya_sessions WHERE workspace_id=%s ORDER BY last_message_at DESC",
                            (self._ws,))
            else:
                cur.execute("SELECT data FROM rya_sessions WHERE workspace_id=%s AND agent=%s ORDER BY last_message_at DESC",
                            (self._ws, agent))
            return [r[0] for r in cur.fetchall()]

    def find_session(self, agent: str, channel: str, external_id: str) -> Optional[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                """SELECT data FROM rya_sessions
                   WHERE workspace_id=%s AND agent=%s AND channel=%s AND external_id=%s""",
                (self._ws, agent, channel, external_id),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def append_message(self, sid: str, role: str, content: str, **extra) -> dict:
        with self._conn.cursor() as cur:
            cur.execute("SELECT data FROM rya_sessions WHERE id=%s AND workspace_id=%s", (sid, self._ws))
            row = cur.fetchone()
            if not row:
                raise KeyError(sid)
            session = dict(row[0])
            cur.execute("SELECT count(*) FROM rya_messages WHERE workspace_id=%s AND session_id=%s",
                        (self._ws, sid))
            seq = cur.fetchone()[0]
            msg = {"id": _new_id("msg"), "seq": seq, "role": role, "content": content,
                   "ts": now_iso(), **extra}
            cur.execute(
                """INSERT INTO rya_messages (id, workspace_id, session_id, seq, ts, data)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (msg["id"], self._ws, sid, seq, msg["ts"], Json(msg)),
            )
            session["messageCount"] = seq + 1
            session["lastMessageAt"] = msg["ts"]
            cur.execute(
                "UPDATE rya_sessions SET last_message_at=%s, data=%s WHERE id=%s AND workspace_id=%s",
                (msg["ts"], Json(session), sid, self._ws),
            )
        return msg

    def list_messages(self, sid: str) -> List[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM rya_messages WHERE workspace_id=%s AND session_id=%s ORDER BY seq ASC",
                (self._ws, sid),
            )
            return [r[0] for r in cur.fetchall()]

    # ---- connections: scoped, encrypted-at-rest credentials ------------
    @staticmethod
    def _public_connection(doc: dict) -> dict:
        from .seal import is_sealed
        return ({k: v for k, v in doc.items() if k != "secret"}
                | {"secretSet": bool(doc.get("secret")),
                   "encrypted": is_sealed(doc.get("secret"))})

    def create_connection(self, provider: str, scopes: List[str], secret: Optional[str] = None,
                          owner: Optional[str] = None, label: Optional[str] = None) -> dict:
        from .seal import seal
        conn = {
            "id": _new_id("conn"), "provider": provider, "owner": owner or self.user_id,
            "scopes": list(scopes or []), "label": label or provider,
            # Server context: key comes from RYA_SECRET_KEY (no project keyfile).
            "secret": seal(secret, None), "status": "active", "createdAt": now_iso(),
        }
        with self._conn.cursor() as cur:
            cur.execute(
                """INSERT INTO rya_connections (id, workspace_id, owner, provider, status, data)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (conn["id"], self._ws, conn["owner"], provider, "active", Json(conn)),
            )
        return self._public_connection(conn)

    def upsert_connection(self, provider: str, scopes: List[str], secret: Optional[str] = None,
                          owner: Optional[str] = None, label: Optional[str] = None) -> dict:
        """Overwrite-in-place the active connection for (provider, owner), or create
        one. Keyed on (provider, owner) — NOT the random id — so a re-login refreshes
        the same row instead of minting duplicates that could later inject a stale
        token. Preserves the existing id/createdAt when overwriting; stamps updatedAt."""
        from .seal import seal
        owner = owner or self.user_id
        with self._conn.cursor() as cur:
            cur.execute(
                """SELECT id, data FROM rya_connections
                   WHERE workspace_id=%s AND provider=%s AND status='active'
                     AND owner IS NOT DISTINCT FROM %s
                   ORDER BY (data->>'createdAt') ASC LIMIT 1 FOR UPDATE""",
                (self._ws, provider, owner),
            )
            row = cur.fetchone()
            conn = {
                "id": row[0] if row else _new_id("conn"),
                "provider": provider, "owner": owner,
                "scopes": list(scopes or []), "label": label or provider,
                "secret": seal(secret, None), "status": "active",
                "createdAt": (row[1].get("createdAt") if row else now_iso()) or now_iso(),
                "updatedAt": now_iso(),
            }
            if row:
                cur.execute(
                    "UPDATE rya_connections SET owner=%s, status='active', data=%s WHERE id=%s",
                    (owner, Json(conn), conn["id"]),
                )
            else:
                cur.execute(
                    """INSERT INTO rya_connections (id, workspace_id, owner, provider, status, data)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (conn["id"], self._ws, owner, provider, "active", Json(conn)),
                )
        return self._public_connection(conn)

    def get_connection(self, provider: str, owner: Optional[str] = None) -> Optional[dict]:
        from .seal import unseal
        with self._conn.cursor() as cur:
            # Prefer a user-owned connection; fall back to a workspace-shared one.
            cur.execute(
                """SELECT data, owner FROM rya_connections
                   WHERE workspace_id=%s AND provider=%s AND status='active'
                     AND (owner=%s OR owner IS NULL)
                   ORDER BY (owner IS NULL) ASC LIMIT 1""",
                (self._ws, provider, owner),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {**row[0], "secret": unseal(row[0].get("secret"), None)}

    def list_connections(self) -> List[dict]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT data FROM rya_connections WHERE workspace_id=%s ORDER BY (data->>'createdAt') DESC",
                        (self._ws,))
            return [self._public_connection(r[0]) for r in cur.fetchall()]

    def revoke_connection(self, conn_id: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute("SELECT data FROM rya_connections WHERE id=%s AND workspace_id=%s", (conn_id, self._ws))
            row = cur.fetchone()
            if not row:
                return False
            doc = dict(row[0]); doc["status"] = "revoked"; doc["secret"] = None
            cur.execute("UPDATE rya_connections SET status='revoked', data=%s WHERE id=%s AND workspace_id=%s",
                        (Json(doc), conn_id, self._ws))
            return True

    def reseal_connections(self) -> dict:
        """Encrypt any legacy plaintext secrets at rest (needs RYA_SECRET_KEY in a
        server context). Idempotent and workspace-scoped."""
        from .seal import is_sealed, seal
        scanned = resealed = already = empty = 0
        with self._conn.cursor() as cur:
            cur.execute("SELECT id, data FROM rya_connections WHERE workspace_id=%s", (self._ws,))
            rows = cur.fetchall()
        for cid, data in rows:
            scanned += 1
            sec = data.get("secret")
            if not sec:
                empty += 1
                continue
            if is_sealed(sec):
                already += 1
                continue
            new = seal(sec, None)  # env key only in server context
            if is_sealed(new):
                doc = {**data, "secret": new}
                with self._conn.cursor() as cur:
                    cur.execute("UPDATE rya_connections SET data=%s WHERE id=%s AND workspace_id=%s",
                                (Json(doc), cid, self._ws))
                resealed += 1
        return {"scanned": scanned, "resealed": resealed,
                "alreadyEncrypted": already, "noSecret": empty}

    # ======================================================================
    # Platform state — mirrors the FileStore surface (see store.py for the
    # rationale behind each group). PLATFORM_DESIGN D7, D10, D11, D12, §6.
    # ======================================================================

    # ---- append-only journal (D10) ---------------------------------------
    def journal_append(self, run_id: str, entry: dict) -> dict:
        """One INSERT per step. The revision is computed in-statement so two
        processes racing on the same seq (a reclaimed run and its predecessor)
        both land a row rather than one clobbering the other."""
        seq = int(entry.get("seq") or 0)
        appended_at = now_iso()
        with self._conn.cursor() as cur:
            cur.execute(
                """INSERT INTO rya_journal
                       (workspace_id, run_id, seq, revision, content_key, kind, label,
                        status, data, appended_at)
                   SELECT %s, %s, %s,
                          COALESCE((SELECT MAX(revision) + 1 FROM rya_journal
                                    WHERE workspace_id=%s AND run_id=%s AND seq=%s), 0),
                          %s, %s, %s, %s, %s, %s
                   RETURNING revision""",
                (self._ws, run_id, seq, self._ws, run_id, seq,
                 entry.get("contentKey"), entry.get("kind"), entry.get("label"),
                 entry.get("status"), Json(entry), appended_at),
            )
            revision = cur.fetchone()[0]
        return {**entry, "runId": run_id, "revision": revision, "appendedAt": appended_at}

    def journal_read(self, run_id: str) -> Dict[str, dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT ON (seq) seq, revision, data, appended_at
                   FROM rya_journal WHERE workspace_id=%s AND run_id=%s
                   ORDER BY seq, revision DESC""",
                (self._ws, run_id),
            )
            return {str(seq): {**data, "revision": rev, "appendedAt": at}
                    for seq, rev, data, at in cur.fetchall()}

    def journal_revisions(self, run_id: str) -> List[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                """SELECT seq, revision, data, appended_at FROM rya_journal
                   WHERE workspace_id=%s AND run_id=%s ORDER BY seq, revision""",
                (self._ws, run_id),
            )
            return [{**data, "revision": rev, "appendedAt": at}
                    for _, rev, data, at in cur.fetchall()]

    # ---- durable meter (D10) ---------------------------------------------
    def meter_append(self, record: dict) -> dict:
        rec = {"id": _new_id("mtr"), "ts": now_iso(), **record}
        with self._conn.cursor() as cur:
            cur.execute(
                """INSERT INTO rya_meter (id, workspace_id, run_id, ts, kind, agent,
                       agent_version, model, input_tokens, output_tokens, cost_usd, data)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (id) DO NOTHING""",
                (rec["id"], self._ws, rec.get("runId"), rec["ts"], rec.get("kind"),
                 rec.get("agent"), rec.get("agentVersion"), rec.get("model"),
                 int(rec.get("inputTokens") or 0), int(rec.get("outputTokens") or 0),
                 float(rec.get("costUsd") or 0.0), Json(rec)),
            )
        return rec

    def meter_read(self, run_id: Optional[str] = None, since: Optional[str] = None,
                   until: Optional[str] = None, limit: int = 1000) -> List[dict]:
        clauses = ["workspace_id=%s"]
        params: List[Any] = [self._ws]
        if run_id is not None:
            clauses.append("run_id=%s")
            params.append(run_id)
        if since is not None:
            clauses.append("ts >= %s")
            params.append(since)
        if until is not None:
            clauses.append("ts <= %s")
            params.append(until)
        params.append(int(limit))
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT data FROM rya_meter WHERE {' AND '.join(clauses)} "
                        "ORDER BY ts DESC LIMIT %s", tuple(params))
            return [r[0] for r in cur.fetchall()][::-1]

    _METER_GROUPS = {"model": "model", "agent": "agent", "kind": "kind",
                     "agentVersion": "agent_version", "runId": "run_id"}

    def meter_totals(self, since: Optional[str] = None, until: Optional[str] = None,
                     group_by: Optional[str] = None) -> dict:
        clauses = ["workspace_id=%s"]
        params: List[Any] = [self._ws]
        if since is not None:
            clauses.append("ts >= %s")
            params.append(since)
        if until is not None:
            clauses.append("ts <= %s")
            params.append(until)
        agg = ("SUM(input_tokens), SUM(output_tokens), SUM(cost_usd), COUNT(*)")
        where = " AND ".join(clauses)
        with self._conn.cursor() as cur:
            if group_by:
                # Whitelist the column: group_by reaches this from an API query
                # parameter, so it must never be interpolated straight into SQL.
                col = self._METER_GROUPS.get(group_by)
                if col is None:
                    raise RyaError("E_VALIDATION",
                                   f"Cannot group the meter by '{group_by}'.",
                                   hint=f"Use one of {sorted(self._METER_GROUPS)}.")
                cur.execute(f"SELECT {col}, {agg} FROM rya_meter WHERE {where} "
                            f"GROUP BY {col}", tuple(params))
                return {str(k): {"inputTokens": int(i or 0), "outputTokens": int(o or 0),
                                 "costUsd": float(c or 0), "calls": int(n or 0)}
                        for k, i, o, c, n in cur.fetchall()}
            cur.execute(f"SELECT {agg} FROM rya_meter WHERE {where}", tuple(params))
            i, o, c, n = cur.fetchone()
            return {"inputTokens": int(i or 0), "outputTokens": int(o or 0),
                    "costUsd": float(c or 0), "calls": int(n or 0)}

    # ---- privileged policy (D7, §11.2) -----------------------------------
    def policy_get(self, key: str) -> Optional[dict]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT value FROM rya_policy WHERE workspace_id=%s AND key=%s",
                        (self._ws, key))
            row = cur.fetchone()
            return row[0] if row else None

    def policy_set(self, key: str, value: Optional[dict], actor: Optional[str] = None) -> dict:
        changed_at = now_iso()
        with self._conn.cursor() as cur:
            cur.execute(
                """INSERT INTO rya_policy (workspace_id, key, value, version, actor, changed_at)
                   VALUES (%s, %s, %s, 1, %s, %s)
                   ON CONFLICT (workspace_id, key) DO UPDATE
                       SET value=EXCLUDED.value, version=rya_policy.version + 1,
                           actor=EXCLUDED.actor, changed_at=EXCLUDED.changed_at
                   RETURNING version, (SELECT value FROM rya_policy p
                                       WHERE p.workspace_id=%s AND p.key=%s)""",
                (self._ws, key, Json(value), actor, changed_at, self._ws, key),
            )
            version, previous = cur.fetchone()
            record = {"key": key, "value": value, "version": version, "actor": actor,
                      "previous": previous, "changedAt": changed_at}
            cur.execute(
                """INSERT INTO rya_policy_log
                       (workspace_id, key, value, previous, version, actor, changed_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (self._ws, key, Json(value), Json(previous), version, actor, changed_at),
            )
        return record

    def policy_all(self) -> Dict[str, Any]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT key, value FROM rya_policy WHERE workspace_id=%s "
                        "AND value IS NOT NULL", (self._ws,))
            return {k: v for k, v in cur.fetchall()}

    def policy_history(self, key: Optional[str] = None, limit: int = 50) -> List[dict]:
        with self._conn.cursor() as cur:
            if key is None:
                cur.execute("SELECT key, value, previous, version, actor, changed_at "
                            "FROM rya_policy_log WHERE workspace_id=%s ORDER BY id DESC LIMIT %s",
                            (self._ws, int(limit)))
            else:
                cur.execute("SELECT key, value, previous, version, actor, changed_at "
                            "FROM rya_policy_log WHERE workspace_id=%s AND key=%s "
                            "ORDER BY id DESC LIMIT %s", (self._ws, key, int(limit)))
            return [{"key": k, "value": v, "previous": p, "version": ver,
                     "actor": a, "changedAt": ts}
                    for k, v, p, ver, a, ts in cur.fetchall()]

    # ---- deployments: immutable versions (D12) ---------------------------
    def version_create(self, record: dict) -> dict:
        existing = self.version_by_hash(record["agent"], record["bundleHash"])
        if existing is not None:
            return existing
        version = {"id": _new_id("ver"), "state": "active", "createdAt": now_iso(),
                   "retiredAt": None, **record}
        with self._conn.cursor() as cur:
            cur.execute(
                """INSERT INTO rya_versions (id, workspace_id, agent, bundle_hash,
                       state, created_at, retired_at, data)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (workspace_id, agent, bundle_hash) DO NOTHING""",
                (version["id"], self._ws, version["agent"], version["bundleHash"],
                 version["state"], version["createdAt"], None, Json(version)),
            )
            if cur.rowcount == 0:  # lost the race; the other writer's row wins
                won = self.version_by_hash(record["agent"], record["bundleHash"])
                if won is not None:
                    return won
        return version

    def version_get(self, version_id: str) -> Optional[dict]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT data FROM rya_versions WHERE id=%s AND workspace_id=%s",
                        (version_id, self._ws))
            row = cur.fetchone()
            return row[0] if row else None

    def version_by_hash(self, agent: str, bundle_hash: str) -> Optional[dict]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT data FROM rya_versions WHERE workspace_id=%s AND agent=%s "
                        "AND bundle_hash=%s", (self._ws, agent, bundle_hash))
            row = cur.fetchone()
            return row[0] if row else None

    def version_list(self, agent: Optional[str] = None, state: Optional[str] = None) -> List[dict]:
        clauses = ["workspace_id=%s"]
        params: List[Any] = [self._ws]
        if agent is not None:
            clauses.append("agent=%s")
            params.append(agent)
        if state is not None:
            clauses.append("state=%s")
            params.append(state)
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT data FROM rya_versions WHERE {' AND '.join(clauses)} "
                        "ORDER BY created_at DESC", tuple(params))
            return [r[0] for r in cur.fetchall()]

    def version_set_state(self, version_id: str, state: str) -> Optional[dict]:
        doc = self.version_get(version_id)
        if doc is None:
            return None
        doc = {**doc, "state": state,
               "retiredAt": now_iso() if state == "retired" else None}
        with self._conn.cursor() as cur:
            cur.execute("UPDATE rya_versions SET state=%s, retired_at=%s, data=%s "
                        "WHERE id=%s AND workspace_id=%s",
                        (state, doc["retiredAt"], Json(doc), version_id, self._ws))
        return doc

    # ---- promotion gate evidence (§9) ------------------------------------
    def version_attest(self, version_id: str, attestation: dict) -> dict:
        record = {"versionId": version_id, "createdAt": now_iso(), **attestation}
        with self._conn.cursor() as cur:
            cur.execute(
                """INSERT INTO rya_version_attestations
                       (workspace_id, version_id, kind, ok, actor, created_at, data)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (self._ws, version_id, record.get("kind") or "unknown",
                 record.get("ok"), record.get("actor"), record["createdAt"], Json(record)))
            row = cur.fetchone()
        return {**record, "id": f"att_{row[0]}" if row else None}

    def version_attestations(self, version_id: str, kind: Optional[str] = None) -> List[dict]:
        clauses = ["workspace_id=%s", "version_id=%s"]
        params: List[Any] = [self._ws, version_id]
        if kind is not None:
            clauses.append("kind=%s")
            params.append(kind)
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT id, data FROM rya_version_attestations "
                        f"WHERE {' AND '.join(clauses)} ORDER BY id", tuple(params))
            return [{**r[1], "id": f"att_{r[0]}"} for r in cur.fetchall()]

    # ---- deployments: environments (D11) ---------------------------------
    def env_get(self, name: str, agent: str) -> Optional[dict]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT data FROM rya_environments WHERE workspace_id=%s "
                        "AND name=%s AND agent=%s", (self._ws, name, agent))
            row = cur.fetchone()
            return row[0] if row else None

    def env_set_current(self, name: str, agent: str, version_id: str,
                        actor: Optional[str] = None) -> dict:
        doc = self.env_get(name, agent) or {"name": name, "agent": agent,
                                            "currentVersionId": None, "history": [],
                                            "createdAt": now_iso()}
        if doc.get("currentVersionId"):
            doc.setdefault("history", []).append(
                {"versionId": doc["currentVersionId"], "replacedAt": now_iso(), "actor": actor})
        doc["currentVersionId"] = version_id
        doc["updatedAt"] = now_iso()
        doc["actor"] = actor
        with self._conn.cursor() as cur:
            cur.execute(
                """INSERT INTO rya_environments (workspace_id, name, agent,
                       current_version_id, updated_at, data)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (workspace_id, name, agent) DO UPDATE
                       SET current_version_id=EXCLUDED.current_version_id,
                           updated_at=EXCLUDED.updated_at, data=EXCLUDED.data""",
                (self._ws, name, agent, version_id, doc["updatedAt"], Json(doc)),
            )
        return doc

    def env_list(self, agent: Optional[str] = None) -> List[dict]:
        with self._conn.cursor() as cur:
            if agent is None:
                cur.execute("SELECT data FROM rya_environments WHERE workspace_id=%s "
                            "ORDER BY name", (self._ws,))
            else:
                cur.execute("SELECT data FROM rya_environments WHERE workspace_id=%s "
                            "AND agent=%s ORDER BY name", (self._ws, agent))
            return [r[0] for r in cur.fetchall()]

    # ---- worker registration (§6) ----------------------------------------
    def worker_register(self, record: dict) -> dict:
        worker = {"id": record.get("id") or _new_id("wrk"), "status": "alive",
                  "startedAt": now_iso(), "lastHeartbeatAt": now_iso(), **record}
        with self._conn.cursor() as cur:
            cur.execute(
                """INSERT INTO rya_workers (id, workspace_id, agent, version_id,
                       bundle_hash, status, started_at, last_heartbeat_at, data)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (id) DO UPDATE SET status=EXCLUDED.status,
                       last_heartbeat_at=EXCLUDED.last_heartbeat_at, data=EXCLUDED.data""",
                (worker["id"], self._ws, worker.get("agent"), worker.get("versionId"),
                 worker.get("bundleHash"), worker["status"], worker["startedAt"],
                 worker["lastHeartbeatAt"], Json(worker)),
            )
        return worker

    def worker_heartbeat(self, worker_id: str, **fields) -> Optional[dict]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT data FROM rya_workers WHERE id=%s AND workspace_id=%s",
                        (worker_id, self._ws))
            row = cur.fetchone()
            if not row:
                return None
            doc = {**row[0], **fields, "lastHeartbeatAt": now_iso()}
            cur.execute("UPDATE rya_workers SET last_heartbeat_at=%s, status=%s, data=%s "
                        "WHERE id=%s AND workspace_id=%s",
                        (doc["lastHeartbeatAt"], doc.get("status", "alive"), Json(doc),
                         worker_id, self._ws))
        return doc

    def worker_deregister(self, worker_id: str, reason: Optional[str] = None) -> Optional[dict]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT data FROM rya_workers WHERE id=%s AND workspace_id=%s",
                        (worker_id, self._ws))
            row = cur.fetchone()
            if not row:
                return None
            doc = {**row[0], "status": "stopped", "stoppedAt": now_iso(), "stopReason": reason}
            cur.execute("UPDATE rya_workers SET status='stopped', data=%s "
                        "WHERE id=%s AND workspace_id=%s", (Json(doc), worker_id, self._ws))
        return doc

    def worker_list(self, agent: Optional[str] = None, version_id: Optional[str] = None,
                    status: Optional[str] = None) -> List[dict]:
        clauses = ["workspace_id=%s"]
        params: List[Any] = [self._ws]
        for col, val in (("agent", agent), ("version_id", version_id), ("status", status)):
            if val is not None:
                clauses.append(f"{col}=%s")
                params.append(val)
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT data FROM rya_workers WHERE {' AND '.join(clauses)} "
                        "ORDER BY started_at DESC", tuple(params))
            return [r[0] for r in cur.fetchall()]
