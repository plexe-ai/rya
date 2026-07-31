"""Per-workspace quotas and fair claiming — PLATFORM_DESIGN §11.12, D13, §6.

D13 gives each tenant its own process; that stops one tenant reading another's
data and does nothing about one tenant eating the node. The claims under test:

* an unconfigured workspace is unlimited (quotas are opt-in),
* every limit refuses ADMISSION and never aborts work in flight,
* a batch enqueue is admitted or refused as a unit,
* a mistyped limit is refused rather than silently capping nothing,
* the least-busy concurrency key gets the next slot (§6's fairness primitive),
* ordering WITHIN a key still honours priority.
"""

from pathlib import Path

import pytest

from rya import queue as q
from rya.errors import RyaError
from rya.quotas import (
    POLICY_KEY,
    check_admission,
    require_admission,
    resolve_quota,
    set_quota,
    usage_snapshot,
)
from rya.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path)
    s.ensure()
    return s


def _run(store, status: str = "running", created: str = "2026-07-30T10:00:00Z") -> dict:
    run = {"id": store.new_run_id(), "agent": "a", "status": status, "createdAt": created}
    store.save_run(run)
    return run


# --------------------------------------------------------------------------- #
# opt-in
# --------------------------------------------------------------------------- #
def test_an_unconfigured_workspace_is_unlimited(store):
    policy = resolve_quota(store)
    assert policy.enforced is False
    assert check_admission(store, kind="run").allowed is True
    # And it costs nothing: with no limits there is nothing to count.
    assert check_admission(store, kind="run").usage.keys() <= {"since"}


def test_an_unenforced_quota_does_not_block_enqueue(store):
    job = q.enqueue(store, "t", {"n": 1})
    assert job["status"] == "pending"


# --------------------------------------------------------------------------- #
# the limits
# --------------------------------------------------------------------------- #
def test_concurrent_run_limit_counts_only_non_terminal_runs(store):
    set_quota(store, {"maxConcurrentRuns": 2})
    _run(store, "completed")
    _run(store, "completed")
    _run(store, "running")
    # Two finished runs do not occupy capacity.
    assert check_admission(store, kind="run").allowed is True

    _run(store, "waiting_approval")  # a paused run is still live
    verdict = check_admission(store, kind="run")
    assert verdict.allowed is False
    assert verdict.violations[0]["limit"] == "max_concurrent_runs"


def test_a_paused_run_still_occupies_concurrency(store):
    """`waiting_approval` can last for days (README: "durably, for days if
    needed"), so it must count — otherwise a workspace parks a thousand runs on
    approvals and the cap means nothing."""
    set_quota(store, {"maxConcurrentRuns": 1})
    _run(store, "waiting_approval")
    with pytest.raises(RyaError) as e:
        require_admission(store, kind="run")
    assert e.value.code == "E_QUOTA_EXCEEDED"


def test_queue_depth_limit_refuses_a_new_job(store):
    set_quota(store, {"maxQueueDepth": 2})
    q.enqueue(store, "t", {"n": 1})
    q.enqueue(store, "t", {"n": 2})
    with pytest.raises(RyaError) as e:
        q.enqueue(store, "t", {"n": 3})
    assert e.value.code == "E_QUOTA_EXCEEDED"
    assert "pending queue jobs 2/2" in e.value.message


def test_claiming_a_job_frees_queue_depth(store):
    """Depth counts PENDING work, so draining the queue restores capacity — the
    limit is backpressure, not a lifetime cap."""
    set_quota(store, {"maxQueueDepth": 1})
    q.enqueue(store, "t", {"n": 1})
    with pytest.raises(RyaError):
        q.enqueue(store, "t", {"n": 2})

    q.claim(store, "w1")
    assert q.enqueue(store, "t", {"n": 2})["status"] == "pending"


def test_an_idempotent_re_enqueue_is_never_refused(store):
    """A client retrying through a timeout must not be told it is over quota for
    work the platform already accepted."""
    set_quota(store, {"maxQueueDepth": 1})
    first = q.enqueue(store, "t", {"n": 1}, job_id="fixed")
    again = q.enqueue(store, "t", {"n": 1}, job_id="fixed")
    assert again["id"] == first["id"]


def test_a_batch_is_admitted_or_refused_as_a_unit(store):
    """One free slot must not admit five hundred jobs, and a batch accepted
    halfway is worse than one refused outright: the caller cannot tell where the
    fan-out stopped."""
    set_quota(store, {"maxQueueDepth": 3})
    with pytest.raises(RyaError) as e:
        q.enqueue_batch(store, "t", [{"payload": {"n": i}} for i in range(5)])
    assert e.value.code == "E_QUOTA_EXCEEDED"
    # Nothing was written.
    assert store.queue_counts().get("pending", 0) == 0

    jobs = q.enqueue_batch(store, "t", [{"payload": {"n": i}} for i in range(3)])
    assert len(jobs) == 3


