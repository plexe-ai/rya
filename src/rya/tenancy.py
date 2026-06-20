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

_DATA_TABLES = ["rya_runs", "rya_approvals", "rya_jobs", "rya_memory", "rya_sessions",
                "rya_messages", "rya_connections"]

_TENANCY_SCHEMA = """
CREATE TABLE IF NOT EXISTS rya_workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS rya_api_keys (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES rya_workspaces(id) ON DELETE CASCADE,
    key_hash TEXT NOT NULL UNIQUE,
    label TEXT,
    created_at TEXT
);
"""


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def app_dsn(admin_dsn: str, password: Optional[str] = None) -> str:
    """Derive the non-superuser data-plane DSN (rya_app role) from the admin DSN."""
    params = conninfo_to_dict(admin_dsn)
    params["user"] = "rya_app"
    params["password"] = password or os.environ.get("RYA_APP_DB_PASSWORD", "rya_app_pw")
    return make_conninfo(**params)


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
        pw = app_password or os.environ.get("RYA_APP_DB_PASSWORD", "rya_app_pw")
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
            cur.execute("GRANT USAGE ON SCHEMA public TO rya_app")
            for tbl in _DATA_TABLES:
                cur.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tbl} TO rya_app")
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
        return app_dsn(self.admin_dsn, pw)

    def create_workspace(self, name: str) -> dict:
        ws = {"id": _new_id("ws"), "name": name, "createdAt": now_iso()}
        with self._conn.cursor() as cur:
            cur.execute("INSERT INTO rya_workspaces (id, name, created_at) VALUES (%s, %s, %s)",
                        (ws["id"], ws["name"], ws["createdAt"]))
        return ws

    def list_workspaces(self) -> List[dict]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT id, name, created_at FROM rya_workspaces ORDER BY created_at")
            return [{"id": r[0], "name": r[1], "createdAt": r[2]} for r in cur.fetchall()]

    def create_api_key(self, workspace_id: str, label: str = "") -> dict:
        import secrets

        plaintext = "rya_sk_" + secrets.token_urlsafe(24)
        rec = {"id": _new_id("key"), "workspaceId": workspace_id, "label": label,
               "createdAt": now_iso()}
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO rya_api_keys (id, workspace_id, key_hash, label, created_at) VALUES (%s,%s,%s,%s,%s)",
                (rec["id"], workspace_id, hash_key(plaintext), label, rec["createdAt"]),
            )
        # Plaintext returned ONCE; only the hash is persisted.
        return {**rec, "key": plaintext}

    def resolve_key(self, plaintext: str) -> Optional[str]:
        """Return the workspace_id for an API key, or None."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT workspace_id FROM rya_api_keys WHERE key_hash=%s", (hash_key(plaintext),))
            row = cur.fetchone()
            return row[0] if row else None
