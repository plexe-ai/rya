"""Multi-tenancy: workspaces, API keys, and Row-Level-Security provisioning.

This is the layer that sits **above** the substrate-agnostic core (per the
architecture): the core never knows about tenants; this module makes one deployed
agent serve many isolated customers. It lives in the OSS core but only activates
on Postgres — single-tenant/local use never touches it.

- A **workspace** is a tenant. Every run/approval/job/memory row carries its
  ``workspace_id`` (see store_postgres).
- An **API key** (``rya_sk_…``) maps to exactly one workspace. Only the SHA-256
  hash is stored; the plaintext is shown once at creation.
- ``setup()`` installs RLS + ``FORCE`` + per-table policies and a non-superuser
  ``rya_app`` role, so tenant isolation is enforced by Postgres itself — not just
  app code. The data plane connects as ``rya_app`` with ``app.workspace_id`` set.

Requires the [postgres] extra.
"""

from __future__ import annotations

import hashlib
import os
from typing import List, Optional

from .store import now_iso, _new_id

try:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
except ImportError as exc:  # pragma: no cover
    raise ImportError("Tenancy requires the [postgres] extra: pip install 'rya[postgres]'") from exc

from .store_postgres import _SCHEMA as _DATA_SCHEMA

_DATA_TABLES = ["rya_runs", "rya_approvals", "rya_jobs", "rya_queue", "rya_stream",
                "rya_memory", "rya_sessions", "rya_messages", "rya_connections", "rya_files", "rya_job_groups",
                # Platform state (PLATFORM_DESIGN D7, D10, D11, D12, §6) — RLS
                # applies to every one of these exactly as it does to run data.
                "rya_journal", "rya_meter", "rya_policy", "rya_policy_log",
                "rya_versions", "rya_version_attestations", "rya_environments",
                "rya_workers",
                # Phase 5: named leases (the supervisor's singleton guard). RLS'd like
                # everything else — a lease names a workspace's fleet, so one tenant
                # reading or breaking another's would be a cross-tenant scheduling
                # path even though the row holds no tenant data.
                "rya_leases"]

# PLATFORM_DESIGN §8: "the commit path connects as a distinct write-privileged
# Postgres role, separate from the read path, so the runtime cannot perform
# privileged writes even if its policy code is wrong."
#
# These are the tables that DECIDE rather than record: kill switches, guard
# policy, per-environment config, the immutable version ledger and the
# environment pointers. The `api` process (control plane) writes them; the
# `worker` process — the one that loads client bundles — gets SELECT only, so a
# bug in worker-side policy code cannot escalate into a policy WRITE. This is a
# database-level backstop for the code-level rule in §3 (no client-versioned code
# holds a store handle).
_GOVERNANCE_TABLES = ["rya_policy", "rya_policy_log", "rya_versions", "rya_environments"]

_TENANCY_SCHEMA = """
CREATE TABLE IF NOT EXISTS rya_users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT
);
-- D29: the BILLING entity, above the workspace. An organization owns many
-- workspaces; a workspace remains the ISOLATION boundary. Deliberately not in
-- `_DATA_TABLES` and so carrying no RLS policy: like `rya_workspaces`, it is
-- admin-plane state reached over the privileged connection, never by tenant
-- code. See MULTITENANT_DESIGN's D29 split rule for which concern follows which.
CREATE TABLE IF NOT EXISTS rya_organizations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT
);
-- Phase 5: the org's BUDGET, which is the first thing to read `org_id` and the
-- reason D29 exists. Money limits only (see `rya.orgs._BUDGET_FIELDS`): tokens and
-- cost aggregate cleanly across workspaces because they are summed from a durable
-- ledger after the fact, and concurrency does not.
ALTER TABLE rya_organizations ADD COLUMN IF NOT EXISTS budget JSONB;
CREATE TABLE IF NOT EXISTS rya_workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner_user_id TEXT,
    created_at TEXT
);
ALTER TABLE rya_workspaces ADD COLUMN IF NOT EXISTS owner_user_id TEXT;
-- NULLABLE, and nothing reads it yet (Phase 5's quota work is the first
-- consumer). It lands now because it is cheapest while every workspace still maps
-- to exactly one org: adding it once tenants and invoices exist means a backfill
-- against live data with a billing boundary already in use.
ALTER TABLE rya_workspaces ADD COLUMN IF NOT EXISTS org_id TEXT
    REFERENCES rya_organizations(id);
CREATE INDEX IF NOT EXISTS idx_workspaces_org ON rya_workspaces (org_id);
CREATE TABLE IF NOT EXISTS rya_api_keys (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES rya_workspaces(id) ON DELETE CASCADE,
    key_hash TEXT NOT NULL UNIQUE,
    label TEXT,
    created_at TEXT
);
ALTER TABLE rya_api_keys ADD COLUMN IF NOT EXISTS created_by TEXT;
CREATE TABLE IF NOT EXISTS rya_workspace_members (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES rya_workspaces(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    user_id TEXT,               -- NULL until the invite is claimed at signup/login
    role TEXT NOT NULL DEFAULT 'member',
    invited_by TEXT,
    created_at TEXT,
    UNIQUE (workspace_id, email)
);
"""


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def app_db_password() -> str:
    """Password for the non-superuser rya_app data-plane role. Strong and
    consistent in production (explicit RYA_APP_DB_PASSWORD, else derived from the
    RYA_SECRET_KEY so role-create and connect agree); a clearly-labelled weak
    default only for local dev when neither is set."""
    pw = os.environ.get("RYA_APP_DB_PASSWORD")
    if pw:
        return pw
    key = os.environ.get("RYA_SECRET_KEY")
    if key:
        return "rya_" + hashlib.sha256(key.encode()).hexdigest()[:40]
    return "rya_app_pw"  # local-dev only — set RYA_APP_DB_PASSWORD in production


