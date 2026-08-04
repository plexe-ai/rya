"""Organizations — the billing entity above the workspace (D29).

**The split rule, in code.** D29 says the *isolation* boundary stays
``workspace_id`` and the *billing* boundary becomes ``org_id``. Phase 1 landed the
schema half (``rya_organizations``, a nullable ``org_id`` on workspaces, a
one-org-per-workspace backfill) and deliberately left it inert: nothing read the
column. This module is what reads it, and it reads it for exactly two things —
**aggregate usage** and **budget** — because those are the two questions whose
answer is "per org" rather than "per tenant".

Nothing here moves an isolation boundary. No RLS policy references ``org_id``, no
store handle is scoped by it, and a workspace's data is as unreachable from a
sibling in the same org as from a stranger. What an org shares is a bill.

**Where the enforcement happens, and why it is not where the numbers are computed.**
Summing an org's usage needs to read several workspaces' meters, which needs a
connection that spans tenants — the admin DSN. Putting that connection on the
admission path would mean the *hot path of every tenant's every run* holding a
credential that can read every other tenant, which is the exact thing D18 spent
Phase 4 removing from a much less privileged process.

So the rollup is computed by a privileged reconciler and its *verdict* is written
down, per workspace, in that workspace's own policy row::

    reconcile()            admin DSN, spans workspaces, computes the org total
        │
        └─► policy["orgBudget"] in each member workspace   (a derived row)
                │
                └─► quotas.check_admission reads it with no special privilege

The cost is staleness bounded by the reconciler's interval, and the reason that is
acceptable is already written down in :mod:`rya.quotas`: token and cost limits
"throttle the *next* piece of work, and the overshoot is bounded by one run rather
than by nothing". An org budget is the same kind of limit, so it inherits the same
argument with a wider bound — one reconcile interval instead of one run. Killing a
run mid-journal to tighten that would trade a durability bug for a billing nicety,
which §11.12 already refused once.

**The derived row is labelled as derived.** It carries ``computedAt``, the org id and
the reconciler's own name, because an operator who finds a workspace refusing work
needs to be able to tell "this tenant is over ITS budget" from "this tenant's *org*
is over its budget, and the tenant may be nearly idle". Those have different
answers and the same error code otherwise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from .errors import RyaError
from .store import now_iso

log = logging.getLogger("rya.orgs")

# The derived per-workspace verdict. A separate policy key from `quotas` on purpose:
# `rya quotas set` is an operator writing a limit, and this is the platform writing a
# computed fact. Sharing one row would mean a reconcile tick could clobber a limit
# somebody typed.
POLICY_KEY = "orgBudget"

# Wire name -> (attribute, coercion). Deliberately the money limits only.
#
# An org is a *billing* entity, so an org-level `maxConcurrentRuns` would be a
# scheduling limit expressed at the boundary that does no scheduling — it would have
# to be enforced by summing live runs across workspaces, on the admission path, over
# the connection this module exists to keep off that path. Tokens and cost are
# already computed from a durable ledger that is summed after the fact, which is why
# they aggregate cleanly and concurrency does not.
_BUDGET_FIELDS: Dict[str, tuple] = {
    "maxTokensPerDay": ("max_tokens_per_day", int),
    "maxCostUsdPerDay": ("max_cost_usd_per_day", float),
    "maxCostUsdPerMonth": ("max_cost_usd_per_month", float),
}


def _day_start(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT00:00:00Z")


def _month_start(now: Optional[datetime] = None) -> str:
    """First instant of the current UTC month.

    A calendar month rather than 30 days, because an invoice is a calendar artefact
    and a customer reading "your monthly budget" means the thing their card is
    charged for.
    """
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-01T00:00:00Z")


@dataclass(frozen=True)
class OrgBudget:
    """An org's limits. Every field ``None`` means unlimited."""

    max_tokens_per_day: Optional[int] = None
    max_cost_usd_per_day: Optional[float] = None
    max_cost_usd_per_month: Optional[float] = None
    source: str = "default"

    @property
    def enforced(self) -> bool:
        return any(v is not None for v in (self.max_tokens_per_day,
                                           self.max_cost_usd_per_day,
                                           self.max_cost_usd_per_month))

    def describe(self) -> dict:
        return {"enforced": self.enforced, "source": self.source,
                "maxTokensPerDay": self.max_tokens_per_day,
                "maxCostUsdPerDay": self.max_cost_usd_per_day,
                "maxCostUsdPerMonth": self.max_cost_usd_per_month}


