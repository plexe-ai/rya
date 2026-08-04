"""Org-aggregated usage and budget (D29) — the billing boundary above a workspace.

The split rule is the thing under test: **usage is metered at ``workspace_id`` and
budgeted at ``org_id``, and no isolation boundary moves.** So the assertions come in
two halves —

* the arithmetic and the enforcement, which are pure or store-local and run
  everywhere;
* the cross-workspace rollup, which needs Postgres because it needs a connection
  that spans tenants — and *that* is the property, not an inconvenience. The
  aggregate is computed outside the tenant plane and only its verdict is pushed
  back in, so no tenant's admission path ever holds a credential that can read a
  sibling.
"""

import os

import pytest

from rya import orgs as O
from rya import quotas
from rya.errors import RyaError
from rya.store import Store

DSN = os.environ.get("RYA_TEST_DATABASE_URL") or os.environ.get("RYA_DATABASE_URL")
needs_pg = pytest.mark.skipif(not DSN, reason="org rollup needs Postgres")


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path)
    s.ensure()
    return s


# ---- the budget vocabulary -------------------------------------------------

def test_only_money_limits_are_accepted():
    """An org is the BILLING boundary. An org-level `maxConcurrentRuns` would be a
    scheduling limit at a boundary that does no scheduling — it would have to be
    enforced by summing live runs across workspaces, on the admission path, over the
    connection this whole design exists to keep off it."""
    assert O.coerce_budget({"maxCostUsdPerMonth": 500}).max_cost_usd_per_month == 500
    with pytest.raises(RyaError) as e:
        O.coerce_budget({"maxConcurrentRuns": 5})
    assert e.value.code == "E_VALIDATION"
    assert "BILLING boundary" in (e.value.hint or "")


def test_a_mistyped_budget_key_is_refused_rather_than_ignored():
    """The same stance `quotas.py` and `gates.py` take, and at the org level the thing
    that silently capped nothing is somebody's invoice."""
    with pytest.raises(RyaError):
        O.coerce_budget({"maxCostUsdPerMoth": 500})
    with pytest.raises(RyaError):
        O.coerce_budget({"maxCostUsdPerDay": -1})
    assert O.coerce_budget(None).enforced is False
    assert O.coerce_budget({}).source == "default"


def test_zero_means_spend_nothing_and_unset_means_unlimited():
    assert O.coerce_budget({"maxCostUsdPerDay": 0}).max_cost_usd_per_day == 0
    assert O.violations_for(O.coerce_budget({"maxCostUsdPerDay": 0}),
                            {"costUsdToday": 0})
    assert O.coerce_budget({}).max_cost_usd_per_day is None
    assert O.violations_for(O.coerce_budget({}), {"costUsdToday": 10 ** 9}) == []


def test_violations_name_the_boundary_they_belong_to():
    """`scope` is what lets a refusal say "your organization is over budget" instead
    of "your workspace quota is exhausted" to a tenant that is nearly idle."""
    budget = O.coerce_budget({"maxTokensPerDay": 100, "maxCostUsdPerMonth": 5})
    breaches = O.violations_for(budget, {"tokensToday": 100, "costUsdMonth": 4.99})
    assert [v["limit"] for v in breaches] == ["max_tokens_per_day"]
    assert breaches[0]["scope"] == "org"
    assert breaches[0]["label"].startswith("org ")


# ---- enforcement, from the derived row -------------------------------------

def _publish_verdict(store, **over):
    budget = O.coerce_budget(over.pop("budget", {"maxCostUsdPerMonth": 10}))
    row = O.usage_row(org_id="org_acme", budget=budget,
                      usage=over.pop("usage", {"costUsdMonth": 12.0}),
                      workspaces=["ws_a", "ws_b"])
    row.update(over)
    store.policy_set(O.POLICY_KEY, row)
    return row


