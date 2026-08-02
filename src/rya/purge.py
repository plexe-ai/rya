"""Tenant deletion, two-phase (D31): ``disable`` now, ``purge`` after retention.

RLS makes reads safe and says nothing about erasure. This module is the erasure
half, and its shape follows from one observation in D31: **destroying a key is O(1)
and chasing rows is not.** So the order of operations is

    1. crypto-shred    destroy the workspace's seal key(s)
    2. objects         delete bundle archives under the workspace prefix
    3. rows            delete across the 19 `_DATA_TABLES`
    4. stub            leave an anonymised audit record

and step 1 is first on purpose: after it, every sealed secret in the workspace is
unreadable whether or not steps 2–4 complete. A purge interrupted halfway has
already delivered the property that matters most, and the remainder is bulk.

**Two prerequisites, both of which D31 said were prerequisites.** #7 (workspace-
prefixed bundle keys) is what makes step 2 *enumerable* — a flat content-addressed
namespace has no per-tenant listing, so before D20 there was no way to know which
archives were this tenant's. #13 (per-tenant seal keys) is what makes step 1 exist at
all: one deployment key means shredding it destroys every tenant's data. Both landed
before this module, which is why it can be built rather than described.

**Where the honest gap is.** ``RYA_KEY_PROVIDER=deployment`` — still the default, and
correctly so — has nothing to shred. A purge in that configuration deletes rows and
objects and makes no cryptographic claim, and :func:`purge` says so in its report
rather than implying otherwise. That is why :class:`PurgeReport` carries
``crypto_shredded`` as a separate field from ``ok``: an operator answering a deletion
request needs to know which of the two they can attest to.

**The unresolved part, still unresolved.** The governance tables are append-only and
`SELECT`-only by design ("Read the verdict, never write it", ``tenancy.py``), which is
in direct tension with erasure. The position D31 took is an **anonymised audit stub**
— retain the decision record, drop the payload — and this module implements exactly
that. A jurisdiction demanding full erasure would override it, and that needs legal
review rather than an engineering answer, so the stub is written in a way that makes
what was kept obvious rather than buried.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from .errors import RyaError
from .store import now_iso

log = logging.getLogger("rya.purge")

# The default retention window between `disable` and `purge`. Not zero, and the
# reason is operational rather than legal: `disable` is what a billing failure or an
# abuse report triggers, and both are reversible mistakes often enough that an
# immediate purge would turn a support ticket into an unrecoverable one.
DEFAULT_RETENTION_DAYS = 30

STATE_ACTIVE = "active"
STATE_DISABLED = "disabled"
STATE_PURGED = "purged"

# Where the lifecycle state lives. A policy row rather than a column on
# `rya_workspaces`, because the state is read by the *data* plane (a disabled
# workspace refuses claims) and `rya_workspaces` is admin-plane state the execution
# role deliberately cannot see.
POLICY_KEY = "lifecycle"

E_NOT_ALLOWED = "E_PURGE_NOT_ALLOWED"
E_DISABLED = "E_WORKSPACE_DISABLED"


# ---- state ------------------------------------------------------------------

@dataclass(frozen=True)
class Lifecycle:
    """A workspace's deletion state, and when it entered it."""

    state: str = STATE_ACTIVE
    at: str = ""
    reason: str = ""
    actor: str = ""
    retention_days: int = DEFAULT_RETENTION_DAYS

    @property
    def active(self) -> bool:
        return self.state == STATE_ACTIVE

    def purgeable_at(self) -> Optional[datetime]:
        if self.state != STATE_DISABLED or not self.at:
            return None
        try:
            when = datetime.fromisoformat(self.at.replace("Z", "+00:00"))
        except ValueError:  # pragma: no cover - a malformed timestamp
            return None
        return when + timedelta(days=max(0, self.retention_days))

    def describe(self) -> dict:
        due = self.purgeable_at()
        return {"state": self.state, "at": self.at or None, "reason": self.reason or None,
                "actor": self.actor or None, "retentionDays": self.retention_days,
                "purgeableAt": due.isoformat().replace("+00:00", "Z") if due else None}


