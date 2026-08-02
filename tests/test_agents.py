"""The agent registry — D21's replacement for `load_manifest` at boot.

`build_app` used to learn what it served by reading one `rya.agent.yaml`. These
tests pin the replacement: agents come from published versions and environment
pointers, their declarations come from the manifest persisted on the version
record (#8), and nothing here imports a handler.

The interesting cases are the degenerate ones — a version published before #8, an
environment pointing at a vanished version, a manifest a newer SDK wrote — because
each is a state a real upgrade produces and each has a different right answer.
"""

from pathlib import Path

import pytest

from rya import agents
from rya.bundles import build_bundle
from rya.cli import scaffold
from rya.deployments import create_version, promote
from rya.errors import RyaError
from rya.store import Store


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "state")
    store.ensure()
    return store


def _project(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    scaffold.write_project(root, name, template="demo")
    return root


def _publish(store, tmp_path: Path, name: str, *, environment: str | None = None) -> dict:
    bundle = build_bundle(_project(tmp_path, name))
    return create_version(store, agent=name, bundle=bundle, environment=environment)


def test_an_agent_exists_because_a_version_was_published(tmp_path):
    store = _store(tmp_path)
    assert agents.names(store) == []
    _publish(store, tmp_path, "billing")
    assert agents.names(store) == ["billing"]


def test_two_agents_published_from_two_projects_are_both_served(tmp_path):
    """The Phase 2 property, at the registry layer: one store, two agents, no
    manifest on disk deciding which one is real."""
    store = _store(tmp_path)
    _publish(store, tmp_path, "billing")
    _publish(store, tmp_path, "support")
    assert agents.names(store) == ["billing", "support"]
    assert {r.name for r in agents.list_refs(store)} == {"billing", "support"}


def test_the_declarations_come_from_the_version_record_not_a_file(tmp_path):
    store = _store(tmp_path)
    version = _publish(store, tmp_path, "billing")
    ref = agents.resolve(store, "billing")
    assert ref.source == agents.SOURCE_VERSION
    assert ref.manifest.name == "billing"
    assert (ref.declared_by or {})["id"] == version["id"]
    # The scaffolded demo declares tools; they reached the registry through the
    # database, which is the whole of D21.
    assert [t.id for t in ref.manifest.tools]


def test_the_promoted_version_is_the_one_resolved(tmp_path):
    store = _store(tmp_path)
    first = _publish(store, tmp_path, "billing")
    # A second, different bundle for the same agent.
    root = tmp_path / "billing"
    (root / "src" / "extra.py").write_text("X = 1\n")
    second = create_version(store, agent="billing", bundle=build_bundle(root))
    assert second["id"] != first["id"]

    promote(store, environment="prod", agent="billing", version_id=first["id"])
    ref = agents.resolve(store, "billing", environment="prod")
    assert ref.version_id == first["id"]
    assert ref.environment == "prod"


def test_an_unpromoted_agent_declares_but_does_not_pin(tmp_path):
    """Two different questions with two different answers.

    Publishing without promoting must not leave the agent declaration-less — an
    empty tool list would read as "declares nothing" rather than "not promoted
    yet". But it must not produce a PIN either: pinning a run to a version no
    environment points at routes it to a worker that does not exist, and
    `queue.claim`'s version filter then leaves the turn pending forever."""
    store = _store(tmp_path)
    version = _publish(store, tmp_path, "billing")
    ref = agents.resolve(store, "billing", environment="prod")
    assert (ref.declared_by or {})["id"] == version["id"]
    assert [t.id for t in ref.manifest.tools]
    assert ref.version_id is None and ref.environment is None


def test_a_version_published_before_the_manifest_was_persisted_stays_addressable(tmp_path):
    """`version_create` dedupes on (agent, bundleHash) and never backfills, so
    pre-#8 records keep their missing manifest forever. The agent must still be
    listable, promotable and rollback-able — those operate on version ids."""
    store = _store(tmp_path)
    version = _publish(store, tmp_path, "billing")
    stripped = {k: v for k, v in version.items() if k != "manifest"}
    (store.versions_dir / f"{version['id']}.json").write_text(__import__("json").dumps(stripped))

    ref = agents.resolve(store, "billing")
    assert ref.source == agents.SOURCE_UNKNOWN
    assert ref.manifest.name == "billing"
    assert ref.manifest.tools == []
    assert ref.describe()["manifestAvailable"] is False
    assert agents.names(store) == ["billing"]


def test_an_unreadable_manifest_names_the_version_that_needs_rolling_back(tmp_path):
    store = _store(tmp_path)
    version = _publish(store, tmp_path, "billing")
    broken = {**version, "manifest": {"name": "billing", "runtime": "cobol"}}
    (store.versions_dir / f"{version['id']}.json").write_text(__import__("json").dumps(broken))

    with pytest.raises(RyaError) as exc:
        agents.resolve(store, "billing")
    assert exc.value.code == "E_AGENT_MANIFEST_INVALID"
    assert version["id"] in exc.value.message


def test_one_broken_agent_does_not_make_the_list_unrenderable(tmp_path):
    """A workspace with a bad version must not lose the console for its other
    agents — `list_refs` degrades that one entry, not the call."""
    store = _store(tmp_path)
    version = _publish(store, tmp_path, "billing")
    _publish(store, tmp_path, "support")
    broken = {**version, "manifest": {"name": "billing", "runtime": "cobol"}}
    (store.versions_dir / f"{version['id']}.json").write_text(__import__("json").dumps(broken))

    refs = {r.name: r for r in agents.list_refs(store)}
    assert refs["billing"].source == agents.SOURCE_UNKNOWN
    assert refs["support"].source == agents.SOURCE_VERSION


def test_an_unknown_agent_is_a_named_refusal(tmp_path):
    store = _store(tmp_path)
    _publish(store, tmp_path, "billing")
    with pytest.raises(RyaError) as exc:
        agents.resolve(store, "support")
    assert exc.value.code == "E_AGENT_NOT_FOUND"


def test_a_mounted_project_answers_for_its_own_agent(tmp_path):
    """Single-tenant `rya dev` has no published versions at all. The working tree
    is also what the inline worker would execute, so reporting it is reporting
    what runs."""
    store = _store(tmp_path)
    root = _project(tmp_path, "local")
    assert agents.names(store, root) == ["local"]
    ref = agents.resolve(store, "local", root)
    assert ref.source == agents.SOURCE_PROJECT
    assert ref.version is None


def test_a_published_version_beats_the_mounted_tree(tmp_path):
    """The tempting rule is the other way round — the tree is what a single-tenant
    inline worker executes. But `AgentRef.version` is what a queued run gets
    pinned to and a working tree has no version, so preferring the tree would
    silently un-pin every run on a deployment that has both."""
    store = _store(tmp_path)
    root = _project(tmp_path, "local")
    version = create_version(store, agent="local", bundle=build_bundle(root),
                             environment="prod")

    ref = agents.resolve(store, "local", root, environment="prod")
    assert ref.source == agents.SOURCE_VERSION
    assert ref.version_id == version["id"]


def test_a_pre_manifest_version_borrows_the_mounted_tree_but_keeps_its_pin(tmp_path):
    """Same content, locally readable — a better answer than an empty placeholder.
    The pin still comes from the version, which is the part that matters."""
    store = _store(tmp_path)
    root = _project(tmp_path, "local")
    version = create_version(store, agent="local", bundle=build_bundle(root),
                             environment="prod")
    stripped = {k: v for k, v in version.items() if k != "manifest"}
    (store.versions_dir / f"{version['id']}.json").write_text(__import__("json").dumps(stripped))

    ref = agents.resolve(store, "local", root, environment="prod")
    assert ref.source == agents.SOURCE_PROJECT
    assert ref.version_id == version["id"]   # promoted, so it pins
    assert [t.id for t in ref.manifest.tools]


def test_a_mounted_project_does_not_answer_for_another_agent(tmp_path):
    """The mounted tree is one agent's, not the deployment's. Everyone else still
    comes from the database — that is what makes a single-tenant deployment
    multi-agent."""
    store = _store(tmp_path)
    root = _project(tmp_path, "local")
    _publish(store, tmp_path, "billing")
    assert agents.names(store, root) == ["billing", "local"]
    assert agents.resolve(store, "billing", root).source == agents.SOURCE_VERSION


def test_a_tenants_own_version_shadows_the_mounted_project(tmp_path):
    """The multi-tenant shape. The mounted tree is the OPERATOR's agent and is
    offered to every workspace — that is the deployed-agent model the product
    already ships. A workspace that publishes its own under the same name gets its
    own, resolved through its own store, so offering the tree is a fallback rather
    than a cross-tenant read."""
    store = _store(tmp_path)
    root = _project(tmp_path, "local")

    # A workspace that has published nothing sees the operator's agent.
    assert agents.resolve(store, "local", root).source == agents.SOURCE_PROJECT

    # One that has published its own sees its own, and gets the pin with it.
    version = create_version(store, agent="local", bundle=build_bundle(root),
                             environment="prod")
    ref = agents.resolve(store, "local", root, environment="prod")
    assert ref.source == agents.SOURCE_VERSION
    assert ref.version_id == version["id"]


def test_sole_agent_is_none_once_a_second_agent_exists(tmp_path):
    """D28 Rule 6's precondition: an unprefixed agent-scoped route is only
    unambiguous while there is exactly one candidate."""
    store = _store(tmp_path)
    assert agents.sole_agent(store) is None
    _publish(store, tmp_path, "billing")
    assert agents.sole_agent(store) == "billing"
    _publish(store, tmp_path, "support")
    assert agents.sole_agent(store) is None