def test_an_exhausted_org_budget_refuses_this_workspaces_admission(store):
    """The push-down, from the tenant's side. The workspace has no quota of its own
    and is not over anything it can see; what stops it is the org row."""
    assert quotas.check_admission(store, kind="run").allowed is True
    _publish_verdict(store)

    verdict = quotas.check_admission(store, kind="run")
    assert verdict.allowed is False
    assert [v["scope"] for v in verdict.violations] == ["org"]
    with pytest.raises(RyaError) as e:
        quotas.require_admission(store, kind="run")
    assert e.value.code == "E_QUOTA_EXCEEDED"
    assert "Organization budget exhausted" in e.value.message
    assert "rya orgs show" in (e.value.hint or "")


def test_an_org_within_budget_changes_nothing(store):
    """A verdict that says "fine" must be as inert as no verdict at all, or the
    reconciler becomes a thing that can break every tenant by running."""
    _publish_verdict(store, usage={"costUsdMonth": 1.0},
                     budget={"maxCostUsdPerMonth": 10})
    assert store.policy_get(O.POLICY_KEY)["exhausted"] is False
    assert quotas.check_admission(store, kind="run").allowed is True
    assert O.org_violations(store) == []


def test_the_workspaces_own_reason_is_named_first(store):
    """Both boundaries exhausted at once. "You are over your own token cap" is
    actionable by the tenant and "your organization is over its monthly budget" is
    not, so the local one leads."""
    quotas.set_quota(store, {"maxTokensPerDay": 1})
    store.meter_append({"runId": "r", "inputTokens": 5})
    _publish_verdict(store)

    verdict = quotas.check_admission(store, kind="run")
    assert [v.get("scope") for v in verdict.violations] == ["workspace", "org"]
    with pytest.raises(RyaError) as e:
        quotas.require_admission(store, kind="run")
    # Not attributed to the org alone, because it is not the org alone.
    assert e.value.message.startswith("Workspace quota exhausted")


def test_the_org_budget_applies_to_jobs_and_workers_too(store):
    """An org over budget should not merely stop spending on inference — it should
    stop accepting work that will spend. A queue that keeps filling behind an
    exhausted budget is a backlog somebody still has to pay for later."""
    _publish_verdict(store)
    for kind in ("run", "job", "worker", "model"):
        with pytest.raises(RyaError):
            quotas.require_admission(store, kind=kind)


def test_a_missing_or_unreadable_verdict_fails_open(store):
    """Like `purge.lifecycle` and unlike the guard. A billing rollup that has not run
    yet must not become an availability incident for every tenant."""
    assert O.read_verdict(store) is None
    assert O.org_violations(store) == []
    store.policy_set(O.POLICY_KEY, {"orgId": "org_x"})   # no `exhausted`, no violations
    assert O.org_violations(store) == []
    assert quotas.check_admission(store, kind="run").allowed is True


def test_the_derived_row_says_it_is_derived(store):
    """An operator who finds a workspace refusing work has to be able to tell "this
    tenant is over ITS budget" from "this tenant's org is, and the tenant may be
    nearly idle" — and then which sibling spent it."""
    row = _publish_verdict(store)
    assert row["source"] == "reconcile"
    assert row["orgId"] == "org_acme"
    assert row["workspaces"] == ["ws_a", "ws_b"]
    assert row["computedAt"]
    breach = O.org_violations(store)[0]
    assert breach["orgId"] == "org_acme"
    assert breach["computedAt"] == row["computedAt"]


def test_the_org_verdict_lives_in_its_own_policy_key(store):
    """`rya quotas set` is an operator writing a limit; this is the platform writing a
    computed fact. Sharing one row would let a reconcile tick clobber a limit somebody
    typed."""
    quotas.set_quota(store, {"maxRunsPerDay": 7})
    _publish_verdict(store)
    assert quotas.resolve_quota(store).max_runs_per_day == 7
    assert O.POLICY_KEY != quotas.POLICY_KEY