def app_dsn(admin_dsn: str, password: Optional[str] = None) -> str:
    """Derive the non-superuser data-plane DSN (rya_app role) from the admin DSN."""
    params = conninfo_to_dict(admin_dsn)
    params["user"] = "rya_app"
    params["password"] = password or app_db_password()
    return make_conninfo(**params)


def worker_dsn(admin_dsn: str, password: Optional[str] = None) -> str:
    """The EXECUTION-plane DSN (rya_worker role). Same RLS scoping as rya_app but
    SELECT-only on the governance tables (see ``_GOVERNANCE_TABLES``), so the
    process that loads client bundles cannot write policy — PLATFORM_DESIGN §8."""
    params = conninfo_to_dict(admin_dsn)
    params["user"] = "rya_worker"
    params["password"] = password or app_db_password()
    return make_conninfo(**params)


def is_worker_dsn(dsn: str) -> bool:
    """Whether ``dsn`` already connects as ``rya_worker``."""
    try:
        return conninfo_to_dict(dsn).get("user") == "rya_worker"
    except Exception:
        return False


def ensure_worker_dsn(dsn: str, password: Optional[str] = None) -> str:
    """:func:`worker_dsn`, but a no-op when ``dsn`` already names ``rya_worker``.

    A deployment that hands the worker container its own least-privilege DSN —
    which is the goal, so the process never *holds* superuser credentials rather
    than merely declining to use them — must not have that DSN's password
    rewritten from ``RYA_APP_DB_PASSWORD``.
    """
    return dsn if is_worker_dsn(dsn) else worker_dsn(dsn, password)