def coerce_budget(spec: Optional[Mapping[str, Any]], source: str = "policy") -> OrgBudget:
    """Validate an org budget, refusing unknown keys.

    Same stance as ``quotas._coerce`` and ``gates.py``, for the same reason: a
    mistyped ``maxCostUsdPerMonth`` that silently resolved to unlimited would be a
    budget that reports itself as configured while capping nothing — and at the org
    level that is somebody's invoice.
    """
    if not spec:
        return OrgBudget(source="default")
    if not isinstance(spec, Mapping):
        raise RyaError("E_VALIDATION", "An org budget must be an object.",
                       hint='e.g. {"maxCostUsdPerMonth": 500}')
    unknown = [k for k in spec if k not in _BUDGET_FIELDS]
    if unknown:
        raise RyaError(
            "E_VALIDATION",
            f"Unrecognised org budget key(s): {', '.join(sorted(unknown))}.",
            hint="Valid keys: " + ", ".join(sorted(_BUDGET_FIELDS)) + ". An org is the "
            "BILLING boundary (D29), so only the limits that map to money live here — "
            "concurrency and queue depth stay per workspace, where the scheduler is.")
    kwargs: Dict[str, Any] = {}
    for wire, (attr, cast) in _BUDGET_FIELDS.items():
        if spec.get(wire) is None:
            continue
        try:
            value = cast(spec[wire])
        except (TypeError, ValueError):
            raise RyaError("E_VALIDATION",
                           f"Org budget `{wire}` must be a {cast.__name__}, got "
                           f"{spec[wire]!r}.") from None
        if value < 0:
            raise RyaError("E_VALIDATION",
                           f"Org budget `{wire}` must not be negative (got {value}).",
                           hint="Omit the key for 'unlimited'; 0 means 'spend nothing'.")
        kwargs[attr] = value
    return OrgBudget(source=source, **kwargs)


# (usage key, budget attribute, human label)
_LIMITS = (
    ("tokensToday", "max_tokens_per_day", "org tokens today"),
    ("costUsdToday", "max_cost_usd_per_day", "org USD today"),
    ("costUsdMonth", "max_cost_usd_per_month", "org USD this month"),
)


def violations_for(budget: OrgBudget, usage: Mapping[str, Any]) -> List[dict]:
    """Which org limits are at their ceiling. Pure, so the arithmetic is testable.

    Deliberately a free function rather than a method on a verdict object: the same
    comparison has to be made by the reconciler (which has the numbers) and asserted
    by a test (which has neither a database nor an org), and one implementation of it
    is what keeps the derived row and the check in agreement.
    """
    out = []
    for usage_key, attr, label in _LIMITS:
        limit = getattr(budget, attr)
        if limit is None:
            continue
        current = usage.get(usage_key) or 0
        if current >= limit:
            out.append({"limit": attr, "label": label, "current": current,
                        "max": limit, "scope": "org"})
    return out


def usage_row(*, org_id: str, budget: OrgBudget, usage: Mapping[str, Any],
              workspaces: Optional[List[str]] = None) -> dict:
    """The derived row written into each member workspace's policy table."""
    breaches = violations_for(budget, usage)
    return {"orgId": org_id, "exhausted": bool(breaches), "violations": breaches,
            "budget": budget.describe(), "usage": dict(usage),
            "workspaces": sorted(workspaces or []), "computedAt": now_iso(),
            "source": "reconcile"}


# How old a verdict may be before it is reported as stale. Generous relative to
# `DEFAULT_RECONCILE_SECONDS` — three missed cycles, not one — because a single slow
# tick is not a broken deployment and an alarm that cries wolf gets muted, which is the
# state this whole trigger exists to avoid.
DEFAULT_RECONCILE_SECONDS = 300.0
STALE_AFTER_SECONDS = 900.0


def verdict_age_seconds(verdict: Optional[Mapping[str, Any]],
                        *, now: Optional[datetime] = None) -> Optional[float]:
    """Seconds since a verdict was computed, or ``None`` if that is unknowable."""
    stamp = (verdict or {}).get("computedAt")
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    reference = now or datetime.now(timezone.utc)
    if when.tzinfo is None:  # pragma: no cover - now_iso always writes an offset
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (reference - when).total_seconds())