def lifecycle(store) -> Lifecycle:
    """Read the state. Absent means active, which is what every workspace starts as."""
    getter = getattr(store, "policy_get", None)
    if getter is None:
        return Lifecycle()
    try:
        raw = getter(POLICY_KEY) or {}
    except Exception:  # noqa: BLE001
        # Fails OPEN, unlike the guard. A policy read failure must not make a live
        # workspace look disabled and stop serving its traffic — the guard fails closed
        # because a missing allowlist is a security question, and this is availability.
        return Lifecycle()
    data: dict = raw.get("policy", raw) if isinstance(raw, dict) else {}
    if not isinstance(data, dict):
        return Lifecycle()
    return Lifecycle(state=str(data.get("state") or STATE_ACTIVE),
                     at=str(data.get("at") or ""),
                     reason=str(data.get("reason") or ""),
                     actor=str(data.get("actor") or ""),
                     # `or DEFAULT` would be wrong here and was: a retentionDays of 0
                     # is falsy, so an operator who deliberately configured no window
                     # silently got thirty days and no way to reach zero except
                     # --force, which also skips the state check they did want.
                     retention_days=int(DEFAULT_RETENTION_DAYS
                                        if data.get("retentionDays") is None
                                        else data["retentionDays"]))


def require_active(store) -> None:
    """Refuse work for a disabled workspace. The enforcement half of ``disable``.

    Called from the admission path, beside the quota check, because "stop scheduling
    and refuse claims" is what D31 says ``disable`` means — and a disable that only
    revoked API keys would leave every already-queued item to run.
    """
    state = lifecycle(store)
    if state.active:
        return
    raise RyaError(
        E_DISABLED,
        f"This workspace is {state.state}"
        + (f" ({state.reason})" if state.reason else "") + ".",
        hint=("Disabling is reversible: `rya workspaces enable <workspace>`. Queued "
              "work is refused rather than dropped, so re-enabling resumes it."
              if state.state == STATE_DISABLED else
              "A purged workspace cannot be restored — its seal key was destroyed."),
    )


# ---- phase one: disable -----------------------------------------------------

def disable(store, *, reason: str = "", actor: str = "", tenancy=None,
            workspace: str = "", retention_days: int = DEFAULT_RETENTION_DAYS) -> dict:
    """Immediate, synchronous, reversible. The only step a billing failure triggers.

    Three effects, and the order is chosen so that a failure part-way leaves the
    workspace *more* disabled rather than less: write the state first (which stops
    admission and claiming everywhere), then revoke the API keys.
    """
    record = {"state": STATE_DISABLED, "at": now_iso(), "reason": reason,
              "actor": actor, "retentionDays": int(retention_days)}
    store.policy_set(POLICY_KEY, record, actor=actor or None)
    revoked = 0
    if tenancy is not None and workspace:
        for key in tenancy.list_keys(workspace):
            if tenancy.revoke_key(workspace, key["id"]):
                revoked += 1
    log.warning("workspace %s disabled (%s), %d api keys revoked",
                workspace or "(current)", reason or "no reason given", revoked)
    return {**Lifecycle(state=STATE_DISABLED, at=str(record["at"]), reason=reason,
                        actor=actor,
                        retention_days=int(retention_days)).describe(),
            "keysRevoked": revoked}


def enable(store, *, actor: str = "") -> dict:
    """Undo a disable. Refuses on a purged workspace, which cannot be undone."""
    state = lifecycle(store)
    if state.state == STATE_PURGED:
        raise RyaError(
            E_NOT_ALLOWED,
            "This workspace was purged and cannot be re-enabled.",
            hint="Its seal key was destroyed, so its sealed data is unreadable by "
                 "construction — re-enabling would produce a workspace that cannot "
                 "open its own secrets.")
    store.policy_set(POLICY_KEY, {"state": STATE_ACTIVE, "at": now_iso(),
                                  "actor": actor}, actor=actor or None)
    return lifecycle(store).describe()


# ---- phase two: purge -------------------------------------------------------