class Tenancy:
    """Admin-side operations. Uses a privileged connection (the RYA_DATABASE_URL)."""

    def __init__(self, admin_dsn: str) -> None:
        self.admin_dsn = admin_dsn
        self._conn = psycopg.connect(admin_dsn, autocommit=True)

    def close(self) -> None:  # pragma: no cover
        self._conn.close()

    def setup(self, app_password: Optional[str] = None) -> str:
        """Idempotently install data tables, tenancy tables, the rya_app role, and
        RLS policies. Returns the data-plane (rya_app) DSN."""
        pw = app_password or app_db_password()
        with self._conn.cursor() as cur:
            cur.execute(_DATA_SCHEMA)
            cur.execute(_TENANCY_SCHEMA)
            # Non-superuser data-plane role (superusers bypass RLS, so we must not
            # use one for tenant data access). The PASSWORD value is a literal, so
            # it can be parameterized; the role name cannot, hence it's fixed.
            # CREATE/ALTER ROLE are utility statements that don't accept bind
            # params, so the password must be a safely-quoted literal.
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname='rya_app'")
            if cur.fetchone() is None:
                cur.execute(sql.SQL("CREATE ROLE rya_app LOGIN PASSWORD {} NOSUPERUSER").format(sql.Literal(pw)))
            else:
                cur.execute(sql.SQL("ALTER ROLE rya_app PASSWORD {}").format(sql.Literal(pw)))
            # The execution-plane role. Same login shape as rya_app; the grants
            # below are what make it strictly weaker (§8).
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname='rya_worker'")
            if cur.fetchone() is None:
                cur.execute(sql.SQL("CREATE ROLE rya_worker LOGIN PASSWORD {} NOSUPERUSER").format(sql.Literal(pw)))
            else:
                cur.execute(sql.SQL("ALTER ROLE rya_worker PASSWORD {}").format(sql.Literal(pw)))
            cur.execute("GRANT USAGE ON SCHEMA public TO rya_app")
            cur.execute("GRANT USAGE ON SCHEMA public TO rya_worker")
            # rya_policy_log has a BIGSERIAL id; inserting into it needs the
            # sequence, which is not covered by the table grant.
            cur.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO rya_app")
            for tbl in _DATA_TABLES:
                cur.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tbl} TO rya_app")
                if tbl in _GOVERNANCE_TABLES:
                    # Read the verdict, never write it.
                    cur.execute(f"GRANT SELECT ON {tbl} TO rya_worker")
                else:
                    cur.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tbl} TO rya_worker")
                # FORCE so the table owner is subject to RLS too; superusers still
                # bypass (which is why the data plane uses rya_app).
                cur.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
                cur.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY")
                cur.execute(f"DROP POLICY IF EXISTS ws_isolation ON {tbl}")
                cur.execute(
                    f"""CREATE POLICY ws_isolation ON {tbl}
                        USING (workspace_id = current_setting('app.workspace_id', true))
                        WITH CHECK (workspace_id = current_setting('app.workspace_id', true))"""
                )
            # Per-user RLS on runs ("RLS as the user"): a row is visible only if it
            # belongs to the workspace AND (it's a shared/agent row OR it is owned
            # by the requesting user). The data plane sets app.user_id from the
            # *verified* JWT, so a user can never read another user's runs — enforced
            # by Postgres, not app code. Backward compatible: owner IS NULL => shared.
            cur.execute("ALTER TABLE rya_runs ADD COLUMN IF NOT EXISTS owner TEXT")
            cur.execute("DROP POLICY IF EXISTS ws_isolation ON rya_runs")
            cur.execute(
                """CREATE POLICY ws_isolation ON rya_runs
                   USING (workspace_id = current_setting('app.workspace_id', true)
                          AND (owner IS NULL OR owner = current_setting('app.user_id', true)))
                   WITH CHECK (workspace_id = current_setting('app.workspace_id', true))"""
            )
            # Per-user RLS on conversations too: a session is visible only to its
            # owner (or shared agent sessions where owner IS NULL). Messages inherit
            # isolation transitively — they are only reachable via a visible session.
            cur.execute("DROP POLICY IF EXISTS ws_isolation ON rya_sessions")
            cur.execute(
                """CREATE POLICY ws_isolation ON rya_sessions
                   USING (workspace_id = current_setting('app.workspace_id', true)
                          AND (owner IS NULL OR owner = current_setting('app.user_id', true)))
                   WITH CHECK (workspace_id = current_setting('app.workspace_id', true))"""
            )
            # Connections carry vaulted credentials — same per-user rule: a user
            # sees only their own connections plus workspace-shared (owner NULL).
            cur.execute("DROP POLICY IF EXISTS ws_isolation ON rya_connections")
            cur.execute(
                """CREATE POLICY ws_isolation ON rya_connections
                   USING (workspace_id = current_setting('app.workspace_id', true)
                          AND (owner IS NULL OR owner = current_setting('app.user_id', true)))
                   WITH CHECK (workspace_id = current_setting('app.workspace_id', true))"""
            )
            self._backfill_organizations(cur)
        return app_dsn(self.admin_dsn, pw)

    # ---- organizations (D29) --------------------------------------------
    def _backfill_organizations(self, cur) -> int:
        """Give every org-less workspace an organization of its own.

        Idempotent by construction: it selects only ``org_id IS NULL``, so the
        second run finds nothing. One-org-per-workspace is the only backfill that
        cannot be wrong — merging workspaces into a shared org is a billing
        assertion nobody has made yet, and un-merging later means splitting an
        invoice. Operators group workspaces afterwards with
        :meth:`assign_workspace_to_org`.
        """
        cur.execute("SELECT id, name FROM rya_workspaces WHERE org_id IS NULL")
        orphans = cur.fetchall()
        for ws_id, ws_name in orphans:
            org_id = _new_id("org")
            cur.execute(
                "INSERT INTO rya_organizations (id, name, created_at) VALUES (%s,%s,%s)",
                (org_id, ws_name, now_iso()))
            cur.execute("UPDATE rya_workspaces SET org_id=%s WHERE id=%s", (org_id, ws_id))
        return len(orphans)

    def create_organization(self, name: str) -> dict:
        org = {"id": _new_id("org"), "name": name, "createdAt": now_iso()}
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO rya_organizations (id, name, created_at) VALUES (%s,%s,%s)",
                (org["id"], org["name"], org["createdAt"]))
        return org

    def list_organizations(self) -> List[dict]:
        with self._conn.cursor() as cur:
            cur.execute("""SELECT o.id, o.name, o.created_at, COUNT(w.id), o.budget
                           FROM rya_organizations o
                           LEFT JOIN rya_workspaces w ON w.org_id = o.id
                           GROUP BY o.id, o.name, o.created_at, o.budget
                           ORDER BY o.created_at""")
            return [{"id": r[0], "name": r[1], "createdAt": r[2], "workspaces": r[3],
                     "budget": r[4] or None}
                    for r in cur.fetchall()]

    def get_organization(self, org_id: str) -> Optional[dict]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT id, name, created_at, budget FROM rya_organizations "
                        "WHERE id=%s", (org_id,))
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute("SELECT id FROM rya_workspaces WHERE org_id=%s ORDER BY id",
                        (org_id,))
            members = [r[0] for r in cur.fetchall()]
        return {"id": row[0], "name": row[1], "createdAt": row[2],
                "budget": row[3] or None, "workspaces": members}

    def set_org_budget(self, org_id: str, budget: Optional[dict]) -> dict:
        """Write an org's budget. ``None`` clears it.

        Validated here rather than trusted, because this is the *admin* plane and the
        row it writes is read by a reconciler running unattended — a malformed budget
        would either be silently ignored (a limit that caps nothing) or would break
        every tick for every org. Refusing at the write is the only place the person
        who made the mistake is still present.
        """
        from psycopg.types.json import Json

        from .errors import RyaError
        from .orgs import coerce_budget

        coerce_budget(budget)
        with self._conn.cursor() as cur:
            cur.execute("UPDATE rya_organizations SET budget=%s WHERE id=%s",
                        (Json(budget) if budget else None, org_id))
            if cur.rowcount == 0:
                raise RyaError("E_NOT_FOUND", f"No organization '{org_id}'.")
        return {"orgId": org_id, "budget": budget}

    def assign_workspace_to_org(self, workspace_id: str, org_id: str) -> dict:
        """Move a workspace's BILLING to another org. Its isolation is unaffected —
        `workspace_id` does not change and no RLS policy references `org_id`."""
        from .errors import RyaError
        with self._conn.cursor() as cur:
            cur.execute("SELECT 1 FROM rya_organizations WHERE id=%s", (org_id,))
            if cur.fetchone() is None:
                raise RyaError("E_NOT_FOUND", f"No organization '{org_id}'.")
            cur.execute("UPDATE rya_workspaces SET org_id=%s WHERE id=%s",
                        (org_id, workspace_id))
            if cur.rowcount == 0:
                raise RyaError("E_NOT_FOUND", f"No workspace '{workspace_id}'.")
        return {"workspaceId": workspace_id, "orgId": org_id}

    def create_workspace(self, name: str, owner_user_id: Optional[str] = None,
                         org_id: Optional[str] = None) -> dict:
        """Create a workspace. Without an ``org_id`` it gets an organization of
        its own, so the invariant "every workspace has exactly one org" holds from
        creation rather than only after the next ``setup()``."""
        ws = {"id": _new_id("ws"), "name": name, "owner": owner_user_id, "createdAt": now_iso()}
        with self._conn.cursor() as cur:
            if org_id is None:
                org_id = _new_id("org")
                cur.execute(
                    "INSERT INTO rya_organizations (id, name, created_at) VALUES (%s,%s,%s)",
                    (org_id, name, ws["createdAt"]))
            cur.execute(
                "INSERT INTO rya_workspaces (id, name, owner_user_id, created_at, org_id) "
                "VALUES (%s, %s, %s, %s, %s)",
                (ws["id"], ws["name"], owner_user_id, ws["createdAt"], org_id))
        ws["orgId"] = org_id
        return ws

    def list_workspaces(self) -> List[dict]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT id, name, created_at, org_id FROM rya_workspaces "
                        "ORDER BY created_at")
            return [{"id": r[0], "name": r[1], "createdAt": r[2], "orgId": r[3]}
                    for r in cur.fetchall()]

    # ---- self-serve accounts (onboarding) ------------------------------
    def create_user(self, email: str, password: str) -> dict:
        """Create a user account. Raises E_EMAIL_TAKEN if the email exists."""
        from .accounts import hash_password
        from .errors import RyaError
        email = (email or "").strip().lower()
        if not email or "@" not in email or len(password or "") < 8:
            raise RyaError("E_VALIDATION", "A valid email and an 8+ char password are required.")
        uid = _new_id("usr")
        with self._conn.cursor() as cur:
            cur.execute("SELECT 1 FROM rya_users WHERE email=%s", (email,))
            if cur.fetchone():
                raise RyaError("E_EMAIL_TAKEN", "An account with that email already exists.",
                               hint="Log in instead.")
            cur.execute("INSERT INTO rya_users (id, email, password_hash, created_at) VALUES (%s,%s,%s,%s)",
                        (uid, email, hash_password(password), now_iso()))
        return {"id": uid, "email": email}

    def authenticate(self, email: str, password: str) -> Optional[dict]:
        """Verify credentials → {id, email}, or None."""
        from .accounts import verify_password
        email = (email or "").strip().lower()
        with self._conn.cursor() as cur:
            cur.execute("SELECT id, password_hash FROM rya_users WHERE email=%s", (email,))
            row = cur.fetchone()
        if not row or not verify_password(password, row[1]):
            return None
        return {"id": row[0], "email": email}

    def list_user_workspaces(self, user_id: str) -> List[dict]:
        """Workspaces the user OWNS plus those they are a MEMBER of (claimed invites)."""
        with self._conn.cursor() as cur:
            cur.execute(
                """SELECT id, name, created_at, 'owner' AS role FROM rya_workspaces
                   WHERE owner_user_id=%s
                   UNION
                   SELECT w.id, w.name, w.created_at, m.role FROM rya_workspaces w
                   JOIN rya_workspace_members m ON m.workspace_id = w.id
                   WHERE m.user_id=%s
                   ORDER BY created_at""",
                (user_id, user_id))
            return [{"id": r[0], "name": r[1], "createdAt": r[2], "role": r[3]}
                    for r in cur.fetchall()]

    # ---- team membership (invites) --------------------------------------
    def invite_member(self, workspace_id: str, email: str, invited_by: str) -> dict:
        """Invite an email to a workspace. Idempotent per (workspace, email).
        If the account already exists, membership is effective immediately;
        otherwise the invite is claimed automatically at signup/login."""
        from .errors import RyaError
        email = (email or "").strip().lower()
        if not email or "@" not in email:
            raise RyaError("E_VALIDATION", "A valid email is required.")
        with self._conn.cursor() as cur:
            cur.execute("SELECT id FROM rya_users WHERE email=%s", (email,))
            row = cur.fetchone()
            uid = row[0] if row else None
            cur.execute(
                """INSERT INTO rya_workspace_members
                       (id, workspace_id, email, user_id, role, invited_by, created_at)
                   VALUES (%s, %s, %s, %s, 'member', %s, %s)
                   ON CONFLICT (workspace_id, email) DO UPDATE SET user_id=EXCLUDED.user_id
                   RETURNING id""",
                (_new_id("mem"), workspace_id, email, uid, invited_by, now_iso()))
            mid = cur.fetchone()[0]
        return {"id": mid, "workspaceId": workspace_id, "email": email,
                "claimed": uid is not None}

    def list_members(self, workspace_id: str) -> List[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                """SELECT email, user_id, role, created_at FROM rya_workspace_members
                   WHERE workspace_id=%s ORDER BY created_at""", (workspace_id,))
            return [{"email": r[0], "claimed": r[1] is not None, "role": r[2],
                     "invitedAt": r[3]} for r in cur.fetchall()]

    def claim_invites(self, email: str, user_id: str) -> int:
        """Bind pending invites for this email to the (new or logging-in) user."""
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE rya_workspace_members SET user_id=%s WHERE email=%s AND user_id IS NULL",
                (user_id, (email or "").strip().lower()))
            return cur.rowcount

    def workspace_access(self, workspace_id: str, user_id: str) -> Optional[str]:
        """'owner', 'member', or None for this user on this workspace."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT owner_user_id FROM rya_workspaces WHERE id=%s", (workspace_id,))
            row = cur.fetchone()
            if row is None:
                return None
            if row[0] == user_id:
                return "owner"
            cur.execute(
                "SELECT role FROM rya_workspace_members WHERE workspace_id=%s AND user_id=%s",
                (workspace_id, user_id))
            m = cur.fetchone()
            return m[0] if m else None

    def signup(self, email: str, password: str, workspace_name: str = "My workspace") -> dict:
        """One-step onboarding: create the user + their first workspace + an API key.
        Pending invites for this email are claimed, so a teammate who was invited
        before signing up lands in the shared workspace immediately."""
        user = self.create_user(email, password)
        self.claim_invites(user["email"], user["id"])
        ws = self.create_workspace(workspace_name, owner_user_id=user["id"])
        key = self.create_api_key(ws["id"], label="default")
        return {"user": user, "workspace": ws, "apiKey": key["key"]}

    def create_api_key(self, workspace_id: str, label: str = "",
                       created_by: Optional[str] = None) -> dict:
        import secrets

        plaintext = "rya_sk_" + secrets.token_urlsafe(24)
        rec = {"id": _new_id("key"), "workspaceId": workspace_id, "label": label,
               "createdAt": now_iso()}
        with self._conn.cursor() as cur:
            cur.execute(
                """INSERT INTO rya_api_keys (id, workspace_id, key_hash, label, created_at, created_by)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (rec["id"], workspace_id, hash_key(plaintext), label, rec["createdAt"], created_by),
            )
        # Plaintext returned ONCE; only the hash is persisted.
        return {**rec, "key": plaintext}

    def list_keys(self, workspace_id: str) -> List[dict]:
        """Key METADATA only - hashes never leave the store."""
        with self._conn.cursor() as cur:
            cur.execute(
                """SELECT id, label, created_at, created_by FROM rya_api_keys
                   WHERE workspace_id=%s ORDER BY created_at""", (workspace_id,))
            return [{"id": r[0], "label": r[1], "createdAt": r[2], "createdBy": r[3]}
                    for r in cur.fetchall()]

    def revoke_key(self, workspace_id: str, key_id: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM rya_api_keys WHERE id=%s AND workspace_id=%s",
                        (key_id, workspace_id))
            return cur.rowcount > 0

    def remove_member(self, workspace_id: str, email: str) -> dict:
        """Remove a member AND revoke every key they minted for this workspace -
        removal must actually remove access, not just the list entry."""
        email = (email or "").strip().lower()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM rya_workspace_members WHERE workspace_id=%s AND email=%s",
                (workspace_id, email))
            row = cur.fetchone()
            if row is None:
                return {"removed": False, "keysRevoked": 0}
            uid = row[0]
            revoked = 0
            if uid:
                cur.execute("DELETE FROM rya_api_keys WHERE workspace_id=%s AND created_by=%s",
                            (workspace_id, uid))
                revoked = cur.rowcount
            cur.execute("DELETE FROM rya_workspace_members WHERE workspace_id=%s AND email=%s",
                        (workspace_id, email))
            return {"removed": True, "keysRevoked": revoked}

    def change_password(self, user_id: str, current: str, new: str) -> None:
        """Verify the current password, then set a new one."""
        from .accounts import hash_password, verify_password
        from .errors import RyaError
        if len(new or "") < 8:
            raise RyaError("E_VALIDATION", "The new password must be 8+ characters.")
        with self._conn.cursor() as cur:
            cur.execute("SELECT password_hash FROM rya_users WHERE id=%s", (user_id,))
            row = cur.fetchone()
            if row is None or not verify_password(current, row[0]):
                raise RyaError("E_UNAUTHORIZED", "Current password is incorrect.")
            cur.execute("UPDATE rya_users SET password_hash=%s WHERE id=%s",
                        (hash_password(new), user_id))

    def resolve_key(self, plaintext: str) -> Optional[str]:
        """Return the workspace_id for an API key, or None."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT workspace_id FROM rya_api_keys WHERE key_hash=%s", (hash_key(plaintext),))
            row = cur.fetchone()
            return row[0] if row else None
