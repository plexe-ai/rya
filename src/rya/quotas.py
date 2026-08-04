"""Per-workspace quotas — the hosted-operation control (PLATFORM_DESIGN §11.12, D13).

D13 makes the deployment multi-tenant with process isolation per tenant. Process
isolation stops one tenant *reading* another's data; it does nothing about one
tenant consuming the whole node. §6 names the missing half: "one workspace must
not starve another". This module is the resource half of that, and
``queue.claim``'s fair ordering is the scheduling half.

Six limits, all optional, all meaning "unlimited" when unset:

======================  =============================================
``maxConcurrentRuns``   non-terminal runs at once
``maxRunsPerDay``       runs started in the current UTC day
``maxQueueDepth``       pending queue jobs
``maxTokensPerDay``     input+output tokens from the durable meter (D10)
``maxCostUsdPerDay``    cost from the same meter
``maxWorkers``          live registered worker processes (§6)
======================  =============================================

**Where enforcement happens, and why it was admission-only.** A quota is checked
when new work is *admitted* — a run starts, a job is enqueued, a worker
registers. It is never checked mid-run. Killing a run halfway because its
workspace crossed a token budget would leave a journal that can never replay to a
terminal state, which is a durability bug traded for a billing nicety. Token and
cost limits therefore throttle the *next* piece of work, and the overshoot is
bounded by one run rather than by nothing.

**D30 added one more enforcement point, and only one.** The broker checks
``kind="model"`` before every inference call (MULTITENANT #21). The reasoning above
still holds for *runs* and is why nothing else moved: what changed is who pays for an
overrun. While the tenant held the provider key, an overshoot bounded by one run spent
the tenant's money; with a pooled platform key it spends ours, so theft-of-service
became a billing control rather than an abuse nicety. The distinction that keeps the
durability argument intact is that a refused **model call** is an exception a handler
can catch and a journal can record, whereas a killed **run** is a journal that can
never reach a terminal state. ``kind="model"`` therefore selects only the two limits
that map to money — ``maxTokensPerDay`` and ``maxCostUsdPerDay``, the ``any`` rows in
``_LIMITS`` — and never the concurrency or queue-depth caps.

**D31 sits in front of all of it.** ``require_admission`` refuses a *disabled*
workspace before reading a quota at all, which is the enforcement half of
``purge.disable``: revoking API keys stops new callers, and this is what stops the
work already queued.

**D29 sits beside it, one boundary up.** Every limit here is per *workspace*, which
is the isolation boundary; an organization is the *billing* boundary and owns many
workspaces, so a budget belongs to it. :func:`check_admission` therefore appends the
org's violations to this workspace's own, read from a derived policy row that a
privileged reconciler writes — see :mod:`rya.orgs` for why the aggregate is not
computed here, and why the staleness that buys is the same trade §11.12 already made
for token limits. Usage stays metered at ``workspace_id`` throughout: the org total
is a sum of tenant rows, never a replacement for them, so "which workspace spent it"
remains answerable.

**Who may set a quota.** In a multi-tenant deployment, obviously not the tenant:
a limit the limited party can raise is not a limit. The api routes that write this
policy require the admin token (``RYA_ADMIN_TOKEN``), the same gate provisioning
uses. In a single-tenant self-host, the operator and the tenant are the same
person and the distinction is moot.

Reads are cheap by construction: ``store.run_counts`` and ``store.queue_counts``
count in the database, and the meter is already the append-only ledger billing
reads (D10), so a quota check adds no new bookkeeping.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from .errors import RyaError
from .turns import TERMINAL_RUN_STATUSES

POLICY_KEY = "quotas"

# Wire name -> (attribute, coercion). As with gates.py, an unrecognised key is an
# ERROR: a mistyped `maxRunsPerDay` that silently resolved to unlimited would be a
# quota that reports itself as configured while capping nothing.
_QUOTA_FIELDS: dict[str, tuple[str, type]] = {
    "maxConcurrentRuns": ("max_concurrent_runs", int),
    "maxRunsPerDay": ("max_runs_per_day", int),
    "maxQueueDepth": ("max_queue_depth", int),
    "maxTokensPerDay": ("max_tokens_per_day", int),
    "maxCostUsdPerDay": ("max_cost_usd_per_day", float),
    "maxWorkers": ("max_workers", int),
}


def _day_start(now: Optional[datetime] = None) -> str:
    """Start of the current UTC day, in the store's ISO format.

    A fixed UTC day rather than a rolling window: a rolling window needs
    per-request time arithmetic over the ledger, and "resets at midnight UTC" is
    something an operator can explain to a customer.
    """
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT00:00:00Z")


@dataclass(frozen=True)
class QuotaPolicy:
    """Resolved limits for one workspace. Every field ``None`` means unlimited."""

    max_concurrent_runs: Optional[int] = None
    max_runs_per_day: Optional[int] = None
    max_queue_depth: Optional[int] = None
    max_tokens_per_day: Optional[int] = None
    max_cost_usd_per_day: Optional[float] = None
    max_workers: Optional[int] = None
    source: str = "default"

    @property
    def enforced(self) -> bool:
        return any(v is not None for v in (
            self.max_concurrent_runs, self.max_runs_per_day, self.max_queue_depth,
            self.max_tokens_per_day, self.max_cost_usd_per_day, self.max_workers))

    def describe(self) -> dict:
        return {
            "enforced": self.enforced,
            "source": self.source,
            "maxConcurrentRuns": self.max_concurrent_runs,
            "maxRunsPerDay": self.max_runs_per_day,
            "maxQueueDepth": self.max_queue_depth,
            "maxTokensPerDay": self.max_tokens_per_day,
            "maxCostUsdPerDay": self.max_cost_usd_per_day,
            "maxWorkers": self.max_workers,
        }


@dataclass
class QuotaVerdict:
    """Whether new work may be admitted, and which limits are at their ceiling."""

    allowed: bool
    policy: QuotaPolicy
    violations: list[dict]
    usage: dict

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "quota": self.policy.describe(),
                "violations": self.violations, "usage": self.usage}


def _coerce(spec: Mapping[str, Any], source: str) -> QuotaPolicy:
    unknown = [k for k in spec if k not in _QUOTA_FIELDS]
    if unknown:
        raise RyaError(
            "E_VALIDATION",
            f"Unrecognised quota key(s): {', '.join(sorted(unknown))}.",
            hint="Valid keys: " + ", ".join(sorted(_QUOTA_FIELDS)) + ". The policy is refused "
            "rather than partially applied — a mistyped limit that silently capped nothing "
            "would be worse than no quota.",
        )
    kwargs: dict[str, Any] = {}
    for wire, (attr, cast) in _QUOTA_FIELDS.items():
        if wire not in spec or spec[wire] is None:
            continue
        try:
            value = cast(spec[wire])
        except (TypeError, ValueError):
            raise RyaError("E_VALIDATION",
                           f"Quota `{wire}` must be a {cast.__name__}, got {spec[wire]!r}.") from None
        if value < 0:
            raise RyaError("E_VALIDATION", f"Quota `{wire}` must not be negative (got {value}).",
                           hint="Omit the key for 'unlimited'; 0 means 'admit nothing'.")
        kwargs[attr] = value
    return QuotaPolicy(source=source, **kwargs)


def resolve_quota(store) -> QuotaPolicy:
    """The quota for this store's workspace. Never ``None``.

    The store is already workspace-scoped (RLS pins the session to one workspace),
    so "this workspace's quota" is just this workspace's policy row — no tenant id
    is threaded through, and one tenant cannot read or write another's.
    """
    policy = store.policy_get(POLICY_KEY) or {}
    if not isinstance(policy, Mapping):
        raise RyaError(
            "E_QUOTA_EXCEEDED",
            f"The quota policy is malformed ({type(policy).__name__}, expected an object).",
            hint="Admission is refused while the quota cannot be read, rather than proceeding "
            "unlimited. Re-set it with `rya quotas set`.",
        )
    return _coerce(policy, "policy" if policy else "default")


def set_quota(store, policy: Mapping[str, Any] | None, *, actor: Optional[str] = None) -> dict:
    """Write the quota policy, validating it first. ``None`` clears it."""
    if policy is None:
        return store.policy_set(POLICY_KEY, None, actor=actor)
    if not isinstance(policy, Mapping):
        raise RyaError("E_VALIDATION", "The quota policy must be an object.",
                       hint='e.g. {"maxConcurrentRuns": 10, "maxCostUsdPerDay": 25}')
    _coerce(policy, "policy")
    return store.policy_set(POLICY_KEY, dict(policy), actor=actor)


def _usage_for(store, keys: set[str], *, now: Optional[datetime] = None) -> dict:
    """Consumption for exactly the requested keys, and nothing more.

    Laziness is not premature here: admission is on the hot path — every
    ``queue.enqueue`` calls it — and a naive snapshot issues five queries to
    answer a question that usually needs one. A job enqueue looks at queue depth
    and the meter; it has no reason to count runs or list workers.
    """
    since = _day_start(now)
    usage: dict[str, Any] = {"since": since}

    if "concurrentRuns" in keys and hasattr(store, "run_counts"):
        counts = store.run_counts()
        usage["concurrentRuns"] = sum(n for status, n in counts.items()
                                      if status not in TERMINAL_RUN_STATUSES)
    if "runsToday" in keys and hasattr(store, "run_counts"):
        usage["runsToday"] = sum(store.run_counts(since=since).values())
    if "queueDepth" in keys and hasattr(store, "queue_counts"):
        usage["queueDepth"] = int(store.queue_counts().get("pending") or 0)
    if {"tokensToday", "costUsdToday"} & keys and hasattr(store, "meter_totals"):
        totals = store.meter_totals(since=since)
        usage["tokensToday"] = (int(totals.get("inputTokens") or 0)
                                + int(totals.get("outputTokens") or 0))
        usage["costUsdToday"] = round(float(totals.get("costUsd") or 0.0), 6)
    if "workers" in keys and hasattr(store, "worker_list"):
        usage["workers"] = len(store.worker_list(status="alive"))
    return usage


def usage_snapshot(store, *, now: Optional[datetime] = None) -> dict:
    """Everything this workspace is currently consuming.

    Kept separate from :func:`check_admission` so the console and ``rya status``
    can show consumption against a limit without pretending to admit anything.
    """
    return _usage_for(store, {"concurrentRuns", "runsToday", "queueDepth",
                              "tokensToday", "costUsdToday", "workers"}, now=now)


# (usage key, policy attribute, human label, the thing being admitted)
_LIMITS = (
    ("concurrentRuns", "max_concurrent_runs", "concurrent runs", "run"),
    ("runsToday", "max_runs_per_day", "runs today", "run"),
    ("queueDepth", "max_queue_depth", "pending queue jobs", "job"),
    ("tokensToday", "max_tokens_per_day", "tokens today", "any"),
    ("costUsdToday", "max_cost_usd_per_day", "USD today", "any"),
    ("workers", "max_workers", "live workers", "worker"),
)


def check_admission(store, *, kind: str = "run", usage: Optional[dict] = None,
                    now: Optional[datetime] = None) -> QuotaVerdict:
    """Would a new ``kind`` of work be admitted? Returns a verdict, never raises.

    ``kind`` selects which limits apply: starting a run is not blocked by the
    queue being deep, and enqueuing a job is not blocked by the run concurrency
    cap. Token and cost ceilings apply to everything, because they are the ones
    that map to money. ``kind="any"`` checks every limit — what a status page
    wants.
    """
    policy = resolve_quota(store)

    applicable = [(usage_key, attr, label) for usage_key, attr, label, applies_to in _LIMITS
                  if getattr(policy, attr) is not None
                  and (kind == "any" or applies_to in ("any", kind))]
    if usage is None:
        # Only query what an applicable limit actually needs — an unenforced quota
        # costs zero queries, which is what keeps this callable from enqueue().
        usage = _usage_for(store, {k for k, _, _ in applicable}, now=now)

    violations = []
    for usage_key, attr, label in applicable:
        limit = getattr(policy, attr)
        current = usage.get(usage_key) or 0
        if current >= limit:
            violations.append({
                "limit": attr, "label": label, "current": current, "max": limit,
                "scope": "workspace",
            })
    # D29: the org's budget, appended to this workspace's own verdict.
    #
    # Read from a *derived* policy row rather than computed here, and that is the whole
    # design (see `rya.orgs`): summing an org's meter needs a connection that spans
    # tenants, and putting one on every tenant's admission path would undo what Phase 4
    # spent itself removing. The rollup is computed by a privileged reconciler and its
    # verdict pushed down, so this stays a read of the caller's own row.
    #
    # Appended *after* the workspace violations so the more local reason is named first
    # when both are exhausted: "you are over your own token cap" is actionable by the
    # tenant, and "your organization is over its monthly budget" is not.
    #
    # Applied for every `kind`, including `worker` and `job`. An org over budget should
    # not merely stop spending on inference — it should stop accepting work that will
    # spend, and a queue that keeps filling behind an exhausted budget is a backlog
    # somebody still has to pay for later.
    from .orgs import org_violations

    violations += org_violations(store)
    return QuotaVerdict(allowed=not violations, policy=policy,
                        violations=violations, usage=usage)


def require_admission(store, *, kind: str = "run", usage: Optional[dict] = None,
                      now: Optional[datetime] = None) -> QuotaVerdict:
    """Enforce the quota, raising ``E_QUOTA_EXCEEDED`` when it is exhausted.

    The message names every exhausted limit with its current value, because "quota
    exceeded" alone tells a caller nothing about whether to retry in a second or
    to go buy more capacity.
    """
    # D31: a disabled workspace is refused before its quota is even read, and this is
    # the hook rather than each of the four admission call sites, because "may this
    # workspace do work at all" is the same question they were already asking here —
    # and a fifth admission path added later would otherwise miss it. Ordered first
    # because "this tenant is disabled" is a more useful refusal than "and it is also
    # over its token budget".
    from .purge import require_active

    require_active(store)
    verdict = check_admission(store, kind=kind, usage=usage, now=now)
    if verdict.allowed:
        return verdict
    detail = "; ".join(f"{v['label']} {v['current']}/{v['max']}" for v in verdict.violations)
    daily = [v for v in verdict.violations if "Day" in v["limit"] or "today" in v["label"]]
    org = [v for v in verdict.violations if v.get("scope") == "org"]
    # Which *boundary* refused is the first thing the reader needs. A tenant told
    # "workspace quota exhausted" while its own usage is near zero will go looking in
    # the wrong place — the limit that stopped it belongs to its organization, which
    # may have eleven other workspaces spending the budget (D29).
    subject = ("Organization budget exhausted" if org and len(org) == len(verdict.violations)
               else "Workspace quota exhausted")
    raise RyaError(
        "E_QUOTA_EXCEEDED",
        f"{subject}, refusing to admit a new {kind}: {detail}.",
        hint=("Daily limits reset at 00:00 UTC. " if daily else
              "In-flight work will free capacity; retry after it completes. ")
        + ("An org budget is shared across every workspace the organization owns, and "
           "is recomputed periodically — `rya orgs show <id>` names which workspace "
           "spent it. " if org else "")
        + "Inspect with `rya quotas show --json`; raise the limit with `rya quotas set` "
        "(admin token required in a multi-tenant deployment).",
    )