@dataclass
class PurgeReport:
    """What a purge actually destroyed, and what it can therefore attest to.

    ``crypto_shredded`` is separate from ``ok`` because they answer different
    questions. ``ok`` is "did every step complete"; ``crypto_shredded`` is "can this
    deployment claim the sealed data is unreadable" — and with the default deployment
    key provider the answer to the second is *no* however well the first went.
    """

    workspace: str
    dry_run: bool = False
    crypto_shredded: bool = False
    key_generations: int = 0
    key_note: str = ""
    objects_deleted: int = 0
    object_note: str = ""
    rows_deleted: Dict[str, int] = field(default_factory=dict)
    audit_stub: Optional[dict] = None
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def total_rows(self) -> int:
        return sum(self.rows_deleted.values())

    def describe(self) -> dict:
        return {"workspace": self.workspace, "dryRun": self.dry_run, "ok": self.ok,
                "cryptoShredded": self.crypto_shredded,
                "keyGenerations": self.key_generations, "keyNote": self.key_note or None,
                "objectsDeleted": self.objects_deleted,
                "objectNote": self.object_note or None,
                "rowsDeleted": dict(self.rows_deleted), "totalRows": self.total_rows,
                "auditStub": self.audit_stub, "errors": list(self.errors),
                "attestation": self.attestation()}

    def attestation(self) -> str:
        """One sentence an operator can put in front of a deletion request.

        Written by the code that did the work rather than by a human reading a log,
        because the difference between "unreadable by construction" and "rows deleted"
        is exactly the difference someone answering a legal request must not get wrong.
        """
        if self.dry_run:
            return (f"Nothing was destroyed. A purge would delete {self.total_rows} "
                    f"rows and {self.objects_deleted} objects, and "
                    + ("would crypto-shred the workspace key."
                       if self.crypto_shredded else
                       "could NOT crypto-shred — " + (self.key_note or "no per-tenant key.")))
        if not self.ok:
            return (f"INCOMPLETE: {len(self.errors)} step(s) failed. "
                    + ("The seal key was destroyed first, so sealed data is already "
                       "unreadable." if self.crypto_shredded else
                       "No cryptographic claim can be made."))
        if self.crypto_shredded and self.key_generations:
            return (f"Workspace {self.workspace} purged: its seal key was destroyed, "
                    f"so every value sealed under it is unreadable without enumerating "
                    f"them; {self.objects_deleted} bundle objects and {self.total_rows} "
                    f"rows were deleted; an anonymised audit stub remains.")
        if self.crypto_shredded:
            return (f"Workspace {self.workspace} purged: {self.objects_deleted} bundle "
                    f"objects and {self.total_rows} rows were deleted; an anonymised "
                    f"audit stub remains. No key was destroyed because none existed — "
                    f"this workspace had nothing sealed under a per-tenant key.")
        return (f"Workspace {self.workspace} purged by deletion only: {self.objects_deleted} "
                f"bundle objects and {self.total_rows} rows were deleted. NO cryptographic "
                f"claim — {self.key_note or 'this deployment uses one key for all tenants'}. "
                f"Backups taken before now still contain readable data.")


