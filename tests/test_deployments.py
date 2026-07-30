"""Versions, environments, promote, rollback, retire — PLATFORM_DESIGN D11/D12/§9.

The pipeline's contracts, stated as tests:

* a version is content (deploy twice, get one version),
* an environment is a pointer (promote flips it, rollback flips it back),
* retirement fails closed while a run is pinned (§6).
"""

from pathlib import Path

import pytest
import yaml

from rya.bundles import build_bundle
from rya.cli import scaffold
from rya.deployments import (
    create_version,
    current_version,
    describe_environment,
    history,
    list_environments,
    list_versions,
    pinned_runs,
    promote,
    resolve_for_run,
    retire,
    rollback,
)
from rya.errors import RyaError
from rya.store import Store


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "state")
    store.ensure()
    return store


def _project(tmp_path: Path, name: str) -> Path:
    """A real project in its OWN directory — nesting one project inside another
    would put the inner one's files in the outer one's bundle."""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    scaffold.write_project(root, name, template="demo")
    return root


def _run(store, agent: str, version_id: str, status: str = "running") -> dict:
    """A run pinned the way runtime/engine.py must stamp it (D12)."""
    run = {
        "id": store.new_run_id(),
        "agent": agent,
        "versionId": version_id,
        "status": status,
        "createdAt": "2026-07-29T00:00:00Z",
    }
    store.save_run(run)
    return run


# --------------------------------------------------------------------------- #
# versions are content
# --------------------------------------------------------------------------- #
def test_identical_content_is_one_version(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path, "demo")
    first = create_version(store, agent="demo", bundle=build_bundle(root))
    second = create_version(store, agent="demo", bundle=build_bundle(root))
    # `rya deploy` must be safe to retry: idempotent on (agent, bundleHash).
    assert first["id"] == second["id"]
    assert len(list_versions(store, agent="demo")) == 1
    assert first["state"] == "active"
    assert first["bundleHash"] == build_bundle(root).hash


def test_changing_a_file_makes_a_second_version(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path, "demo")
    first = create_version(store, agent="demo", bundle=build_bundle(root))
    (root / "src" / "tools.py").write_text("# changed\n")
    second = create_version(store, agent="demo", bundle=build_bundle(root))
    assert second["id"] != first["id"]
    assert second["bundleHash"] != first["bundleHash"]
    assert len(list_versions(store, agent="demo")) == 2


def test_version_record_carries_provenance(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path, "demo")
    bundle = build_bundle(root)
    version = create_version(
        store, agent="demo", bundle=bundle, actor="ci@example.com",
        metadata={"gitSha": "abc123"},
    )
    assert version["sdkVersion"] == bundle.sdkVersion
    assert version["entrypoint"] == "src/agent.py"
    assert version["manifestVersion"] == "0.1.0"  # a LABEL, not the identity
    assert version["fileCount"] == bundle.fileCount
    assert version["createdBy"] == "ci@example.com"
    assert version["metadata"]["gitSha"] == "abc123"
    # The per-file digest map stays in the archive, not on a record read per run.
    assert "files" not in version


def test_version_agent_must_match_the_manifest(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path, "demo")
    with pytest.raises(RyaError) as exc:
        create_version(store, agent="someone-else", bundle=build_bundle(root))
    assert exc.value.code == "E_BUNDLE_MISMATCH"


# --------------------------------------------------------------------------- #
# environments are pointers
# --------------------------------------------------------------------------- #
def test_promote_flips_the_pointer(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path, "demo")
    v1 = create_version(store, agent="demo", bundle=build_bundle(root))
    assert current_version(store, "prod", "demo") is None

    env = promote(store, environment="prod", agent="demo", version_id=v1["id"], actor="ops")
    assert env["currentVersionId"] == v1["id"]
    assert current_version(store, "prod", "demo")["id"] == v1["id"]
    assert resolve_for_run(store, "prod", "demo")["bundleHash"] == v1["bundleHash"]
    assert [e["name"] for e in list_environments(store, agent="demo")] == ["prod"]


def test_deploy_and_promote_in_one_step(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path, "demo")
    v = create_version(store, agent="demo", bundle=build_bundle(root), environment="staging")
    assert current_version(store, "staging", "demo")["id"] == v["id"]


def test_environments_are_independent(tmp_path):
    # D11: one environment-invariant manifest; the SAME artifact is promoted
    # between environments, so staging and prod are two pointers, not two builds.
    store = _store(tmp_path)
    root = _project(tmp_path, "demo")
    v1 = create_version(store, agent="demo", bundle=build_bundle(root))
    (root / "src" / "tools.py").write_text("# v2\n")
    v2 = create_version(store, agent="demo", bundle=build_bundle(root))

    promote(store, environment="prod", agent="demo", version_id=v1["id"])
    promote(store, environment="staging", agent="demo", version_id=v2["id"])
    assert current_version(store, "prod", "demo")["id"] == v1["id"]
    assert current_version(store, "staging", "demo")["id"] == v2["id"]