def test_the_api_reports_the_org_rollup_beside_the_workspaces_own(tmp_path):
    """`/quotas` and `/usage` gain the rollup (§7's D29 item). It comes from the
    derived row, because this route runs on the tenant plane and holds no connection
    that could compute one — and it is ABSENT rather than empty when the reconciler
    has never run, since "no rollup" and "an all-clear rollup" are different states."""
    from fastapi.testclient import TestClient

    from rya.api.app import build_app
    from rya.cli import scaffold

    root = tmp_path / "proj"
    scaffold.write_project(root, "orgy", template="demo")
    client = TestClient(build_app(root))
    assert "org" not in client.get("/quotas").json()

    store = Store(root)
    store.ensure()
    _publish_verdict(store)
    body = client.get("/quotas").json()
    assert body["org"]["orgId"] == "org_acme"
    assert body["org"]["exhausted"] is True
    assert [v["scope"] for v in body["admission"]] == ["org"]
    usage = client.get("/usage").json()
    assert usage["org"]["budget"]["maxCostUsdPerMonth"] == 10
    # The tenant's own metered usage is still there and is still the authority.
    assert "usage" in usage


# ---- the privileged rollup -------------------------------------------------

@needs_pg
def test_the_rollup_sums_across_an_orgs_workspaces_and_names_the_spender():
    """The part that needs the admin connection, and the reason it does: `rya_meter`
    has FORCE ROW LEVEL SECURITY with a policy pinned to `app.workspace_id`, so a
    per-tenant handle sees one tenant by construction. Two workspaces in one org, one
    of them doing all the spending."""
    from rya.store_postgres import PostgresStore
    from rya.tenancy import Tenancy

    tenancy = Tenancy(DSN)
    tenancy.setup()
    try:
        org = tenancy.create_organization("rollup-test")
        a = tenancy.create_workspace("rollup-a", org_id=org["id"])
        b = tenancy.create_workspace("rollup-b", org_id=org["id"])
        for ws, tokens, cost in ((a["id"], 100, 1.5), (b["id"], 5, 0.25)):
            store = PostgresStore(DSN, ws)
            store.ensure()
            store.meter_append({"runId": store.new_run_id(), "inputTokens": tokens,
                                "outputTokens": 0, "costUsd": cost, "model": "m"})
            store.close()

        usage = O.org_usage(DSN, org["id"])
        assert sorted(usage["workspaces"]) == sorted([a["id"], b["id"]])
        assert usage["tokensToday"] == 105
        assert round(usage["costUsdToday"], 4) == 1.75
        assert usage["byWorkspace"][a["id"]]["tokensToday"] == 100
        assert usage["byWorkspace"][b["id"]]["tokensToday"] == 5

        # And the verdict lands in BOTH members' own policy rows, so each of them
        # refuses without either being able to read the other.
        tenancy.set_org_budget(org["id"], {"maxCostUsdPerDay": 1.0})
        results = O.reconcile(DSN, org_id=org["id"])
        assert results[0]["exhausted"] is True
        assert results[0]["written"] == 2
        for ws in (a["id"], b["id"]):
            store = PostgresStore(DSN, ws)
            try:
                assert O.read_verdict(store)["orgId"] == org["id"]
                with pytest.raises(RyaError):
                    quotas.require_admission(store, kind="run")
            finally:
                store.close()
    finally:
        tenancy.close()