def purge(store, *, workspace: str, keyring=None, bundle_store=None,
          admin_dsn: str = "", force: bool = False,
          dry_run: bool = False, actor: str = "") -> PurgeReport:
    """Destroy a disabled workspace's data. Irreversible.

    ``force`` skips the retention window, which an operator sometimes legitimately
    needs (a test tenant, a court order) and which must be an explicit act rather than
    a default — the window exists because `disable` is reversible and `purge` is not.

    ``dry_run`` reports what would happen without doing it. Worth having for a step
    with no undo, and worth having *first-class* rather than as an operator reading the
    code: the counts it produces are the ones the real run will report.
    """
    report = PurgeReport(workspace=workspace, dry_run=dry_run)
    state = lifecycle(store)
    if not force:
        if state.state != STATE_DISABLED:
            raise RyaError(
                E_NOT_ALLOWED,
                f"Workspace '{workspace}' is {state.state}, and only a disabled "
                "workspace can be purged.",
                hint="Two phases on purpose (D31): `rya tenancy disable` first, which "
                     "is reversible, then purge after the retention window. Pass "
                     "--force to skip both checks.")
        due = state.purgeable_at()
        if due is not None and datetime.now(timezone.utc) < due:
            raise RyaError(
                E_NOT_ALLOWED,
                f"Workspace '{workspace}' is disabled but its retention window runs "
                f"until {due.isoformat()}.",
                hint="The window is what makes a wrong disable recoverable. --force "
                     "skips it deliberately.")

    _shred_key(report, keyring=keyring, workspace=workspace, dry_run=dry_run)
    _delete_objects(report, bundle_store=bundle_store, workspace=workspace,
                    dry_run=dry_run)
    if admin_dsn:
        _delete_rows(report, admin_dsn=admin_dsn, workspace=workspace, dry_run=dry_run)
        _delete_tenancy(report, admin_dsn=admin_dsn, workspace=workspace,
                        dry_run=dry_run)
    else:
        _delete_local(report, store=store, dry_run=dry_run)
    report.audit_stub = _audit_stub(report, state=state, actor=actor)
    if not dry_run:
        # Written LAST and through the same policy surface, so the row that says
        # "purged" survives the row deletion above rather than being caught by it.
        try:
            store.policy_set(POLICY_KEY, {"state": STATE_PURGED, "at": now_iso(),
                                          "actor": actor,
                                          "stub": report.audit_stub},
                             actor=actor or None)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"could not record the purge: {exc}")
    log.warning("purge %s: %s", workspace, report.attestation())
    return report


def _shred_key(report: PurgeReport, *, keyring, workspace: str, dry_run: bool) -> None:
    """Step 1, and first for a reason: after it the rest is bulk.

    A provider that cannot shred raises rather than returning zero (see
    ``keys.KeyProvider.destroy``), and that raise is *recorded* here rather than
    propagated. The purge should still delete the rows — deletion is worth doing even
    without a cryptographic claim — and the report must say which of the two happened.
    """
    if keyring is None:
        report.key_note = ("no key ring was supplied, so no cryptographic claim can be "
                           "made about sealed values")
        return
    if not getattr(keyring, "shreddable", False):
        provider = getattr(getattr(keyring, "provider", None), "name", "unknown")
        report.key_note = (f"the '{provider}' key provider cannot crypto-shred: its "
                           "keys are either shared across tenants or derivable from a "
                           "root, so there is nothing whose destruction makes this "
                           "tenant's data unreadable")
        return
    if dry_run:
        report.crypto_shredded = True
        report.key_note = "would destroy every generation of this workspace's key"
        return
    try:
        report.key_generations = keyring.destroy(workspace)
        report.crypto_shredded = True
        if report.key_generations:
            report.key_note = (f"destroyed {report.key_generations} key generation(s); "
                               "values sealed under them are unreadable")
        else:
            # Zero generations is not a failure and it is not a shred either. Saying
            # "its seal key was destroyed" here would be a small untruth in the one
            # sentence an operator quotes to answer a deletion request, so it gets its
            # own wording: nothing was sealed under a per-tenant key, therefore nothing
            # needed destroying.
            report.key_note = ("no per-tenant key existed for this workspace, so "
                               "nothing was sealed under one and nothing needed "
                               "destroying")
    except Exception as exc:  # noqa: BLE001 - recorded, never fatal
        report.errors.append(f"crypto-shred failed: {exc}")
        report.key_note = str(exc)[:300]