def freshness(store, *, now: Optional[datetime] = None,
              stale_after: float = STALE_AFTER_SECONDS) -> dict:
    """Whether this workspace's org verdict is being kept up to date.

    **This is the answer to §9's "nobody runs `rya orgs reconcile`".** An org budget is
    enforced through a derived per-workspace verdict (D35), and a deployment that sets
    a budget and never refreshes the verdict has a budget that caps nothing — the same
    failure `quotas.py` refuses for a mistyped limit, arrived at by omission instead.
    The supervisor now refreshes it (see `supervisor.supervise_workspaces`), and this
    is what says so when nothing does.

    Three states, and they are deliberately distinguishable:

    * ``none`` — no verdict at all. Either this workspace has no org, which is the
      ordinary single-tenant case and not a problem, or one was assigned and nothing
      has ever reconciled, which is.
    * ``stale`` — a verdict exists and is older than ``stale_after``. The number being
      enforced is real but out of date, so an org over budget keeps spending.
    * ``fresh`` — something is running.

    Reported rather than enforced. Refusing work because a billing rollup is late
    would turn a billing feature into an availability incident, which is the same
    reasoning `read_verdict` fails open for.
    """
    verdict = read_verdict(store)
    age = verdict_age_seconds(verdict, now=now)
    if verdict is None:
        return {"state": "none", "ageSeconds": None, "orgId": None, "stale": False,
                "detail": "no org verdict has been written for this workspace"}
    stale = age is None or age > stale_after
    return {
        "state": "stale" if stale else "fresh",
        "ageSeconds": None if age is None else round(age, 1),
        "orgId": verdict.get("orgId"),
        "stale": stale,
        "detail": (
            f"the org verdict is {age:.0f}s old (stale after {stale_after:.0f}s) — an "
            "org over budget keeps spending until something reconciles"
            if stale and age is not None else
            "the org verdict has no readable timestamp" if stale else
            f"reconciled {age:.0f}s ago"),
    }


def read_verdict(store) -> Optional[dict]:
    """This workspace's last-known org verdict, or ``None`` if there is none.

    Fails **open**, like ``purge.lifecycle`` and unlike the guard: an unreadable
    derived row means the reconciler has not run or the policy read failed, and
    refusing every tenant's work because a billing rollup is unavailable would turn
    a billing feature into an availability incident.
    """
    getter = getattr(store, "policy_get", None)
    if getter is None:
        return None
    try:
        row = getter(POLICY_KEY)
    except Exception:  # noqa: BLE001
        log.debug("org verdict unreadable", exc_info=True)
        return None
    return row if isinstance(row, dict) and row else None


def org_violations(store) -> List[dict]:
    """The org-level violations that apply to this workspace right now.

    What ``quotas.check_admission`` calls. It re-reads the *stored* violations rather
    than recomputing from the stored usage, so that "what was decided" and "what is
    enforced" cannot drift apart between a reconcile and an admission — the row is
    the decision, not its inputs.
    """
    verdict = read_verdict(store)
    if not verdict or not verdict.get("exhausted"):
        return []
    return [dict(v, scope="org", orgId=verdict.get("orgId"),
                 computedAt=verdict.get("computedAt"))
            for v in (verdict.get("violations") or [])]


# ---- the privileged half -----------------------------------------------------

