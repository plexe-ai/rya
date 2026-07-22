"""Filesystem-backed store for the local runtime.

Everything the runtime needs to survive across CLI invocations lives under
``.rya/`` in the project root (next to ``rya.agent.yaml``). There is no daemon:
every command reads and writes this directory, which is what makes pause/resume
and ``rya runs trace`` work across separate process invocations.

Layout::

    .rya/
      runs/<run_id>.json
      approvals/<approval_id>.json
      jobs/<job_id>.json
      queue/<job_id>.json
      memory/<scope>.json
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def open_store(root: Path):
    """Return the configured state store (substrate-agnostic).

    The SAME runtime runs on any of these — the only difference is durability and
    where it's hosted. This is the seam that makes the OSS self-host and the
    managed cloud the same code:

    - ``RYA_DATABASE_URL`` / ``DATABASE_URL`` set -> PostgresStore
      (plain Postgres for self-host, managed Postgres for cloud)
    - otherwise -> FileStore under ``<root>/.rya`` (zero-config local dev)
    """
    import os

    url = os.environ.get("RYA_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if url:
        from .store_postgres import PostgresStore

        store = PostgresStore(url)
        store.ensure()
        return store
    store = FileStore(root)
    store.ensure()
    return store


class FileStore:
    """File-backed store (zero-config local / OSS dev). Default backend."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.dir = self.root / ".rya"
        self.runs_dir = self.dir / "runs"
        self.approvals_dir = self.dir / "approvals"
        self.jobs_dir = self.dir / "jobs"
        self.queue_dir = self.dir / "queue"
        self.streams_dir = self.dir / "streams"
        self.memory_dir = self.dir / "memory"
        self.sessions_dir = self.dir / "sessions"
        self.connections_dir = self.dir / "connections"
        self.files_dir = self.dir / "files"

    def ensure(self) -> None:
        for d in (self.runs_dir, self.approvals_dir, self.jobs_dir, self.queue_dir,
                  self.streams_dir, self.memory_dir, self.sessions_dir, self.connections_dir,
                  self.files_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ---- low level -----------------------------------------------------
    @staticmethod
    def _read(path: Path) -> Optional[dict]:
        if not path.is_file():
            return None
        return json.loads(path.read_text())

    @staticmethod
    def _write(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str))

    # ---- runs ----------------------------------------------------------
    def new_run_id(self) -> str:
        return _new_id("run")

    def save_run(self, run: dict) -> None:
        run["updatedAt"] = now_iso()
        self._write(self.runs_dir / f"{run['id']}.json", run)

    def get_run(self, run_id: str) -> Optional[dict]:
        return self._read(self.runs_dir / f"{run_id}.json")

    def list_runs(self, agent: Optional[str] = None) -> List[dict]:
        runs = []
        for p in sorted(self.runs_dir.glob("run_*.json")):
            data = self._read(p)
            if data and (agent is None or data.get("agent") == agent):
                runs.append(data)
        runs.sort(key=lambda r: r.get("createdAt", ""), reverse=True)
        return runs

    # ---- files (uploaded documents) ------------------------------------
    # Files are immutable once saved: handlers may re-read them on replay and
    # get identical bytes, so reads do not need to be journaled.
    def save_file(self, name: str, content: bytes, content_type: Optional[str] = None,
                  tags: Optional[dict] = None) -> dict:
        import hashlib
        meta = {"id": _new_id("file"), "name": name,
                "contentType": content_type or "application/octet-stream",
                "size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                "tags": tags or {}, "createdAt": now_iso()}
        (self.files_dir / meta["id"]).write_bytes(content)
        self._write(self.files_dir / f"{meta['id']}.json", meta)
        return meta

    def get_file(self, file_id: str) -> Optional[dict]:
        return self._read(self.files_dir / f"{file_id}.json")

    def read_file(self, file_id: str) -> Optional[bytes]:
        p = self.files_dir / file_id
        return p.read_bytes() if p.is_file() and self.get_file(file_id) else None

    def list_files(self, tags: Optional[dict] = None) -> List[dict]:
        out = []
        for p in sorted(self.files_dir.glob("file_*.json")):
            meta = self._read(p)
            if meta and (not tags or all((meta.get("tags") or {}).get(k) == v for k, v in tags.items())):
                out.append(meta)
        out.sort(key=lambda m: m.get("createdAt", ""), reverse=True)
        return out

    # ---- approvals -----------------------------------------------------
    def create_approval(self, run_id: str, title: str, body: str, action: dict) -> dict:
        approval = {
            "id": _new_id("apr"),
            "runId": run_id,
            "title": title,
            "body": body,
            "action": action,
            "status": "pending",
            "createdAt": now_iso(),
            "resolvedAt": None,
            "actionResult": None,
        }
        self._write(self.approvals_dir / f"{approval['id']}.json", approval)
        return approval

    def get_approval(self, approval_id: str) -> Optional[dict]:
        return self._read(self.approvals_dir / f"{approval_id}.json")

    def save_approval(self, approval: dict) -> None:
        self._write(self.approvals_dir / f"{approval['id']}.json", approval)

    def list_approvals(self, status: Optional[str] = None) -> List[dict]:
        out = []
        for p in sorted(self.approvals_dir.glob("apr_*.json")):
            data = self._read(p)
            if data and (status is None or data.get("status") == status):
                out.append(data)
        out.sort(key=lambda a: a.get("createdAt", ""), reverse=True)
        return out

    # ---- jobs ----------------------------------------------------------
    def create_job(self, run_id: str, handler: str, payload: dict, run_at: str,
                   max_attempts: int = 3) -> dict:
        job = {
            "id": _new_id("job"),
            "parentRunId": run_id,
            "handler": handler,
            "payload": payload,
            "status": "pending",
            "runAt": run_at,
            "attempts": 0,
            "maxAttempts": max_attempts,
            "lastError": None,
            "createdAt": now_iso(),
            "resultRunId": None,
        }
        self._write(self.jobs_dir / f"{job['id']}.json", job)
        return job

    def get_job(self, job_id: str) -> Optional[dict]:
        return self._read(self.jobs_dir / f"{job_id}.json")

    def save_job(self, job: dict) -> None:
        self._write(self.jobs_dir / f"{job['id']}.json", job)

    def list_jobs(self, status: Optional[str] = None) -> List[dict]:
        out = []
        for p in sorted(self.jobs_dir.glob("job_*.json")):
            data = self._read(p)
            if data and (status is None or data.get("status") == status):
                out.append(data)
        out.sort(key=lambda j: j.get("runAt", ""))
        return out

    def claim_due_job(self) -> Optional[dict]:
        """Claim one due pending job (best-effort; the file store is single-worker)."""
        now = now_iso()
        for job in self.list_jobs("pending"):
            if (job.get("runAt") or "") <= now:
                job["status"] = "claimed"
                self.save_job(job)
                return job
        return None

    # ---- queue: durable jobs for EXTERNAL workers ------------------------
    # Unlike `jobs` (handler-bound, executed by `rya worker` in-process), queue
    # jobs are claimed and executed by external workers in any language over the
    # HTTP API. Lifecycle lives in rya.queue; the store owns atomic claiming.
    def queue_save(self, job: dict) -> None:
        job["updatedAt"] = now_iso()
        self._write(self.queue_dir / f"{job['id']}.json", job)

    def queue_get(self, job_id: str) -> Optional[dict]:
        return self._read(self.queue_dir / f"{job_id}.json")

    def queue_list(self, status: Optional[str] = None, type: Optional[str] = None) -> List[dict]:
        out = []
        for p in self.queue_dir.glob("*.json"):
            data = self._read(p)
            if data and (status is None or data.get("status") == status) \
                    and (type is None or data.get("type") == type):
                out.append(data)
        out.sort(key=lambda j: (-int(j.get("priority") or 0), j.get("runAt") or "",
                                int(j.get("seq") or 0)))
        return out

    def queue_reap(self, now: str) -> None:
        """Return expired-lease running jobs to the queue, or dead-letter them if
        their attempts are already exhausted."""
        for job in self.queue_list("running"):
            if (job.get("leaseExpiresAt") or "9999") <= now:
                job["workerId"] = None
                job["leaseExpiresAt"] = None
                if int(job.get("attempts") or 0) >= int(job.get("maxAttempts") or 1):
                    job["status"] = "failed"
                    job["deadLetter"] = True
                    job["error"] = job.get("error") or "lease expired"
                    job["completedAt"] = now
                else:
                    job["status"] = "pending"
                    job["lastError"] = "lease expired"
                    job["runAt"] = now
                self.queue_save(job)

    def queue_claim_one(self, worker_id: str, now: str, lease_expires_at: str,
                        types: Optional[List[str]] = None) -> Optional[dict]:
        """Claim one due job (best-effort atomicity; the file store is for local
        single-process dev). Respects per-concurrencyKey running caps."""
        running = {}
        for j in self.queue_list("running"):
            k = j.get("concurrencyKey")
            if k:
                running[k] = running.get(k, 0) + 1
        for job in self.queue_list("pending"):
            if types and job.get("type") not in types:
                continue
            if (job.get("runAt") or "") > now:
                continue
            key = job.get("concurrencyKey")
            if key:
                limit = int(job.get("concurrencyLimit") or 0) or None
                if limit is not None and running.get(key, 0) >= limit:
                    continue
            job["status"] = "running"
            job["workerId"] = worker_id
            job["leaseExpiresAt"] = lease_expires_at
            job["attempts"] = int(job.get("attempts") or 0) + 1
            job["startedAt"] = job.get("startedAt") or now
            self.queue_save(job)
            return job
        return None

    def queue_counts(self) -> dict:
        counts: Dict[str, int] = {}
        for p in self.queue_dir.glob("*.json"):
            data = self._read(p)
            if data:
                counts[data.get("status", "?")] = counts.get(data.get("status", "?"), 0) + 1
        return counts

    # ---- durable turn stream buffer --------------------------------------
    # A chat turn executes on a leased worker (see rya.turns) and relays its
    # frames here; the streaming endpoint TAILS this buffer by monotonic seq.
    # That decouples the stream connection from the executor, so a dropped
    # connection resumes from its last seq and a crashed executor's re-run just
    # appends more frames. One writer per turn (the lease holder), so seq is safe.
    def stream_append(self, turn_id: str, frames: List[dict]) -> int:
        p = self.streams_dir / f"{turn_id}.json"
        doc = self._read(p) or {"turnId": turn_id, "frames": []}
        base = len(doc["frames"])
        for i, f in enumerate(frames):
            doc["frames"].append({"seq": base + i, "kind": f["kind"],
                                  "data": f.get("data"), "ts": now_iso()})
        self._write(p, doc)
        return base + len(frames)

    def stream_read(self, turn_id: str, after_seq: int = -1) -> List[dict]:
        doc = self._read(self.streams_dir / f"{turn_id}.json")
        if not doc:
            return []
        return [f for f in doc["frames"] if f["seq"] > after_seq]

    # ---- memory --------------------------------------------------------
    def _memory_path(self, scope: str) -> Path:
        return self.memory_dir / f"{scope}.json"

    def load_memory(self, scope: str) -> Dict[str, Any]:
        data = self._read(self._memory_path(scope))
        if data is None:
            data = {"kv": {}, "collections": {}}
        return data

    def save_memory(self, scope: str, data: Dict[str, Any]) -> None:
        self._write(self._memory_path(scope), data)

    def list_memory_scopes(self) -> List[str]:
        return sorted(p.stem for p in self.memory_dir.glob("*.json"))

    # ---- conversations: sessions + messages ----------------------------
    def _session_path(self, sid: str) -> Path:
        return self.sessions_dir / f"{sid}.json"

    def create_session(self, agent: str, channel: str, external_id: Optional[str],
                       owner: Optional[str] = None, title: Optional[str] = None) -> dict:
        session = {
            "id": _new_id("ses"), "agent": agent, "channel": channel,
            "externalId": external_id, "owner": owner, "title": title or "Conversation",
            "status": "open", "createdAt": now_iso(), "lastMessageAt": now_iso(),
            "messageCount": 0,
        }
        self._write(self._session_path(session["id"]), {**session, "messages": []})
        return session

    def get_session(self, sid: str) -> Optional[dict]:
        return self._read(self._session_path(sid))

    def list_sessions(self, agent: Optional[str] = None) -> List[dict]:
        out = []
        for p in self.sessions_dir.glob("ses_*.json"):
            doc = self._read(p)
            if doc and (agent is None or doc.get("agent") == agent):
                out.append({k: v for k, v in doc.items() if k != "messages"})
        out.sort(key=lambda s: s.get("lastMessageAt", ""), reverse=True)
        return out

    def find_session(self, agent: str, channel: str, external_id: str) -> Optional[dict]:
        for p in self.sessions_dir.glob("ses_*.json"):
            doc = self._read(p)
            if doc and doc.get("agent") == agent and doc.get("channel") == channel \
                    and doc.get("externalId") == external_id:
                return {k: v for k, v in doc.items() if k != "messages"}
        return None

    def append_message(self, sid: str, role: str, content: str, **extra) -> dict:
        doc = self._read(self._session_path(sid))
        if doc is None:
            raise KeyError(sid)
        msg = {"id": _new_id("msg"), "seq": len(doc["messages"]), "role": role,
               "content": content, "ts": now_iso(), **extra}
        doc["messages"].append(msg)
        doc["messageCount"] = len(doc["messages"])
        doc["lastMessageAt"] = msg["ts"]
        self._write(self._session_path(sid), doc)
        return msg

    def list_messages(self, sid: str) -> List[dict]:
        doc = self._read(self._session_path(sid))
        return doc.get("messages", []) if doc else []

    # ---- connections: scoped, encrypted-at-rest credentials ------------
    @staticmethod
    def _public_connection(doc: dict) -> dict:
        """A connection WITHOUT its secret — safe to list/return to callers."""
        from .seal import is_sealed
        return ({k: v for k, v in doc.items() if k != "secret"}
                | {"secretSet": bool(doc.get("secret")),
                   "encrypted": is_sealed(doc.get("secret"))})

    def create_connection(self, provider: str, scopes: List[str], secret: Optional[str] = None,
                          owner: Optional[str] = None, label: Optional[str] = None) -> dict:
        from .seal import seal
        conn = {
            "id": _new_id("conn"), "provider": provider, "owner": owner,
            "scopes": list(scopes or []), "label": label or provider,
            "secret": seal(secret, self.root), "status": "active", "createdAt": now_iso(),
        }
        self._write(self.connections_dir / f"{conn['id']}.json", conn)
        return self._public_connection(conn)

    def get_connection(self, provider: str, owner: Optional[str] = None) -> Optional[dict]:
        """Resolve the connection for (provider, owner). A user-owned connection
        wins over a workspace-shared one (owner IS NULL); returns it WITH the
        decrypted secret for runtime injection — never expose this to a
        handler/model."""
        from .seal import unseal
        shared = None
        for p in self.connections_dir.glob("conn_*.json"):
            doc = self._read(p)
            if not doc or doc.get("provider") != provider or doc.get("status") != "active":
                continue
            if owner is not None and doc.get("owner") == owner:
                return {**doc, "secret": unseal(doc.get("secret"), self.root)}
            if doc.get("owner") is None:
                shared = doc
        return {**shared, "secret": unseal(shared.get("secret"), self.root)} if shared else None

    def list_connections(self) -> List[dict]:
        out = []
        for p in self.connections_dir.glob("conn_*.json"):
            doc = self._read(p)
            if doc:
                out.append(self._public_connection(doc))
        out.sort(key=lambda c: c.get("createdAt", ""), reverse=True)
        return out

    def revoke_connection(self, conn_id: str) -> bool:
        p = self.connections_dir / f"{conn_id}.json"
        doc = self._read(p)
        if doc is None:
            return False
        doc["status"] = "revoked"
        doc["secret"] = None  # destroy the credential on revoke
        self._write(p, doc)
        return True

    def reseal_connections(self) -> dict:
        """Encrypt any legacy plaintext secrets at rest. Idempotent: already-sealed
        and secret-less rows are left untouched."""
        from .seal import is_sealed, seal
        scanned = resealed = already = empty = 0
        for p in self.connections_dir.glob("conn_*.json"):
            doc = self._read(p)
            if not doc:
                continue
            scanned += 1
            sec = doc.get("secret")
            if not sec:
                empty += 1
                continue
            if is_sealed(sec):
                already += 1
                continue
            new = seal(sec, self.root)
            if is_sealed(new):  # only if cryptography actually sealed it
                doc["secret"] = new
                self._write(p, doc)
                resealed += 1
        return {"scanned": scanned, "resealed": resealed,
                "alreadyEncrypted": already, "noSecret": empty}

    def describe(self) -> dict:
        return {"backend": "file", "location": str(self.dir)}


# Back-compat alias: `Store` has always meant the file backend.
Store = FileStore