def _delete_objects(report: PurgeReport, *, bundle_store, workspace: str,
                    dry_run: bool) -> None:
    """Step 2. Enumerable only because D20 put the workspace in the key.

    Before D20 the archive namespace was flat and content-addressed, so there was no
    listing that meant "this tenant's bundles" — which is why D31 named #7 a
    prerequisite rather than a nice-to-have.
    """
    if bundle_store is None:
        report.object_note = "no bundle store was supplied; archives were not touched"
        return
    from . import bundles as B

    if not B._normalize_workspace(getattr(bundle_store, "workspace", "")):
        # An un-namespaced archive store is the single-tenant layout: the namespace is
        # SHARED, so there is no per-tenant set of objects to delete. Reported as a note
        # rather than an error, because `list_workspace_objects` refusing is the guard
        # against deleting the shared namespace working correctly — and a correct guard
        # firing on the local arm must not make every local purge read as failed.
        report.object_note = ("the archive store is un-namespaced (single-tenant "
                              "layout), so it holds no per-tenant objects to delete")
        return
    try:
        keys = B.list_workspace_objects(bundle_store, workspace)
    except RyaError as exc:
        report.errors.append(f"could not list bundle objects: {exc.message}")
        return
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"could not list bundle objects: {exc}")
        return
    report.objects_deleted = len(keys)
    if dry_run:
        report.object_note = f"would delete {len(keys)} object(s)"
        return
    try:
        B.delete_workspace_objects(bundle_store, workspace)
        report.object_note = f"deleted {len(keys)} object(s)"
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"could not delete bundle objects: {exc}")


def _delete_local(report: PurgeReport, *, store, dry_run: bool) -> None:
    """Step 3 on the file arm, where there is no admin connection to have.

    A ``FileStore`` **is** the workspace — it has no ``workspace_id`` at all, which is
    what keeps `rya dev` on the pre-D20 layout — so purging it means clearing its data
    directories rather than deleting rows matching a tenant. That equivalence is why
    this arm exists rather than the local case being an unsupported hole: without it
    the whole purge path would be unexercisable outside a Postgres deployment, and D31
    asks for it to be *exercised*.

    The policy directory is spared, because the row that will record the purge lives in
    it. Everything else goes, including the journal, the meter ledger and the version
    records — a purge is not a selective cleanup.
    """
    directories = [
        ("rya_runs", getattr(store, "runs_dir", None)),
        ("rya_approvals", getattr(store, "approvals_dir", None)),
        ("rya_jobs", getattr(store, "jobs_dir", None)),
        ("rya_queue", getattr(store, "queue_dir", None)),
        ("rya_stream", getattr(store, "streams_dir", None)),
        ("rya_memory", getattr(store, "memory_dir", None)),
        ("rya_sessions", getattr(store, "sessions_dir", None)),
        ("rya_connections", getattr(store, "connections_dir", None)),
        ("rya_files", getattr(store, "files_dir", None)),
        ("rya_journal", getattr(store, "journal_dir", None)),
        ("rya_meter", getattr(store, "meter_dir", None)),
        ("rya_versions", getattr(store, "versions_dir", None)),
        ("rya_environments", getattr(store, "envs_dir", None)),
        ("rya_workers", getattr(store, "workers_dir", None)),
        ("rya_leases", getattr(store, "leases_dir", None)),
    ]
    if not any(d for _n, d in directories):
        report.errors.append(
            "no admin DSN was supplied and this store has no local data directories, "
            "so no rows were deleted")
        return
    for name, directory in directories:
        if directory is None or not directory.is_dir():
            continue
        entries = [p for p in directory.rglob("*") if p.is_file()]
        report.rows_deleted[name] = len(entries)
        if dry_run:
            continue
        for path in entries:
            try:
                path.unlink()
            except OSError as exc:  # pragma: no cover
                report.errors.append(f"{name}: {exc}")