def org_usage(admin_dsn: str, org_id: str, *,
              now: Optional[datetime] = None) -> dict:
    """Sum one org's meter across every workspace it owns.

    Reads ``rya_meter`` over the admin connection, which is the only connection that
    can: the table has ``FORCE ROW LEVEL SECURITY`` and its policy pins to
    ``app.workspace_id``, so a per-tenant handle sees one tenant by construction.
    That is the property being preserved rather than worked around — the aggregate is
    computed *outside* the tenant plane and only its verdict is pushed back in.

    Returns per-workspace subtotals alongside the org totals, because "which of my
    twelve workspaces spent the money" is the first question anyone asks after
    "why did my org stop".
    """
    import psycopg

    day, month = _day_start(now), _month_start(now)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM rya_workspaces WHERE org_id=%s ORDER BY id",
                        (org_id,))
            members = [r[0] for r in cur.fetchall()]
            if not members:
                return {"orgId": org_id, "workspaces": [], "tokensToday": 0,
                        "costUsdToday": 0.0, "costUsdMonth": 0.0, "byWorkspace": {},
                        "since": day, "monthSince": month}
            cur.execute(
                """SELECT workspace_id,
                          COALESCE(SUM(CASE WHEN ts >= %s
                                       THEN input_tokens + output_tokens END), 0),
                          COALESCE(SUM(CASE WHEN ts >= %s THEN cost_usd END), 0),
                          COALESCE(SUM(cost_usd), 0)
                     FROM rya_meter
                    WHERE workspace_id = ANY(%s) AND ts >= %s
                 GROUP BY workspace_id""",
                (day, day, members, month))
            rows = cur.fetchall()
    by_workspace = {r[0]: {"tokensToday": int(r[1]), "costUsdToday": round(float(r[2]), 6),
                           "costUsdMonth": round(float(r[3]), 6)} for r in rows}
    return {
        "orgId": org_id,
        "workspaces": members,
        "tokensToday": sum(v["tokensToday"] for v in by_workspace.values()),
        "costUsdToday": round(sum(v["costUsdToday"] for v in by_workspace.values()), 6),
        "costUsdMonth": round(sum(v["costUsdMonth"] for v in by_workspace.values()), 6),
        "byWorkspace": by_workspace,
        "since": day, "monthSince": month,
    }


def reconcile(admin_dsn: str, *, org_id: Optional[str] = None,
              now: Optional[datetime] = None, dry_run: bool = False) -> List[dict]:
    """Recompute every org's rollup and push its verdict to member workspaces.

    Idempotent, and safe to run from a cron, a supervisor tick or by hand. One org's
    failure does not stop the others: an org whose usage cannot be read is logged and
    skipped, leaving its members' previous verdict in place — which is the
    conservative direction, since the previous verdict is either "fine" (and the org
    keeps working, bounded by the next successful tick) or "exhausted" (and it stays
    refused until somebody looks).

    The derived row is written with plain SQL rather than through
    ``store.policy_set``, deliberately. ``policy_set`` appends to ``rya_policy_log``,
    which exists so that "who reviewed this allowlist change" is answerable (§12 risk
    7); a computed row rewritten every minute would bury that history under machine
    noise. So: the audit log keeps recording decisions people made, and this records
    a fact.
    """
    import psycopg
    from psycopg.types.json import Json

    out: List[dict] = []
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            if org_id:
                cur.execute("SELECT id, name, budget FROM rya_organizations WHERE id=%s",
                            (org_id,))
            else:
                cur.execute("SELECT id, name, budget FROM rya_organizations ORDER BY id")
            orgs = cur.fetchall()

    for oid, name, raw_budget in orgs:
        try:
            budget = coerce_budget(raw_budget or {})
            usage = org_usage(admin_dsn, oid, now=now)
        except Exception as exc:  # noqa: BLE001 - one org is not every org
            log.warning("org %s could not be reconciled: %s", oid, exc)
            out.append({"orgId": oid, "name": name, "ok": False, "error": str(exc)})
            continue
        row = usage_row(org_id=oid, budget=budget, usage=usage,
                        workspaces=usage["workspaces"])
        record = {"orgId": oid, "name": name, "ok": True,
                  "exhausted": row["exhausted"], "workspaces": usage["workspaces"],
                  "usage": {k: usage[k] for k in ("tokensToday", "costUsdToday",
                                                  "costUsdMonth")},
                  "written": 0}
        if not dry_run and usage["workspaces"]:
            with psycopg.connect(admin_dsn, autocommit=True) as conn:
                with conn.cursor() as cur:
                    for ws in usage["workspaces"]:
                        cur.execute(
                            """INSERT INTO rya_policy (workspace_id, key, value, version,
                                                       actor, changed_at)
                               VALUES (%s,%s,%s,1,%s,%s)
                               ON CONFLICT (workspace_id, key) DO UPDATE
                                 SET value = EXCLUDED.value,
                                     version = rya_policy.version + 1,
                                     actor = EXCLUDED.actor,
                                     changed_at = EXCLUDED.changed_at""",
                            (ws, POLICY_KEY, Json(row), "orgs.reconcile", now_iso()))
                        record["written"] += 1
        out.append(record)
    log.info("reconciled %d org(s); %d exhausted", len(out),
             sum(1 for r in out if r.get("exhausted")))
    return out
