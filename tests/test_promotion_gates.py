"""Promotion gates — PLATFORM_DESIGN §9's server-side admission check.

The claims under test, in order of how much they matter:

* an unconfigured platform is ungated (gates are opt-in, not a breaking change),
* a gated environment refuses a version with no evidence,
* evidence is bound to CONTENT — attesting one tree cannot admit another,
* an empty eval suite does not satisfy an eval gate (no vacuous pass),
* **rollback is never gated**, so a gate cannot hold an outage open,
* an override is recorded rather than silent,
* a malformed or mistyped gate fails closed instead of silently enforcing nothing.
"""

from pathlib import Path

import pytest

from rya.bundles import build_bundle
from rya.cli import scaffold
from rya.deployments import create_version, promote, rollback
from rya.errors import RyaError
from rya.gates import (
    ATTEST_OVERRIDE,
    POLICY_KEY,
    attest_evals,
    attest_readiness,
    attestations,
    check_promotion,
    require_promotion,
    resolve_gate,
    set_gate,
)
from rya.store import Store

READY = {"ready": True, "blocks": [], "warnings": [],
         "summary": {"blocks": 0, "warnings": 0}}
NOT_READY = {"ready": False,
             "blocks": [{"code": "E_TOOL_NO_IMPL", "message": "x", "fix": "y"}],
             "warnings": [], "summary": {"blocks": 1, "warnings": 0}}
WARNED = {"ready": True, "blocks": [],
          "warnings": [{"code": "W_STORE_FILE", "message": "x", "fix": "y"}],
          "summary": {"blocks": 0, "warnings": 1}}

EVALS_PASS = {"ok": True, "total": 3, "passed": 3, "failed": 0, "score": 1.0,
              "hasEvals": True, "results": [{"id": "a", "pass": True}]}
EVALS_FAIL = {"ok": False, "total": 3, "passed": 1, "failed": 2, "score": 0.333,
              "hasEvals": True,
              "results": [{"id": "a", "pass": True}, {"id": "b", "pass": False}]}
EVALS_EMPTY = {"ok": True, "total": 0, "passed": 0, "failed": 0, "score": None,
               "hasEvals": False, "results": []}


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "state")
    store.ensure()
    return store


def _version(tmp_path: Path, store: Store, name: str = "gated", **kw) -> dict:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    scaffold.write_project(root, name, template="demo")
    bundle = build_bundle(root)
    return create_version(store, agent=name, bundle=bundle, **kw)


# --------------------------------------------------------------------------- #
# gates are opt-in
# --------------------------------------------------------------------------- #
def test_an_unconfigured_platform_is_ungated(tmp_path):
    """Promotion gates must not change behaviour for anyone who has not asked for
    them: a platform with no gate policy promotes exactly as it did before."""
    store = _store(tmp_path)
    version = _version(tmp_path, store)
    gate = resolve_gate(store, "prod")
    assert gate.enforced is False

    env = promote(store, environment="prod", agent="gated", version_id=version["id"])
    assert env["currentVersionId"] == version["id"]


def test_gate_requiring_readiness_refuses_a_version_with_no_evidence(tmp_path):
    store = _store(tmp_path)
    version = _version(tmp_path, store)
    set_gate(store, {"environments": {"prod": {"requireReadiness": True}}}, actor="ops")

    with pytest.raises(RyaError) as e:
        promote(store, environment="prod", agent="gated", version_id=version["id"])
    assert e.value.code == "E_PROMOTION_BLOCKED"
    assert "readiness" in e.value.message
    # The refusal has to be actionable, not just a denial.
    assert e.value.hint and "attest" in e.value.hint.lower()


def test_a_passing_readiness_attestation_admits_the_version(tmp_path):
    store = _store(tmp_path)
    version = _version(tmp_path, store)
    set_gate(store, {"environments": {"prod": {"requireReadiness": True}}})
    attest_readiness(store, version, READY, actor="ci")

    env = promote(store, environment="prod", agent="gated", version_id=version["id"])
    assert env["currentVersionId"] == version["id"]


def test_a_failing_readiness_attestation_does_not_admit_it(tmp_path):
    store = _store(tmp_path)
    version = _version(tmp_path, store)
    set_gate(store, {"environments": {"prod": {"requireReadiness": True}}})
    attest_readiness(store, version, NOT_READY, actor="ci")

    with pytest.raises(RyaError) as e:
        promote(store, environment="prod", agent="gated", version_id=version["id"])
    assert e.value.code == "E_PROMOTION_BLOCKED"
    # Names the actual blocker so a coding agent knows what to fix.
    assert "E_TOOL_NO_IMPL" in e.value.message