def _delete_tenancy(report: PurgeReport, *, admin_dsn: str, workspace: str,
                    dry_run: bool) -> None:
    """Step 3b, and it is not a footnote: the identifiers live here.

    ``_DATA_TABLES`` is the *data plane* — nineteen tables of runs, journals, meters
    and policy — and it deliberately excludes the admin-plane tenancy tables, because
    those are reached over the privileged connection and carry no RLS. Which means a
    purge that stopped at ``_DATA_TABLES`` would delete every run and leave
    ``rya_workspace_members`` holding the **email addresses** of everyone who ever had
    access. That is the one category of data a deletion request is usually actually
    about.

    ``rya_users`` is deliberately untouched: a user can belong to several workspaces,
    so deleting the person because one of their workspaces was purged would erase data
    belonging to a different tenant. The membership goes; the account does not.
    """
    if not admin_dsn:
        return
    try:
        import psycopg
    except ImportError:  # pragma: no cover
        return
    # `rya_api_keys` and `rya_workspace_members` are ON DELETE CASCADE from
    # `rya_workspaces`, so the workspace row is deleted LAST and the cascade is what
    # takes them — but they are counted explicitly first, because "the cascade probably
    # handled it" is not an attestation.
    counted = ("rya_api_keys", "rya_workspace_members", "rya_tenant_keys")
    verb = "SELECT count(*) FROM" if dry_run else "DELETE FROM"
    try:
        with psycopg.connect(admin_dsn, autocommit=not dry_run) as conn:
            for table in counted:
                with conn.cursor() as cur:
                    try:
                        cur.execute(f"{verb} {table} WHERE workspace_id = %s",
                                    (workspace,))
                        report.rows_deleted[table] = (
                            int((cur.fetchone() or [0])[0]) if dry_run
                            else (cur.rowcount or 0))
                    except Exception as exc:  # noqa: BLE001
                        report.errors.append(f"{table}: {exc}")
            with conn.cursor() as cur:
                try:
                    cur.execute(f"{verb} rya_workspaces WHERE id = %s", (workspace,))
                    report.rows_deleted["rya_workspaces"] = (
                        int((cur.fetchone() or [0])[0]) if dry_run
                        else (cur.rowcount or 0))
                except Exception as exc:  # noqa: BLE001
                    report.errors.append(f"rya_workspaces: {exc}")
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"could not connect to delete tenancy rows: {exc}")


def _delete_rows(report: PurgeReport, *, admin_dsn: str, workspace: str,
                 dry_run: bool) -> None:
    """Step 3. Needs the ADMIN connection, and that is not an inconvenience.

    RLS would restrict a delete to the current workspace, which is exactly right — but
    the governance tables are `SELECT`-only for the execution role, so the app role
    cannot delete them at all. A purge is an admin-plane act by construction, and
    making it use the admin DSN keeps it out of reach of anything running tenant code.
    """
    if not admin_dsn:
        report.errors.append("no admin DSN was supplied, so no rows were deleted")
        return
    from .tenancy import _DATA_TABLES

    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover
        report.errors.append(f"psycopg is required to delete rows: {exc}")
        return
    verb = "SELECT count(*) FROM" if dry_run else "DELETE FROM"
    try:
        with psycopg.connect(admin_dsn, autocommit=not dry_run) as conn:
            for table in _DATA_TABLES:
                with conn.cursor() as cur:
                    try:
                        cur.execute(f"{verb} {table} WHERE workspace_id = %s",
                                    (workspace,))
                        report.rows_deleted[table] = (
                            int((cur.fetchone() or [0])[0]) if dry_run
                            else (cur.rowcount or 0))
                    except Exception as exc:  # noqa: BLE001 - per table, not fatal
                        # One missing table must not abandon the other eighteen. A
                        # partially-migrated database is a real state, and a purge that
                        # gave up on the first gap would leave the most data behind.
                        report.errors.append(f"{table}: {exc}")
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"could not connect to delete rows: {exc}")


def _audit_stub(report: PurgeReport, *, state: Lifecycle, actor: str) -> dict:
    """Step 4: the decision record, without the payload.

    D31's unresolved tension, implemented as the position it took. What is kept is
    deliberately listed rather than described, so a reviewer can see the whole of it:
    that a workspace existed, that it was disabled and why, that it was purged and by
    whom, and the counts. What is dropped is everything a person could be identified
    from — no emails, no run content, no payloads, no names.
    """
    return {
        "workspace": report.workspace,
        "disabledAt": state.at or None,
        "disabledReason": state.reason or None,
        "purgedAt": now_iso(),
        "purgedBy": actor or None,
        "cryptoShredded": report.crypto_shredded,
        "counts": {"objects": report.objects_deleted, "rows": report.total_rows,
                   "keyGenerations": report.key_generations},
        "retained": "decision record only — no identifiers, payloads or run content",
        "note": ("The governance tables are append-only by design, which is in tension "
                 "with erasure. This stub is D31's position: keep the record, drop the "
                 "payload. A jurisdiction requiring full erasure overrides it."),
    }
