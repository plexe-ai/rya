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
                       ORDER BY q.priority DESC, q.run_at ASC, (q.data->>'seq') ASC
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