def test_re_attesting_after_a_fix_unblocks_the_promotion(tmp_path):
    """Storage is append-only, but the gate reads latest-wins — otherwise a
    version could never recover from one failed check."""
    store = _store(tmp_path)
    version = _version(tmp_path, store)
    set_gate(store, {"environments": {"prod": {"requireReadiness": True}}})
    attest_readiness(store, version, NOT_READY)
    attest_readiness(store, version, READY)

    promote(store, environment="prod", agent="gated", version_id=version["id"])
    # The failure is still on the record: "passed on the second attempt" stays
    # auditable.
    kinds = [(a["kind"], a["ok"]) for a in attestations(store, version["id"])]
    assert kinds == [("readiness", False), ("readiness", True)]


def test_allow_warnings_false_blocks_a_ready_but_warned_version(tmp_path):
    store = _store(tmp_path)
    version = _version(tmp_path, store)
    set_gate(store, {"environments": {"prod": {"requireReadiness": True,
                                               "allowWarnings": False}}})
    attest_readiness(store, version, WARNED)

    with pytest.raises(RyaError) as e:
        promote(store, environment="prod", agent="gated", version_id=version["id"])
    assert "W_STORE_FILE" in e.value.message


# --------------------------------------------------------------------------- #
# evidence is bound to content
# --------------------------------------------------------------------------- #
def test_attesting_one_version_does_not_admit_another(tmp_path):
    """The property that makes a gate a control rather than a ritual: evidence is
    filed against a version id, and a version id is 1:1 with a bundle hash, so
    running the checks against a different tree proves nothing about this one."""
    store = _store(tmp_path)
    checked = _version(tmp_path, store, name="checked")
    other = _version(tmp_path, store, name="other")
    set_gate(store, {"environments": {"prod": {"requireReadiness": True}}})
    attest_readiness(store, checked, READY)

    # `other` is a different agent AND different content; it has no evidence.
    with pytest.raises(RyaError) as e:
        promote(store, environment="prod", agent="other", version_id=other["id"])
    assert e.value.code == "E_PROMOTION_BLOCKED"


def test_an_attestation_for_a_different_bundle_hash_is_refused(tmp_path):
    """Defence in depth against out-of-band edits to the records: the attestation
    carries the hash it was made against, and a mismatch fails closed."""
    store = _store(tmp_path)
    version = _version(tmp_path, store)
    set_gate(store, {"environments": {"prod": {"requireReadiness": True}}})
    store.version_attest(version["id"], {
        "kind": "readiness", "ok": True, "bundleHash": "deadbeef" * 8})

    with pytest.raises(RyaError) as e:
        promote(store, environment="prod", agent="gated", version_id=version["id"])
    assert e.value.code == "E_PROMOTION_BLOCKED"
    assert "deadbeef" in e.value.message


# --------------------------------------------------------------------------- #
# evals
# --------------------------------------------------------------------------- #
def test_eval_gate_admits_a_passing_suite(tmp_path):
    store = _store(tmp_path)
    version = _version(tmp_path, store)
    set_gate(store, {"environments": {"prod": {"requireEvals": True}}})
    attest_evals(store, version, EVALS_PASS, actor="ci")

    promote(store, environment="prod", agent="gated", version_id=version["id"])


def test_eval_gate_refuses_a_failing_suite_and_names_the_cases(tmp_path):
    store = _store(tmp_path)
    version = _version(tmp_path, store)
    set_gate(store, {"environments": {"prod": {"requireEvals": True}}})
    attest_evals(store, version, EVALS_FAIL)

    with pytest.raises(RyaError) as e:
        promote(store, environment="prod", agent="gated", version_id=version["id"])
    assert "b" in e.value.message and "2/3" in e.value.message


def test_an_empty_eval_suite_does_not_satisfy_an_eval_gate(tmp_path):
    """A project with no cases scores vacuously — zero failures out of zero. A
    gate that accepted that would be a checkbox rather than a control."""
    store = _store(tmp_path)
    version = _version(tmp_path, store)
    set_gate(store, {"environments": {"prod": {"requireEvals": True}}})
    attest_evals(store, version, EVALS_EMPTY)

    with pytest.raises(RyaError) as e:
        promote(store, environment="prod", agent="gated", version_id=version["id"])
    assert "no cases" in e.value.message