def test_token_and_cost_limits_read_the_durable_meter(store):
    """Billing reads the append-only ledger (D10), so the quota reads the same
    facts — a token cap derived from `run["trace"]` would be derived from a
    debugging artifact that gets redacted and truncated."""
    set_quota(store, {"maxTokensPerDay": 100, "maxCostUsdPerDay": 1.0})
    store.meter_append({"runId": "r1", "inputTokens": 60, "outputTokens": 30,
                        "costUsd": 0.5, "model": "m"})
    assert check_admission(store, kind="run").allowed is True

    store.meter_append({"runId": "r2", "inputTokens": 10, "outputTokens": 5,
                        "costUsd": 0.2, "model": "m"})
    verdict = check_admission(store, kind="run")
    assert verdict.allowed is False
    assert [v["limit"] for v in verdict.violations] == ["max_tokens_per_day"]


def test_cost_limit_applies_to_jobs_too(store):
    """Money limits are the ones that apply to everything: the queue surface is
    SDK-free (D14) but it is not free of charge."""
    set_quota(store, {"maxCostUsdPerDay": 0.1})
    store.meter_append({"runId": "r1", "costUsd": 0.5})
    with pytest.raises(RyaError):
        q.enqueue(store, "t", {})


def test_a_run_limit_does_not_block_a_job_and_vice_versa(store):
    """Limits are scoped to what they measure: a deep queue is not a reason to
    refuse a run, and run concurrency is not a reason to refuse an enqueue."""
    set_quota(store, {"maxConcurrentRuns": 1, "maxQueueDepth": 1})
    _run(store, "running")
    q.enqueue(store, "t", {})

    assert check_admission(store, kind="job").violations[0]["limit"] == "max_queue_depth"
    assert check_admission(store, kind="run").violations[0]["limit"] == "max_concurrent_runs"


def test_daily_counters_are_scoped_to_today(store):
    set_quota(store, {"maxRunsPerDay": 1})
    _run(store, "completed", created="2020-01-01T00:00:00Z")  # last year
    assert check_admission(store, kind="run").allowed is True


def test_zero_means_admit_nothing(store):
    set_quota(store, {"maxConcurrentRuns": 0})
    with pytest.raises(RyaError):
        require_admission(store, kind="run")


def test_the_refusal_names_every_exhausted_limit_and_when_it_resets(store):
    """"Quota exceeded" alone tells a caller nothing about whether to retry in a
    second or to go buy capacity."""
    set_quota(store, {"maxRunsPerDay": 1, "maxTokensPerDay": 1})
    _run(store, "completed")
    store.meter_append({"runId": "r", "inputTokens": 5})
    with pytest.raises(RyaError) as e:
        require_admission(store, kind="run")
    assert "runs today 1/1" in e.value.message and "tokens today 5/1" in e.value.message
    assert "00:00 UTC" in (e.value.hint or "")


# --------------------------------------------------------------------------- #
# admission only — never mid-flight
# --------------------------------------------------------------------------- #
def test_an_exhausted_quota_does_not_disturb_a_run_already_started(project):
    """A run killed mid-journal could never replay to a terminal state. The quota
    bounds overshoot to one run rather than trading durability for billing."""
    from rya.manifest import load_manifest
    from rya.runtime import Engine, load_agent

    manifest = load_manifest(project / "rya.agent.yaml")
    agent = load_agent(manifest, project)
    store = Store(project)
    store.ensure()
    engine = Engine(manifest, agent, store, project)

    set_quota(store, {"maxConcurrentRuns": 1})
    run = engine.run_event("message.received", {"email": "ada@example.com"})
    assert run["status"] == "waiting_approval"  # paused, and still occupying the slot

    with pytest.raises(RyaError) as e:
        engine.run_event("message.received", {"email": "grace@example.com"})
    assert e.value.code == "E_QUOTA_EXCEEDED"

    # The in-flight run resolves normally: the quota refused a NEW run, it did not
    # break the existing one.
    approval = store.get_approval(run["pendingApproval"])
    resumed = engine.approve(approval["id"])
    assert resumed["status"] == "completed"


# --------------------------------------------------------------------------- #
# malformed policy
# --------------------------------------------------------------------------- #
def test_a_mistyped_limit_is_refused_on_write(store):
    with pytest.raises(RyaError) as e:
        set_quota(store, {"maxRunsPerdayy": 10})
    assert e.value.code == "E_VALIDATION" and "maxRunsPerdayy" in e.value.message


def test_a_negative_limit_is_refused(store):
    with pytest.raises(RyaError):
        set_quota(store, {"maxConcurrentRuns": -1})


def test_an_unparseable_quota_policy_fails_closed(store):
    """Privileged state we cannot read is not "unlimited"."""
    store.policy_set(POLICY_KEY, ["not", "an", "object"])
    with pytest.raises(RyaError) as e:
        resolve_quota(store)
    assert e.value.code == "E_QUOTA_EXCEEDED"