@needs_pg
def test_a_workspace_in_another_org_is_untouched_by_the_rollup():
    """The boundary that must NOT move. An org shares a bill; it shares nothing else,
    and a reconcile for one org must write nothing into another's tenants."""
    from rya.store_postgres import PostgresStore
    from rya.tenancy import Tenancy

    tenancy = Tenancy(DSN)
    tenancy.setup()
    try:
        mine = tenancy.create_organization("isolated-mine")
        theirs = tenancy.create_organization("isolated-theirs")
        a = tenancy.create_workspace("isolated-a", org_id=mine["id"])
        b = tenancy.create_workspace("isolated-b", org_id=theirs["id"])
        store = PostgresStore(DSN, a["id"])
        store.ensure()
        store.meter_append({"runId": store.new_run_id(), "inputTokens": 50,
                            "costUsd": 9.0, "model": "m"})
        store.close()
        tenancy.set_org_budget(mine["id"], {"maxCostUsdPerDay": 1.0})

        O.reconcile(DSN, org_id=mine["id"])
        other = PostgresStore(DSN, b["id"])
        other.ensure()
        try:
            assert O.read_verdict(other) is None
            assert quotas.check_admission(other, kind="run").allowed is True
        finally:
            other.close()
        # The other org's usage does not include the spender either.
        assert O.org_usage(DSN, theirs["id"])["costUsdToday"] == 0
    finally:
        tenancy.close()


@needs_pg
def test_a_dry_run_computes_and_writes_nothing():
    from rya.tenancy import Tenancy

    tenancy = Tenancy(DSN)
    tenancy.setup()
    try:
        org = tenancy.create_organization("dry-run-test")
        ws = tenancy.create_workspace("dry-run-ws", org_id=org["id"])
        tenancy.set_org_budget(org["id"], {"maxCostUsdPerDay": 0})
        results = O.reconcile(DSN, org_id=org["id"], dry_run=True)
        assert results[0]["exhausted"] is True and results[0]["written"] == 0

        from rya.store_postgres import PostgresStore

        store = PostgresStore(DSN, ws["id"])
        store.ensure()
        try:
            assert O.read_verdict(store) is None
        finally:
            store.close()
    finally:
        tenancy.close()


# ---- who refreshes the verdict (§9: "Nobody runs `rya orgs reconcile`") -----

def test_a_verdict_nobody_refreshes_reports_itself_stale(store):
    """The trigger, closed at the reporting end.

    D35 enforces an org budget through a derived per-workspace verdict, and a
    deployment that set a budget and never scheduled a reconciler had a budget that
    capped nothing — the same failure `quotas.py` refuses for a mistyped limit,
    arrived at by omission instead. It is *reported* rather than enforced, because
    refusing every tenant's work when a billing rollup is late would turn a billing
    feature into an availability incident.
    """
    from datetime import datetime, timedelta, timezone

    # No verdict at all: either no org (ordinary, fine) or nothing has ever run (not).
    assert O.freshness(store)["state"] == "none"
    assert O.freshness(store)["stale"] is False

    budget = O.coerce_budget({"maxCostUsdPerDay": 10.0})
    store.policy_set(O.POLICY_KEY, O.usage_row(
        org_id="org_1", budget=budget, usage={"costUsdToday": 1.0},
        workspaces=["ws_a"]), actor="test")

    fresh = O.freshness(store)
    assert fresh["state"] == "fresh" and fresh["stale"] is False
    assert fresh["orgId"] == "org_1" and fresh["ageSeconds"] < 60

    # The same row, an hour later.
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    stale = O.freshness(store, now=later)
    assert stale["state"] == "stale" and stale["stale"] is True
    assert "keeps spending" in stale["detail"]


def test_a_verdict_with_no_readable_timestamp_is_stale_not_fresh(store):
    """Fail toward "somebody should look". An unparseable `computedAt` is a verdict
    whose age is unknown, and treating unknown as current is how a budget stops being
    enforced silently — the same direction `isolation_rank` takes for an unknown
    level."""
    store.policy_set(O.POLICY_KEY, {"orgId": "org_1", "computedAt": "not-a-date"},
                     actor="test")
    assert O.freshness(store)["stale"] is True
    assert O.verdict_age_seconds({"computedAt": "not-a-date"}) is None
    assert O.verdict_age_seconds({}) is None