def test_min_eval_score_is_enforced(tmp_path):
    store = _store(tmp_path)
    version = _version(tmp_path, store)
    set_gate(store, {"environments": {"prod": {"requireEvals": True, "minEvalScore": 0.9}}})
    # Every case passed, but the recorded score is below the bar.
    attest_evals(store, version, {**EVALS_PASS, "score": 0.8})

    with pytest.raises(RyaError) as e:
        promote(store, environment="prod", agent="gated", version_id=version["id"])
    assert "0.8" in e.value.message and "0.9" in e.value.message


# --------------------------------------------------------------------------- #
# per-environment scoping and merging
# --------------------------------------------------------------------------- #
def test_a_gate_on_prod_does_not_gate_staging(tmp_path):
    """§9 gates promotion "between staging and prod" — staging is where the
    evidence gets produced, so gating it identically would deadlock."""
    store = _store(tmp_path)
    version = _version(tmp_path, store)
    set_gate(store, {"environments": {"prod": {"requireEvals": True}}})

    promote(store, environment="staging", agent="gated", version_id=version["id"])
    with pytest.raises(RyaError):
        promote(store, environment="prod", agent="gated", version_id=version["id"])


def test_environment_requirements_merge_over_the_default(tmp_path):
    store = _store(tmp_path)
    version = _version(tmp_path, store)
    set_gate(store, {"default": {"requireReadiness": True},
                     "environments": {"prod": {"requireEvals": True}}})

    prod = resolve_gate(store, "prod")
    assert prod.require_readiness is True and prod.require_evals is True
    staging = resolve_gate(store, "staging")
    assert staging.require_readiness is True and staging.require_evals is False


def test_provenance_requirement_reads_version_metadata(tmp_path):
    store = _store(tmp_path)
    bare = _version(tmp_path, store, name="bare")
    set_gate(store, {"environments": {"prod": {"requireProvenance": ["gitSha"]}}})

    with pytest.raises(RyaError) as e:
        promote(store, environment="prod", agent="bare", version_id=bare["id"])
    assert "gitSha" in e.value.message

    stamped = _version(tmp_path, store, name="stamped", metadata={"gitSha": "abc123"})
    promote(store, environment="prod", agent="stamped", version_id=stamped["id"])


def test_require_actor_blocks_an_anonymous_promotion(tmp_path):
    store = _store(tmp_path)
    version = _version(tmp_path, store)
    set_gate(store, {"environments": {"prod": {"requireActor": True}}})

    with pytest.raises(RyaError):
        promote(store, environment="prod", agent="gated", version_id=version["id"])
    promote(store, environment="prod", agent="gated", version_id=version["id"], actor="ada")


# --------------------------------------------------------------------------- #
# rollback is never gated
# --------------------------------------------------------------------------- #
def test_rollback_is_never_gated(tmp_path):
    """The load-bearing asymmetry. A gate stops unvetted code going FORWARD; if it
    also blocked a rollback, a missing eval attestation would hold an outage open
    — strictly worse than what the gate prevents. Enforced by construction, not by
    an operator remembering a flag under pressure.
    """
    store = _store(tmp_path)
    root = tmp_path / "app"
    root.mkdir()
    scaffold.write_project(root, "app", template="demo")
    v1 = create_version(store, agent="app", bundle=build_bundle(root))
    (root / "src" / "extra.py").write_text("# second version\n")
    v2 = create_version(store, agent="app", bundle=build_bundle(root))
    assert v1["id"] != v2["id"]

    # Both promoted while ungated, so prod has history to roll back through.
    promote(store, environment="prod", agent="app", version_id=v1["id"])
    promote(store, environment="prod", agent="app", version_id=v2["id"])

    # Now the gate turns on and NOTHING has evidence — the incident case.
    set_gate(store, {"environments": {"prod": {"requireReadiness": True,
                                               "requireEvals": True}}})
    with pytest.raises(RyaError):  # forward is blocked
        promote(store, environment="prod", agent="app", version_id=v2["id"])

    env = rollback(store, environment="prod", agent="app", actor="oncall")
    assert env["currentVersionId"] == v1["id"]


# --------------------------------------------------------------------------- #
# overrides are recorded
# --------------------------------------------------------------------------- #
def test_force_overrides_the_gate_and_records_it(tmp_path):
    """An override that left no trace would turn the gate into decoration."""
    store = _store(tmp_path)
    version = _version(tmp_path, store)
    set_gate(store, {"environments": {"prod": {"requireReadiness": True,
                                               "requireEvals": True}}})

    env = promote(store, environment="prod", agent="gated", version_id=version["id"],
                  actor="ada", force=True)
    assert env["currentVersionId"] == version["id"]

    overrides = [a for a in attestations(store, version["id"]) if a["kind"] == ATTEST_OVERRIDE]
    assert len(overrides) == 1
    assert overrides[0]["actor"] == "ada"
    assert overrides[0]["environment"] == "prod"
    assert sorted(overrides[0]["bypassed"]) == ["evals", "readiness"]