def test_quota_changes_are_audited(store):
    set_quota(store, {"maxConcurrentRuns": 5}, actor="ada")
    set_quota(store, {"maxConcurrentRuns": 50}, actor="grace")
    log = store.policy_history(POLICY_KEY)
    assert [(r["actor"], r["value"]["maxConcurrentRuns"]) for r in log[:2]] \
        == [("grace", 50), ("ada", 5)]


def test_usage_snapshot_reports_every_dimension(store):
    _run(store, "running")
    q.enqueue(store, "t", {})
    store.meter_append({"runId": "r", "inputTokens": 3, "outputTokens": 4, "costUsd": 0.25})
    snap = usage_snapshot(store)
    assert snap["concurrentRuns"] == 1
    assert snap["queueDepth"] == 1
    assert snap["tokensToday"] == 7
    assert snap["costUsdToday"] == 0.25


# --------------------------------------------------------------------------- #
# fairness (§6: "one workspace must not starve another")
# --------------------------------------------------------------------------- #
def test_the_least_busy_concurrency_key_gets_the_next_slot(store):
    """Without fair ordering, a workspace that enqueues a huge backlog owns every
    free slot until it drains: selection was purely (priority, runAt). Caps bound
    how much one key HOLDS; ordering decides who gets the NEXT slot."""
    # `noisy` enqueued first and has plenty of headroom under its own cap.
    for i in range(5):
        q.enqueue(store, "t", {"who": "noisy", "i": i},
                  concurrency_key="noisy", concurrency_limit=10)
    q.enqueue(store, "t", {"who": "quiet"}, concurrency_key="quiet", concurrency_limit=10)

    # One noisy job is already running, so noisy is the busier key.
    first = q.claim(store, "w1")[0]
    assert first["payload"]["who"] == "noisy"

    second = q.claim(store, "w2")[0]
    assert second["payload"]["who"] == "quiet", \
        "the idle key must not wait behind four queued jobs of a busy one"


def test_fairness_does_not_reorder_within_a_key(store):
    """Jobs sharing a key see the same running count, so priority still decides
    among them — fairness only arbitrates BETWEEN keys."""
    q.enqueue(store, "t", {"which": "low"}, priority=0, concurrency_key="k")
    q.enqueue(store, "t", {"which": "high"}, priority=10, concurrency_key="k")
    assert q.claim(store, "w1")[0]["payload"]["which"] == "high"


def test_keyless_jobs_keep_plain_priority_ordering(store):
    """Anyone not using concurrency keys sees exactly the previous behaviour."""
    q.enqueue(store, "t", {"which": "low"}, priority=0)
    q.enqueue(store, "t", {"which": "high"}, priority=10)
    assert q.claim(store, "w1")[0]["payload"]["which"] == "high"


def test_a_key_at_its_limit_is_skipped_entirely(store):
    """The cap half of the primitive, unchanged: fairness picks among keys that
    are still allowed to run."""
    q.enqueue(store, "t", {"who": "capped"}, concurrency_key="capped", concurrency_limit=1)
    q.enqueue(store, "t", {"who": "capped2"}, concurrency_key="capped", concurrency_limit=1)
    q.enqueue(store, "t", {"who": "other"}, concurrency_key="other", concurrency_limit=1)

    assert q.claim(store, "w1")[0]["payload"]["who"] == "capped"
    # `capped` is now at its limit of 1, so the only claimable job is `other`.
    assert q.claim(store, "w2")[0]["payload"]["who"] == "other"
    assert q.claim(store, "w3") == []


def test_round_robin_alternates_between_two_busy_keys(store):
    for i in range(3):
        q.enqueue(store, "t", {"who": "a", "i": i}, concurrency_key="a", concurrency_limit=10)
        q.enqueue(store, "t", {"who": "b", "i": i}, concurrency_key="b", concurrency_limit=10)

    picked = [q.claim(store, f"w{i}")[0]["payload"]["who"] for i in range(4)]
    # Strict alternation: after one 'a' runs, 'b' is the less busy key, and so on.
    assert picked == ["a", "b", "a", "b"]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_quotas_set_show_and_clear(project):
    import json as jsonlib
    import os

    from typer.testing import CliRunner

    from rya.cli.main import app as cli

    def run(args):
        old = os.getcwd()
        os.chdir(project)
        try:
            return CliRunner().invoke(cli, args)
        finally:
            os.chdir(old)

    res = run(["quotas", "set", "--max-concurrent-runs", "3",
               "--max-cost-usd-per-day", "2.5", "--actor", "ops", "--json"])
    assert res.exit_code == 0, res.output
    assert jsonlib.loads(res.stdout)["quota"]["maxConcurrentRuns"] == 3

    shown = run(["quotas", "show", "--json"])
    body = jsonlib.loads(shown.stdout)
    assert body["quota"]["maxCostUsdPerDay"] == 2.5
    assert body["usage"]["concurrentRuns"] == 0

    cleared = run(["quotas", "clear", "--json"])
    assert jsonlib.loads(cleared.stdout)["cleared"] is True
    assert run(["quotas", "show", "--json"]).exit_code == 0
