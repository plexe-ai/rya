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
      journal/<run_id>.jsonl      append-only step log (D10)
      meter/ledger.jsonl          append-only billable-fact ledger (D10)
      policy/<key>.json           privileged platform policy (D7)
      policy/log.jsonl            append-only policy audit trail
      versions/<version_id>.json  immutable content-hashed deployments (D12)
      envs/<name>.json            environment -> current-version pointer (D11)
      workers/<worker_id>.json    worker registration + heartbeat (§6)
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fair_order(pending: List[dict], running: Dict[str, int]) -> List[dict]:
    """Order due jobs so the least-busy ``concurrencyKey`` goes first.

    PLATFORM_DESIGN §6 makes ``concurrency_key``/``concurrency_limit`` "the
    fairness primitive — one workspace must not starve another". The *cap* half
    was already implemented; this is the *ordering* half. Without it, a workspace
    that enqueues ten thousand jobs owns every free slot until its backlog drains,
    because selection was purely (priority, runAt): a caps-only scheme bounds how
    much of the fleet one key holds at once but says nothing about who gets the
    next slot.

    Jobs sharing a key all see the same running count, so ordering WITHIN a key is
    untouched (priority still wins, then age). Fairness only decides between
    *different* keys — which is exactly the starvation case. The trade is explicit:
    a high-priority job on a busy key now yields to a low-priority job on an idle
    one. Priority orders a queue; it was never a claim on the whole fleet.

    Keyless jobs count as one shared bucket, which preserves plain (priority,
    runAt) ordering for anyone not using concurrency keys at all.
    """
    return sorted(pending, key=lambda j: running.get(j.get("concurrencyKey") or "", 0))


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
        # ---- platform state (PLATFORM_DESIGN D7, D10, D11, D12, §6) ----------
        self.journal_dir = self.dir / "journal"
        self.meter_dir = self.dir / "meter"
        self.policy_dir = self.dir / "policy"
        self.versions_dir = self.dir / "versions"
        self.envs_dir = self.dir / "envs"
        self.workers_dir = self.dir / "workers"

    def ensure(self) -> None:
        for d in (self.runs_dir, self.approvals_dir, self.jobs_dir, self.queue_dir,
                  self.streams_dir, self.memory_dir, self.sessions_dir, self.connections_dir,
                  self.files_dir, self.journal_dir, self.meter_dir, self.policy_dir,
                  self.versions_dir, self.envs_dir, self.workers_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ---- low level -----------------------------------------------------
    # Writes are ATOMIC (temp file in the same directory + os.replace). This is
    # not a nicety: `rya dev` runs an api process and a worker against the same
    # FileStore, and `work_once(concurrency=N)` claims from N threads, so a
    # reader concurrent with a writer is the normal case rather than an edge.
    # A plain write_text() truncates first, so a reader could see a half-written
    # or zero-length file and die with a JSONDecodeError. os.replace() swaps the
    # directory entry in one step: a reader gets either the whole old version or
    # the whole new one, never a torn one.
    @staticmethod
    def _read(path: Path) -> Optional[dict]:
        try:
            raw = path.read_text()
        except FileNotFoundError:
            # Deleted between the caller's listing and this read (a job claimed
            # and archived by another worker) — indistinguishable from absent.
            return None
        except IsADirectoryError:
            return None
        if not raw.strip():
            # Only reachable for a file written by a pre-atomic build that
            # crashed mid-write. Treat as absent rather than raising.
            return None
        return json.loads(raw)

    @staticmethod
    def _write(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(data, indent=2, default=str)
        # Same directory as the target, so os.replace() stays within one
        # filesystem and is therefore atomic.
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(body)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

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

    def run_counts(self, since: Optional[str] = None) -> Dict[str, int]:
        """Runs per status, optionally only those created at/after ``since``.

        Exists so a quota check (§11.12) is a counting query rather than a full
        materialisation of every run: admission runs on the hot path, and
        ``list_runs`` loads every run document to answer "how many".
        """
        counts: Dict[str, int] = {}
        for p in self.runs_dir.glob("run_*.json"):
            data = self._read(p)
            if not data:
                continue
            if since is not None and (data.get("createdAt") or "") < since:
                continue
            status = data.get("status") or "?"
            counts[status] = counts.get(status, 0) + 1
        return counts

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
        from . import files_s3
        self.files_dir.mkdir(parents=True, exist_ok=True)
        if files_s3.bucket():
            if content:  # presigned uploads register metadata first; bytes arrive via S3 PUT
                files_s3.put_bytes(meta["id"], content, meta["contentType"])
            meta["storage"] = "s3"
        else:
            (self.files_dir / meta["id"]).write_bytes(content)
        self._write(self.files_dir / f"{meta['id']}.json", meta)
        return meta

    def get_file(self, file_id: str) -> Optional[dict]:
        return self._read(self.files_dir / f"{file_id}.json")

    def read_file(self, file_id: str) -> Optional[bytes]:
        meta = self.get_file(file_id)
        if meta is None:
            return None
        if meta.get("storage") == "s3":
            from . import files_s3
            return files_s3.get_bytes(file_id)
        p = self.files_dir / file_id
        return p.read_bytes() if p.is_file() else None

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
                   max_attempts: int = 3, group_id: Optional[str] = None) -> dict:
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
            "groupId": group_id,
        }
        self._write(self.jobs_dir / f"{job['id']}.json", job)
        return job

    # ---- job groups (fan-in): remaining counter + exactly-once fire --------
    def create_job_group(self, on_complete: dict, count: int) -> dict:
        group = {"id": _new_id("grp"), "remaining": count, "fired": False,
                 "failed": False, "onComplete": on_complete, "createdAt": now_iso()}
        self._write(self.jobs_dir / f"{group['id']}.json", group)
        return group

    _group_lock = threading.Lock()  # file backend: serialize the decrement

    def group_job_done(self, group_id: str, success: bool = True) -> Optional[dict]:
        """Decrement on success; mark failed on terminal failure. Thread-safe
        within a process via the lock; Postgres is the multi-process-safe one."""
      # noqa
        with FileStore._group_lock:
            return self._group_job_done_locked(group_id, success)

    def _group_job_done_locked(self, group_id: str, success: bool = True) -> Optional[dict]:
        path = self.jobs_dir / f"{group_id}.json"
        group = self._read(path)
        if group is None:
            return None
        if not success:
            group["failed"] = True
            self._write(path, group)
            return {"fire": False}
        group["remaining"] = max(0, group["remaining"] - 1)
        fire = group["remaining"] == 0 and not group["fired"] and not group["failed"]
        if fire:
            group["fired"] = True
        self._write(path, group)
        return {"fire": fire, "onComplete": group["onComplete"]}

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
        single-process dev). Respects per-concurrencyKey running caps, and picks
        the LEAST busy key first — see ``_fair_order``."""
        running = {}
        for j in self.queue_list("running"):
            k = j.get("concurrencyKey")
            if k:
                running[k] = running.get(k, 0) + 1
        for job in _fair_order(self.queue_list("pending"), running):
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

    def upsert_connection(self, provider: str, scopes: List[str], secret: Optional[str] = None,
                          owner: Optional[str] = None, label: Optional[str] = None) -> dict:
        """Overwrite-in-place the active connection for (provider, owner), or create
        one if none exists. Keyed on (provider, owner) — NOT the random id — so a
        user re-logging in refreshes the same doc instead of minting duplicates
        (which would let get_connection later inject a stale token). Preserves the
        existing id/createdAt when overwriting; stamps updatedAt."""
        from .seal import seal
        existing_path = None
        for p in self.connections_dir.glob("conn_*.json"):
            doc = self._read(p)
            if (doc and doc.get("provider") == provider and doc.get("owner") == owner
                    and doc.get("status") == "active"):
                existing_path = p
                existing = doc
                break
        conn = {
            "id": existing["id"] if existing_path else _new_id("conn"),
            "provider": provider, "owner": owner,
            "scopes": list(scopes or []), "label": label or provider,
            "secret": seal(secret, self.root), "status": "active",
            "createdAt": existing["createdAt"] if existing_path else now_iso(),
            "updatedAt": now_iso(),
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

    # ======================================================================
    # Platform state — the tables PLATFORM_DESIGN §11 items 2, 4, 8, 9 and 10
    # need. Deliberately grouped and duck-typed the same way as everything
    # above, so PostgresStore can mirror the surface without an ABC.
    # ======================================================================

    # ---- append-only journal (D10) ---------------------------------------
    # `save_run` rewrites the whole run as one blob, which is fine for a run
    # summary and wrong for a commit path: a step needs an APPEND. Entries are
    # revisioned rather than overwritten, so an approval's pending -> approved
    # transition adds a row instead of destroying the prior one. Readers take the
    # highest revision per seq; auditors read them all.
    @staticmethod
    def _append_jsonl(path: Path, record: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    @staticmethod
    def _read_jsonl(path: Path) -> List[dict]:
        if not path.is_file():
            return []
        out = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:  # a torn final write; ignore the tail
                    continue
        return out

    def journal_append(self, run_id: str, entry: dict) -> dict:
        path = self.journal_dir / f"{run_id}.jsonl"
        prior = [e for e in self._read_jsonl(path) if e.get("seq") == entry.get("seq")]
        record = {**entry, "runId": run_id,
                  "revision": max((int(e.get("revision") or 0) for e in prior), default=-1) + 1,
                  "appendedAt": now_iso()}
        self._append_jsonl(path, record)
        return record

    def journal_read(self, run_id: str) -> Dict[str, dict]:
        """The materialized journal: highest revision per seq, keyed by str(seq)
        so it drops straight into ``run["journal"]``."""
        latest: Dict[str, dict] = {}
        for e in self._read_jsonl(self.journal_dir / f"{run_id}.jsonl"):
            key = str(e.get("seq"))
            if key not in latest or int(e.get("revision") or 0) >= int(latest[key].get("revision") or 0):
                latest[key] = e
        return latest

    def journal_revisions(self, run_id: str) -> List[dict]:
        """Every revision ever appended, in write order — the audit view."""
        return self._read_jsonl(self.journal_dir / f"{run_id}.jsonl")

    # ---- durable meter (D10) ---------------------------------------------
    # Billing must not be derived from `run["trace"]`, which is a debugging
    # artifact that gets redacted, truncated and rewritten. A billable fact is
    # written here once, immutably, at the moment it happens.
    def meter_append(self, record: dict) -> dict:
        rec = {"id": _new_id("mtr"), "ts": now_iso(), **record}
        self._append_jsonl(self.meter_dir / "ledger.jsonl", rec)
        return rec

    def meter_read(self, run_id: Optional[str] = None, since: Optional[str] = None,
                   until: Optional[str] = None, limit: int = 1000) -> List[dict]:
        out = []
        for rec in self._read_jsonl(self.meter_dir / "ledger.jsonl"):
            if run_id is not None and rec.get("runId") != run_id:
                continue
            if since is not None and (rec.get("ts") or "") < since:
                continue
            if until is not None and (rec.get("ts") or "") > until:
                continue
            out.append(rec)
        return out[-limit:]

    def meter_totals(self, since: Optional[str] = None, until: Optional[str] = None,
                     group_by: Optional[str] = None) -> dict:
        """Summed billable facts, optionally bucketed by a record field
        (``model``, ``agent``, ``agentVersion``, ``kind``)."""
        buckets: Dict[str, dict] = {}
        for rec in self.meter_read(since=since, until=until, limit=10 ** 9):
            key = str(rec.get(group_by)) if group_by else "_total"
            b = buckets.setdefault(key, {"inputTokens": 0, "outputTokens": 0,
                                         "costUsd": 0.0, "calls": 0})
            b["inputTokens"] += int(rec.get("inputTokens") or 0)
            b["outputTokens"] += int(rec.get("outputTokens") or 0)
            b["costUsd"] += float(rec.get("costUsd") or 0.0)
            b["calls"] += 1
        return buckets if group_by else buckets.get("_total", {
            "inputTokens": 0, "outputTokens": 0, "costUsd": 0.0, "calls": 0})

    # ---- privileged policy (D7, §11.2) -----------------------------------
    # Kill switches, guard policy and per-environment config are PLATFORM state.
    # They lived in the generic `_runtime_config` memory scope, which a bundle can
    # write through `ctx.memory.set` — governance a client can edit is not
    # governance. These live in their own namespace with an append-only audit
    # trail, and `ctx.memory` refuses reserved scopes (see sdk/context.py).
    def policy_get(self, key: str) -> Optional[dict]:
        doc = self._read(self.policy_dir / f"{key}.json")
        return doc.get("value") if doc else None

    def policy_set(self, key: str, value: Optional[dict], actor: Optional[str] = None) -> dict:
        prior = self._read(self.policy_dir / f"{key}.json")
        record = {
            "key": key,
            "value": value,
            "version": int((prior or {}).get("version") or 0) + 1,
            "actor": actor,
            "previous": (prior or {}).get("value"),
            "changedAt": now_iso(),
        }
        self._write(self.policy_dir / f"{key}.json", record)
        # §12 risk 7: "who reviewed this allowlist change" is a feature, so every
        # write lands in an append-only log the pointer write cannot destroy.
        self._append_jsonl(self.policy_dir / "log.jsonl", record)
        return record

    def policy_all(self) -> Dict[str, Any]:
        out = {}
        for p in sorted(self.policy_dir.glob("*.json")):
            doc = self._read(p)
            if doc and doc.get("value") is not None:
                out[doc["key"]] = doc["value"]
        return out

    def policy_history(self, key: Optional[str] = None, limit: int = 50) -> List[dict]:
        log = self._read_jsonl(self.policy_dir / "log.jsonl")
        if key is not None:
            log = [r for r in log if r.get("key") == key]
        return log[-limit:][::-1]

    # ---- deployments: immutable versions (D12) ---------------------------
    def version_create(self, record: dict) -> dict:
        """Record an immutable, content-hashed version. Idempotent: re-recording
        the same (agent, bundleHash) returns the existing row untouched, which is
        what makes `rya deploy` safe to retry."""
        existing = self.version_by_hash(record["agent"], record["bundleHash"])
        if existing is not None:
            return existing
        version = {
            "id": _new_id("ver"),
            "state": "active",
            "createdAt": now_iso(),
            "retiredAt": None,
            **record,
        }
        self._write(self.versions_dir / f"{version['id']}.json", version)
        return version

    def version_get(self, version_id: str) -> Optional[dict]:
        return self._read(self.versions_dir / f"{version_id}.json")

    def version_by_hash(self, agent: str, bundle_hash: str) -> Optional[dict]:
        for p in self.versions_dir.glob("ver_*.json"):
            doc = self._read(p)
            if doc and doc.get("agent") == agent and doc.get("bundleHash") == bundle_hash:
                return doc
        return None

    def version_list(self, agent: Optional[str] = None, state: Optional[str] = None) -> List[dict]:
        out = []
        for p in self.versions_dir.glob("ver_*.json"):
            doc = self._read(p)
            if doc and (agent is None or doc.get("agent") == agent) \
                    and (state is None or doc.get("state") == state):
                out.append(doc)
        out.sort(key=lambda v: v.get("createdAt", ""), reverse=True)
        return out

    def version_set_state(self, version_id: str, state: str) -> Optional[dict]:
        doc = self.version_get(version_id)
        if doc is None:
            return None
        doc["state"] = state
        doc["retiredAt"] = now_iso() if state == "retired" else None
        self._write(self.versions_dir / f"{version_id}.json", doc)
        return doc

    # ---- promotion gate evidence (§9) ------------------------------------
    # §9 turns the readiness gate into "a server-side admission check rather
    # than a client-side courtesy" and lets evals gate staging→prod. An
    # attestation is the evidence a check ran, and it is filed against a
    # VERSION ID — which, because version_create is idempotent on
    # (agent, bundleHash), is a 1:1 handle on the exact content. That binding is
    # the whole security property: you cannot satisfy prod's gate by running
    # evals against a different tree, because the attestation would be filed
    # against a different version.
    #
    # Append-only for the same reason the policy log is: an override or a failed
    # check must stay visible after a later passing one.
    def version_attest(self, version_id: str, attestation: dict) -> dict:
        record = {"id": _new_id("att"), "versionId": version_id,
                  "createdAt": now_iso(), **attestation}
        self._append_jsonl(self.versions_dir / "attestations" / f"{version_id}.jsonl", record)
        return record

    def version_attestations(self, version_id: str, kind: Optional[str] = None) -> List[dict]:
        """Every attestation for a version, in write order (oldest first)."""
        out = self._read_jsonl(self.versions_dir / "attestations" / f"{version_id}.jsonl")
        return [r for r in out if kind is None or r.get("kind") == kind]

    # ---- deployments: environments (D11) ---------------------------------
    # One *current* version pointer per (environment, agent). A promote is a
    # pointer flip and a rollback is the same flip backwards, which is why the
    # prior pointer is kept in `history` rather than overwritten.
    def _env_key(self, name: str, agent: str) -> str:
        return f"{name}__{agent}".replace("/", "_")

    def env_get(self, name: str, agent: str) -> Optional[dict]:
        return self._read(self.envs_dir / f"{self._env_key(name, agent)}.json")

    def env_set_current(self, name: str, agent: str, version_id: str,
                        actor: Optional[str] = None) -> dict:
        doc = self.env_get(name, agent) or {"name": name, "agent": agent,
                                            "currentVersionId": None, "history": [],
                                            "createdAt": now_iso()}
        if doc.get("currentVersionId"):
            doc["history"].append({"versionId": doc["currentVersionId"],
                                   "replacedAt": now_iso(), "actor": actor})
        doc["currentVersionId"] = version_id
        doc["updatedAt"] = now_iso()
        doc["actor"] = actor
        self._write(self.envs_dir / f"{self._env_key(name, agent)}.json", doc)
        return doc

    def env_list(self, agent: Optional[str] = None) -> List[dict]:
        out = []
        for p in sorted(self.envs_dir.glob("*.json")):
            doc = self._read(p)
            if doc and (agent is None or doc.get("agent") == agent):
                out.append(doc)
        return out

    # ---- worker registration (§6) ----------------------------------------
    # `queue.claim` takes a bare worker_id string that is never registered or
    # validated. A worker now registers what it actually is — bundle hash and
    # handler set — so "the image is missing a handler" is a startup failure and
    # an operator can see which version is live for which key.
    def worker_register(self, record: dict) -> dict:
        worker = {
            "id": record.get("id") or _new_id("wrk"),
            "status": "alive",
            "startedAt": now_iso(),
            "lastHeartbeatAt": now_iso(),
            **record,
        }
        self._write(self.workers_dir / f"{worker['id']}.json", worker)
        return worker

    def worker_heartbeat(self, worker_id: str, **fields) -> Optional[dict]:
        doc = self._read(self.workers_dir / f"{worker_id}.json")
        if doc is None:
            return None
        doc.update(fields)
        doc["lastHeartbeatAt"] = now_iso()
        self._write(self.workers_dir / f"{worker_id}.json", doc)
        return doc

    def worker_deregister(self, worker_id: str, reason: Optional[str] = None) -> Optional[dict]:
        doc = self._read(self.workers_dir / f"{worker_id}.json")
        if doc is None:
            return None
        doc["status"] = "stopped"
        doc["stoppedAt"] = now_iso()
        doc["stopReason"] = reason
        self._write(self.workers_dir / f"{worker_id}.json", doc)
        return doc

    def worker_list(self, agent: Optional[str] = None, version_id: Optional[str] = None,
                    status: Optional[str] = None) -> List[dict]:
        out = []
        for p in self.workers_dir.glob("wrk_*.json"):
            doc = self._read(p)
            if not doc:
                continue
            if agent is not None and doc.get("agent") != agent:
                continue
            if version_id is not None and doc.get("versionId") != version_id:
                continue
            if status is not None and doc.get("status") != status:
                continue
            out.append(doc)
        out.sort(key=lambda w: w.get("startedAt", ""), reverse=True)
        return out

    def describe(self) -> dict:
        return {"backend": "file", "location": str(self.dir)}


# Back-compat alias: `Store` has always meant the file backend.
Store = FileStore