def test_an_override_is_not_needed_when_the_gate_passes(tmp_path):
    """force=True on a satisfied gate must not litter the record with a bypass
    that never happened."""
    store = _store(tmp_path)
    version = _version(tmp_path, store)
    set_gate(store, {"environments": {"prod": {"requireReadiness": True}}})
    attest_readiness(store, version, READY)

    promote(store, environment="prod", agent="gated", version_id=version["id"], force=True)
    assert not [a for a in attestations(store, version["id"]) if a["kind"] == ATTEST_OVERRIDE]


# --------------------------------------------------------------------------- #
# malformed policy fails closed
# --------------------------------------------------------------------------- #
def test_a_mistyped_requirement_is_refused_on_write(tmp_path):
    """`requireEval` (singular) silently resolving to an unenforced gate would be
    a governance control that reports itself as configured while enforcing
    nothing. Rejected where the operator can see it."""
    store = _store(tmp_path)
    with pytest.raises(RyaError) as e:
        set_gate(store, {"environments": {"prod": {"requireEval": True}}})
    assert e.value.code == "E_VALIDATION"
    assert "requireEval" in e.value.message


def test_an_unparseable_gate_policy_fails_closed(tmp_path):
    """Privileged state we cannot read is not "no requirements" — the same
    fail-closed posture guard.py takes on an unreadable policy."""
    store = _store(tmp_path)
    version = _version(tmp_path, store)
    store.policy_set(POLICY_KEY, {"environments": {"prod": ["not", "an", "object"]}})

    with pytest.raises(RyaError) as e:
        promote(store, environment="prod", agent="gated", version_id=version["id"])
    assert e.value.code == "E_PROMOTION_BLOCKED"