def test_rollback_restores_the_previous_pointer_and_history_shows_both(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path, "demo")
    v1 = create_version(store, agent="demo", bundle=build_bundle(root))
    (root / "src" / "tools.py").write_text("# v2\n")
    v2 = create_version(store, agent="demo", bundle=build_bundle(root))

    promote(store, environment="prod", agent="demo", version_id=v1["id"], actor="ops")
    promote(store, environment="prod", agent="demo", version_id=v2["id"], actor="ops")
    assert current_version(store, "prod", "demo")["id"] == v2["id"]

    # §9: "Rollback is a pointer flip."
    env = rollback(store, environment="prod", agent="demo", actor="oncall")
    assert env["currentVersionId"] == v1["id"]

    entries = history(store, "prod", "demo")
    assert entries[0]["versionId"] == v1["id"] and entries[0]["current"] is True
    assert [e["versionId"] for e in entries] == [v1["id"], v2["id"], v1["id"]]
    assert entries[0]["actor"] == "oncall"
    assert entries[0]["bundleHash"] == v1["bundleHash"]


def test_rollback_to_an_explicit_version(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path, "demo")
    ids = []
    for i in range(3):
        (root / "src" / "tools.py").write_text(f"# v{i}\n")
        v = create_version(store, agent="demo", bundle=build_bundle(root))
        ids.append(v["id"])
        promote(store, environment="prod", agent="demo", version_id=v["id"])
    env = rollback(store, environment="prod", agent="demo", to_version_id=ids[0])
    assert env["currentVersionId"] == ids[0]


def test_rollback_with_no_history(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path, "demo")
    v1 = create_version(store, agent="demo", bundle=build_bundle(root), environment="prod")
    with pytest.raises(RyaError) as exc:
        rollback(store, environment="prod", agent="demo")
    assert exc.value.code == "E_VERSION_NOT_FOUND"
    assert v1["id"] in exc.value.message