def test_the_supervisor_fan_out_reconciles_at_most_once_per_interval(monkeypatch):
    """The scheduler, and why it is a supervisor tick rather than a fourth run mode.

    The supervisor already runs continuously, already holds the admin DSN in
    `--all-workspaces` mode, and already has D34's lease. A `rya reconciler` process
    would be a fourth thing to deploy and monitor for one query a minute.

    Throttled by wall clock rather than by a durable row: `orgs.reconcile` is
    idempotent, so two supervisors both running it costs a duplicated query and not a
    wrong answer, and a lease for that would be machinery for nothing.
    """
    from rya.execution import supervisor as S

    calls = []
    monkeypatch.setattr(O, "reconcile", lambda dsn, **kw: calls.append(dsn) or [])
    monkeypatch.setattr(S, "_last_reconcile", {})
    policy = S.SupervisorPolicy(reconcile_orgs_seconds=300.0)

    assert S.reconcile_orgs("dsn://x", policy=policy, now=1000.0) == []
    assert S.reconcile_orgs("dsn://x", policy=policy, now=1100.0) is None   # too soon
    assert S.reconcile_orgs("dsn://x", policy=policy, now=1400.0) == []     # 400s later
    # A different deployment's DSN has its own clock.
    assert S.reconcile_orgs("dsn://y", policy=policy, now=1401.0) == []
    assert calls == ["dsn://x", "dsn://x", "dsn://y"]


def test_the_reconciler_can_be_turned_off_and_a_failure_never_stops_scheduling(monkeypatch):
    """Two separate refusals to let billing break the fleet.

    Off is a real setting — a deployment with a cron already running one does not want
    two — and a rollup that raises is logged and swallowed, because the supervisor's
    job is keeping the fleet matched to the work. Same direction `read_verdict` fails
    in, for the same reason.
    """
    from rya.execution import supervisor as S

    monkeypatch.setattr(S, "_last_reconcile", {})
    off = S.SupervisorPolicy(reconcile_orgs_seconds=0)
    assert S.reconcile_orgs("dsn://x", policy=off, now=1.0) is None

    def boom(dsn, **kw):
        raise RuntimeError("postgres is down")

    monkeypatch.setattr(O, "reconcile", boom)
    on = S.SupervisorPolicy(reconcile_orgs_seconds=60.0)
    assert S.reconcile_orgs("dsn://x", policy=on, now=2.0) is None   # swallowed


@needs_pg
def test_the_fan_out_actually_refreshes_a_stale_verdict(tmp_path, monkeypatch):
    """End to end over the admin connection: a budget set, a verdict absent, one fan-out
    tick, and the verdict is there. The whole content of the trigger being closed.

    ``RYA_DATABASE_URL`` is set rather than assumed. `supervise_workspaces` opens each
    member's store through `open_worker_store`, which resolves from the environment —
    so without it the fan-out reads FileStores in `project_root` and reconciles a
    database nobody is using. That is the *right* behaviour and a confusing test.
    """
    from rya.execution import supervisor as S
    from rya.execution.drivers import LocalDriver
    from rya.store_postgres import PostgresStore
    from rya.tenancy import Tenancy

    monkeypatch.setenv("RYA_DATABASE_URL", DSN)
    tenancy = Tenancy(DSN)
    tenancy.setup()
    try:
        org = tenancy.create_organization("fan-out-test")
        ws = tenancy.create_workspace("fan-out-ws", org_id=org["id"])
        tenancy.set_org_budget(org["id"], {"maxCostUsdPerDay": 5.0})

        member = PostgresStore(DSN, ws["id"])
        member.ensure()
        try:
            assert O.freshness(member)["state"] == "none"
            S._last_reconcile.clear()
            S.supervise_workspaces(
                admin_dsn=DSN, driver=LocalDriver(), workspaces=[ws["id"]],
                project_root=tmp_path,
                policy=S.SupervisorPolicy(reconcile_orgs_seconds=1.0))
            assert O.freshness(member)["state"] == "fresh"
        finally:
            member.close()
    finally:
        S._last_reconcile.clear()
        tenancy.close()