def test_min_eval_score_out_of_range_is_refused(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(RyaError):
        set_gate(store, {"environments": {"prod": {"minEvalScore": 5}}})


def test_gate_changes_land_in_the_policy_audit_log(tmp_path):
    """§12 risk 7: for a governance product, "who tightened this gate" is a
    feature, not a residual."""
    store = _store(tmp_path)
    set_gate(store, {"environments": {"prod": {"requireReadiness": True}}}, actor="ada")
    set_gate(store, {"environments": {"prod": {"requireEvals": True}}}, actor="grace")

    log = store.policy_history(POLICY_KEY)
    assert [r["actor"] for r in log[:2]] == ["grace", "ada"]


# --------------------------------------------------------------------------- #
# the dry-run surface
# --------------------------------------------------------------------------- #
def test_check_promotion_reports_every_check_not_just_failures(tmp_path):
    """"readiness and evals were both required and both satisfied by these
    attestations" is the audit record that makes a gate worth having."""
    store = _store(tmp_path)
    version = _version(tmp_path, store)
    set_gate(store, {"environments": {"prod": {"requireReadiness": True,
                                               "requireEvals": True}}})
    attest_readiness(store, version, READY)
    attest_evals(store, version, EVALS_FAIL)

    result = check_promotion(store, version=version, environment="prod")
    assert result.allowed is False
    assert {c["check"] for c in result.checks} == {"readiness", "evals"}
    assert [c["check"] for c in result.blocked] == ["evals"]
    # Every failure carries a fix.
    assert all(c["fix"] for c in result.blocked)


def test_require_promotion_returns_quietly_on_an_unenforced_gate(tmp_path):
    store = _store(tmp_path)
    version = _version(tmp_path, store)
    result = require_promotion(store, version=version, environment="dev")
    assert result.gate.enforced is False and result.checks == []


# --------------------------------------------------------------------------- #
# the CLI surface — a coding agent drives all of this over --json
# --------------------------------------------------------------------------- #
def _cli(args, cwd):
    import os

    from typer.testing import CliRunner

    from rya.cli.main import app

    old = os.getcwd()
    os.chdir(cwd)
    try:
        return CliRunner().invoke(app, args)
    finally:
        os.chdir(old)


def test_cli_gate_set_show_and_check_round_trip(project):
    import json as jsonlib

    res = _cli(["gate", "set", "--env", "prod", "--require-readiness",
                "--actor", "ada", "--json"], project)
    assert res.exit_code == 0, res.output
    assert jsonlib.loads(res.stdout)["gate"]["requireReadiness"] is True

    shown = _cli(["gate", "show", "--env", "prod", "--json"], project)
    assert jsonlib.loads(shown.stdout)["gates"][0]["enforced"] is True

    # Deploying without promoting records the version AND attests readiness, so
    # the gate is satisfiable in one step from a clean tree.
    dep = _cli(["deploy", "--env", "prod", "--no-promote", "--json"], project)
    assert dep.exit_code == 0, res.output
    version_id = jsonlib.loads(dep.stdout)["versionId"]

    checked = _cli(["gate", "check", "--env", "prod", "--version", version_id, "--json"], project)
    assert checked.exit_code == 0, checked.output
    assert jsonlib.loads(checked.stdout)["allowed"] is True

    promoted = _cli(["promote", "--env", "prod", "--version", version_id, "--json"], project)
    assert promoted.exit_code == 0, promoted.output


def test_cli_gate_check_exits_seven_when_blocked(project):
    """CI gates on the exit code, not on prose."""
    import json as jsonlib

    _cli(["gate", "set", "--env", "prod", "--require-evals", "--json"], project)
    dep = _cli(["deploy", "--env", "prod", "--no-promote", "--json"], project)
    version_id = jsonlib.loads(dep.stdout)["versionId"]

    checked = _cli(["gate", "check", "--env", "prod", "--version", version_id, "--json"], project)
    assert checked.exit_code == 7
    body = jsonlib.loads(checked.stdout)
    assert body["allowed"] is False
    assert [c["check"] for c in body["checks"]] == ["evals"]


def test_cli_eval_attest_satisfies_an_eval_gate(project):
    """`rya eval --attest` is the staging→prod handoff §9 describes: the evidence
    produced in one place is what admits the version in another."""
    import json as jsonlib

    _cli(["gate", "set", "--env", "prod", "--require-evals", "--json"], project)
    dep = _cli(["deploy", "--env", "prod", "--no-promote", "--json"], project)
    version_id = jsonlib.loads(dep.stdout)["versionId"]

    blocked = _cli(["promote", "--env", "prod", "--version", version_id, "--json"], project)
    assert blocked.exit_code == 7  # EXIT_VALIDATION, from E_PROMOTION_BLOCKED
    assert jsonlib.loads(blocked.stdout)["error"]["code"] == "E_PROMOTION_BLOCKED"

    ev = _cli(["eval", "--attest", "--version", version_id, "--actor", "ci", "--json"], project)
    assert ev.exit_code == 0, ev.output
    assert jsonlib.loads(ev.stdout)["attestation"]["kind"] == "evals"

    ok = _cli(["promote", "--env", "prod", "--version", version_id, "--json"], project)
    assert ok.exit_code == 0, ok.output


def test_cli_eval_attest_defaults_to_the_working_trees_version(project):
    """No version id copied by hand: the working tree hashes to exactly one
    recorded version (D12), so --attest can find it."""
    import json as jsonlib

    dep = _cli(["deploy", "--env", "dev", "--no-promote", "--json"], project)
    version_id = jsonlib.loads(dep.stdout)["versionId"]

    ev = _cli(["eval", "--attest", "--json"], project)
    assert ev.exit_code == 0, ev.output
    assert jsonlib.loads(ev.stdout)["attestation"]["versionId"] == version_id


def test_cli_eval_attest_refuses_when_the_tree_was_never_recorded(project):
    """Fail closed rather than attesting against a version that does not describe
    the code just evaluated."""
    import json as jsonlib

    ev = _cli(["eval", "--attest", "--json"], project)
    assert ev.exit_code == 4  # EXIT_NOT_FOUND
    assert jsonlib.loads(ev.stdout)["error"]["code"] == "E_VERSION_NOT_FOUND"


def test_cli_promote_force_records_the_override(project):
    import json as jsonlib

    _cli(["gate", "set", "--env", "prod", "--require-evals", "--json"], project)
    dep = _cli(["deploy", "--env", "prod", "--no-promote", "--json"], project)
    version_id = jsonlib.loads(dep.stdout)["versionId"]

    forced = _cli(["promote", "--env", "prod", "--version", version_id,
                   "--actor", "ada", "--force", "--json"], project)
    assert forced.exit_code == 0, forced.output

    from rya.store import Store as _S
    store = _S(project)
    kinds = [a["kind"] for a in attestations(store, version_id)]
    assert ATTEST_OVERRIDE in kinds