def test_rollback_on_an_unknown_environment(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(RyaError) as exc:
        rollback(store, environment="prod", agent="demo")
    assert exc.value.code == "E_ENVIRONMENT_NOT_FOUND"


# --------------------------------------------------------------------------- #
# promotion refusals — all fail closed
# --------------------------------------------------------------------------- #
def test_promote_unknown_version(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(RyaError) as exc:
        promote(store, environment="prod", agent="demo", version_id="ver_missing")
    assert exc.value.code == "E_VERSION_NOT_FOUND"
    assert exc.value.hint


def test_promote_retired_version(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path, "demo")
    v1 = create_version(store, agent="demo", bundle=build_bundle(root))
    retire(store, v1["id"])
    with pytest.raises(RyaError) as exc:
        promote(store, environment="prod", agent="demo", version_id=v1["id"])
    assert exc.value.code == "E_VERSION_RETIRED"


def test_promote_another_agents_version(tmp_path):
    store = _store(tmp_path)
    other = _project(tmp_path, "other-agent")
    v = create_version(store, agent="other-agent", bundle=build_bundle(other))
    with pytest.raises(RyaError) as exc:
        promote(store, environment="prod", agent="demo", version_id=v["id"])
    # Not NOT_FOUND: the id exists, the agent boundary was crossed.
    assert exc.value.code == "E_BUNDLE_MISMATCH"
    assert "other-agent" in exc.value.message


def test_promote_requires_an_environment_name(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path, "demo")
    v = create_version(store, agent="demo", bundle=build_bundle(root))
    with pytest.raises(RyaError) as exc:
        promote(store, environment="  ", agent="demo", version_id=v["id"])
    assert exc.value.code == "E_VALIDATION"


def test_resolve_for_run_without_a_promotion(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(RyaError) as exc:
        resolve_for_run(store, "prod", "demo")
    assert exc.value.code == "E_ENVIRONMENT_NOT_FOUND"
    assert "rya deploy" in exc.value.hint


def test_resolve_for_run_refuses_a_retired_pointer(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path, "demo")
    v = create_version(store, agent="demo", bundle=build_bundle(root), environment="prod")
    # Force the state the retire guard normally prevents (a pointer at a retired
    # version): a NEW run must not start on code we stopped retaining.
    retire(store, v["id"], force=True)
    with pytest.raises(RyaError) as exc:
        resolve_for_run(store, "prod", "demo")
    assert exc.value.code == "E_VERSION_RETIRED"


def test_dangling_pointer_fails_closed(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path, "demo")
    v = create_version(store, agent="demo", bundle=build_bundle(root), environment="prod")
    (store.versions_dir / f"{v['id']}.json").unlink()  # simulate a deleted version
    with pytest.raises(RyaError) as exc:
        current_version(store, "prod", "demo")
    assert exc.value.code == "E_VERSION_NOT_FOUND"


# --------------------------------------------------------------------------- #
# retirement (§6 "Version retirement", D12)
# --------------------------------------------------------------------------- #
def test_retire_is_refused_while_a_run_is_pinned(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path, "demo")
    v = create_version(store, agent="demo", bundle=build_bundle(root))
    run = _run(store, "demo", v["id"], status="running")

    assert [r["id"] for r in pinned_runs(store, v["id"])] == [run["id"]]
    with pytest.raises(RyaError) as exc:
        retire(store, v["id"])
    assert exc.value.code == "E_VERSION_IN_USE"
    assert run["id"] in exc.value.message
    assert store.version_get(v["id"])["state"] == "active"

    # ... and allowed once that run reaches a terminal status.
    run["status"] = "completed"
    store.save_run(run)
    assert pinned_runs(store, v["id"]) == []
    assert retire(store, v["id"])["state"] == "retired"


@pytest.mark.parametrize("status", ["completed", "failed", "rejected", "needs_reconnect"])
def test_terminal_runs_do_not_pin(tmp_path, status):
    store = _store(tmp_path)
    root = _project(tmp_path, "demo")
    v = create_version(store, agent="demo", bundle=build_bundle(root))
    _run(store, "demo", v["id"], status=status)
    assert pinned_runs(store, v["id"]) == []


def test_paused_run_still_pins(tmp_path):
    # A run waiting on an approval is exactly the case D12 exists for: it will
    # resume by REPLAYING its journal against the code that wrote it.
    store = _store(tmp_path)
    root = _project(tmp_path, "demo")
    v = create_version(store, agent="demo", bundle=build_bundle(root))
    _run(store, "demo", v["id"], status="paused")
    with pytest.raises(RyaError) as exc:
        retire(store, v["id"])
    assert exc.value.code == "E_VERSION_IN_USE"


def test_unpinned_runs_are_ignored(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path, "demo")
    v = create_version(store, agent="demo", bundle=build_bundle(root))
    store.save_run({"id": store.new_run_id(), "agent": "demo", "status": "running",
                    "createdAt": "2026-07-29T00:00:00Z"})  # pre-D12: no versionId
    assert pinned_runs(store, v["id"]) == []
    assert retire(store, v["id"])["state"] == "retired"


def test_retire_is_refused_for_a_current_pointer(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path, "demo")
    v = create_version(store, agent="demo", bundle=build_bundle(root), environment="prod")
    with pytest.raises(RyaError) as exc:
        retire(store, v["id"])
    assert exc.value.code == "E_VERSION_IN_USE"
    assert "prod" in exc.value.message


def test_retire_force_overrides(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path, "demo")
    v = create_version(store, agent="demo", bundle=build_bundle(root))
    _run(store, "demo", v["id"], status="running")
    assert retire(store, v["id"], force=True)["state"] == "retired"


def test_retire_is_idempotent_and_unknown_version_fails(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path, "demo")
    v = create_version(store, agent="demo", bundle=build_bundle(root))
    assert retire(store, v["id"])["state"] == "retired"
    assert retire(store, v["id"])["state"] == "retired"
    assert list_versions(store, agent="demo", state="active") == []
    with pytest.raises(RyaError) as exc:
        retire(store, "ver_nope")
    assert exc.value.code == "E_VERSION_NOT_FOUND"


# --------------------------------------------------------------------------- #
# operator views
# --------------------------------------------------------------------------- #
def test_describe_environment_reports_drain_state(tmp_path):
    store = _store(tmp_path)
    root = _project(tmp_path, "demo")
    v1 = create_version(store, agent="demo", bundle=build_bundle(root), environment="prod")
    _run(store, "demo", v1["id"], status="running")
    (root / "src" / "tools.py").write_text("# v2\n")
    v2 = create_version(store, agent="demo", bundle=build_bundle(root), environment="prod")

    info = describe_environment(store, "prod", "demo")
    assert info["currentVersionId"] == v2["id"]
    assert info["historyDepth"] == 1
    # §9: in-flight runs finish on THEIR version, so v1 is still draining.
    assert info["pinnedRuns"] == {v1["id"]: 1}


def test_history_on_an_unknown_environment(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(RyaError) as exc:
        history(store, "prod", "demo")
    assert exc.value.code == "E_ENVIRONMENT_NOT_FOUND"


def test_manifest_environment_field_is_gone(tmp_path):
    """D11: `environment:` is deleted from the manifest. A fresh scaffold has no
    such field, and a LEGACY manifest that still declares one is ignored (with a
    warning) rather than silently believed — the failure the decision names is a
    production container declaring itself `local` and nothing noticing."""
    from rya.manifest import load_manifest

    store = _store(tmp_path)
    root = _project(tmp_path, "demo")
    p = root / "rya.agent.yaml"
    assert "environment" not in yaml.safe_load(p.read_text())

    doc = yaml.safe_load(p.read_text())
    doc["environment"] = "local"
    p.write_text(yaml.safe_dump(doc))
    manifest = load_manifest(p)
    assert not hasattr(manifest, "environment")

    # ...and the bundle it describes promotes into prod unchanged.
    v = create_version(store, agent="demo", bundle=build_bundle(root), environment="prod")
    assert current_version(store, "prod", "demo")["id"] == v["id"]
    assert "environment" not in v
