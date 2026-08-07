"""One deployment, many agents — the Phase 2 property (D21, D28).

`build_app` used to resolve one `rya.agent.yaml` at boot, so `{agent_id}` was
decorative and a second agent had nowhere to be. These tests are the statement
that it no longer is: two agents published from two different projects are served
by one process, independently promotable, independently governed, and addressed
by name rather than by whichever manifest happened to be on disk.

The negative cases matter as much as the positive ones. An unknown agent must
404 (it could not, while the id was decorative), and an unprefixed agent-scoped
route on a two-agent deployment must refuse and name the candidates rather than
silently answer for one of them.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rya.api.app import RULE6_SUNSET, build_app
from rya.bundles import build_bundle, pack
from rya.cli import scaffold


def _project(tmp_path: Path, name: str) -> Path:
    root = tmp_path / "src-projects" / name
    root.mkdir(parents=True, exist_ok=True)
    scaffold.write_project(root, name, template="demo")
    return root


def _publish(client: TestClient, tmp_path: Path, name: str, *, env: str | None = None):
    """Publish through the HTTP path — the one that never imports the bundle."""
    root = _project(tmp_path, name)
    bundle = build_bundle(root)
    archive = pack(bundle, tmp_path / f"{name}.tar.gz")
    q = f"?hash={bundle.hash}" + (f"&env={env}" if env else "&promote=false")
    r = client.post(f"/agents/{name}/versions{q}", content=archive.read_bytes(),
                    headers={"content-type": "application/gzip"})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    """An api with NO mounted project — the manifest-free boot D21 introduces."""
    monkeypatch.setenv("RYA_ALLOW_UNAUTHENTICATED_PUBLISH", "1")
    monkeypatch.delenv("RYA_MULTITENANT", raising=False)
    monkeypatch.delenv("RYA_DATABASE_URL", raising=False)
    home = tmp_path / "deployment"
    home.mkdir()
    return TestClient(build_app(home)), home


def test_the_api_boots_with_no_manifest_on_disk(deployment):
    """The whole of D21 in one assertion: `load_manifest(root/'rya.agent.yaml')`
    used to run at boot, so a deployment without one could not start at all."""
    client, _ = deployment
    assert client.get("/healthz").json()["ok"] is True
    assert client.get("/agents").json() == {"agents": [], "count": 0}


def test_two_agents_from_two_projects_are_served_by_one_deployment(deployment, tmp_path):
    client, _ = deployment
    _publish(client, tmp_path, "billing", env="prod")
    _publish(client, tmp_path, "support", env="prod")

    served = client.get("/agents").json()
    assert [a["name"] for a in served["agents"]] == ["billing", "support"]

    # Each answers for ITSELF, not for whichever was published last.
    assert client.get("/agents/billing").json()["name"] == "billing"
    assert client.get("/agents/support").json()["name"] == "support"
    assert client.get("/agents/billing/tools").json()["agent"] == "billing"


def test_publishing_an_agent_this_deployment_never_heard_of_is_accepted(deployment, tmp_path):
    """`app.py`'s name check refused any bundle whose name was not the mounted
    manifest's. It is obsolete rather than relaxed: the version record IS the
    agent's existence, so an unknown name is a new agent."""
    client, _ = deployment
    out = _publish(client, tmp_path, "brand-new")
    assert out["agent"] == "brand-new"
    assert "brand-new" in [a["name"] for a in client.get("/agents").json()["agents"]]


def test_a_bundle_still_cannot_be_filed_under_a_name_it_does_not_declare(deployment, tmp_path):
    """What survives of the old check — the half that was always about the
    ARTIFACT. Content filed in a namespace it does not claim would become
    promotable into the wrong pointer."""
    client, _ = deployment
    root = _project(tmp_path, "billing")
    bundle = build_bundle(root)
    archive = pack(bundle, tmp_path / "mislabelled.tar.gz")
    r = client.post(f"/agents/support/versions?hash={bundle.hash}&promote=false",
                    content=archive.read_bytes(),
                    headers={"content-type": "application/gzip"})
    assert r.status_code == 400
    msg = r.json()["error"]["message"]
    assert "billing" in msg and "support" in msg


def test_the_two_agents_promote_and_roll_back_independently(deployment, tmp_path):
    client, _ = deployment
    billing_v1 = _publish(client, tmp_path, "billing", env="prod")
    support_v1 = _publish(client, tmp_path, "support", env="prod")

    # A second version of billing only.
    (tmp_path / "src-projects" / "billing" / "src" / "extra.py").write_text("X = 1\n")
    billing_v2 = _publish(client, tmp_path, "billing", env="prod")
    assert billing_v2["versionId"] != billing_v1["versionId"]

    def current(agent):
        return client.get(f"/agents/{agent}/environments/prod").json()["currentVersion"]["id"]

    assert current("billing") == billing_v2["versionId"]
    assert current("support") == support_v1["versionId"]  # untouched

    # Rolling billing back leaves support exactly where it was.
    r = client.post("/agents/billing/environments/prod/rollback", json={})
    assert r.status_code == 200, r.text
    assert current("billing") == billing_v1["versionId"]
    assert current("support") == support_v1["versionId"]


def test_each_agent_has_its_own_guard_policy(deployment, tmp_path):
    """The finding that made D28 more than routing: `rya_policy` is keyed
    `(workspace_id, key)` and both call sites passed a bare literal, so two agents
    in one workspace shared one guard — silently."""
    client, _ = deployment
    _publish(client, tmp_path, "billing", env="prod")
    _publish(client, tmp_path, "support", env="prod")

    r = client.put("/agents/billing/guard",
                   json={"policy": {"rules": [{"action": "allow", "pattern": "api.billing.test"}]}})
    assert r.status_code == 200, r.text

    billing = client.get("/agents/billing/guard").json()
    support = client.get("/agents/support/guard").json()
    assert billing["exists"] is True
    assert any("api.billing.test" in str(rule) for rule in billing["policy"]["rules"])
    assert support["exists"] is False, "support inherited billing's guard"


def test_each_agent_has_its_own_promotion_gate(deployment, tmp_path):
    client, _ = deployment
    _publish(client, tmp_path, "billing", env="prod")
    _publish(client, tmp_path, "support", env="prod")

    assert client.put("/agents/billing/gate",
                      json={"environments": {"prod": {"requireReadiness": True}}}
                      ).status_code == 200

    def prod_gate(agent):
        gates = client.get(f"/agents/{agent}/gate").json()["gates"]
        return next(g for g in gates if g["environment"] == "prod")

    assert prod_gate("billing")["requireReadiness"] is True
    assert prod_gate("support")["requireReadiness"] is False


def test_a_gate_written_before_d28_still_governs_an_agent_that_has_none(deployment, tmp_path):
    """The upgrade path. An eager rename would have to guess which agent a shared
    row belonged to; the read-time fallback keeps it enforcing for everyone until
    each agent is given its own."""
    from rya.gates import POLICY_KEY
    from rya.store import open_store

    client, home = deployment
    _publish(client, tmp_path, "billing", env="prod")
    _publish(client, tmp_path, "support", env="prod")
    open_store(home).policy_set(POLICY_KEY, {"environments": {"prod": {"requireEvals": True}}})

    def prod_gate(agent):
        gates = client.get(f"/agents/{agent}/gate").json()["gates"]
        return next(g for g in gates if g["environment"] == "prod")

    assert prod_gate("billing")["requireEvals"] is True
    assert prod_gate("support")["requireEvals"] is True

    # Giving billing its own takes over for billing ALONE.
    client.put("/agents/billing/gate", json={"environments": {"prod": {"requireActor": True}}})
    assert prod_gate("billing")["requireEvals"] is False
    assert prod_gate("billing")["requireActor"] is True
    assert prod_gate("support")["requireEvals"] is True


def test_an_unknown_agent_is_a_404(deployment, tmp_path):
    client, _ = deployment
    _publish(client, tmp_path, "billing")
    r = client.get("/agents/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "E_AGENT_NOT_FOUND"


def test_an_unprefixed_route_resolves_the_sole_agent_and_says_it_is_deprecated(deployment, tmp_path):
    """D28 Rule 6. Without it the CLI, the console and `e2e_platform.py` all break
    in one commit — and a migration that must land atomically gets deferred."""
    client, _ = deployment
    _publish(client, tmp_path, "billing", env="prod")
    r = client.get("/tools")
    assert r.status_code == 200
    assert r.json()["agent"] == "billing"
    assert r.headers["Deprecation"] == "true"
    assert r.headers["Sunset"] == RULE6_SUNSET


def test_an_unprefixed_route_refuses_once_a_second_agent_exists(deployment, tmp_path):
    client, _ = deployment
    _publish(client, tmp_path, "billing", env="prod")
    _publish(client, tmp_path, "support", env="prod")
    r = client.get("/tools")
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_AGENT_AMBIGUOUS"
    # The hint names both candidates, which is the entire value of this error. It
    # reached the console as the string "HTTP 400" until the envelope was unified.
    assert "billing" in body["error"]["hint"] and "support" in body["error"]["hint"]


def test_the_prefixed_route_carries_no_deprecation_header(deployment, tmp_path):
    client, _ = deployment
    _publish(client, tmp_path, "billing", env="prod")
    r = client.get("/agents/billing/tools")
    assert r.status_code == 200
    assert "Deprecation" not in r.headers


def test_the_console_lists_every_agent_and_scopes_to_the_selected_one(deployment, tmp_path):
    client, _ = deployment
    _publish(client, tmp_path, "billing", env="prod")
    _publish(client, tmp_path, "support", env="prod")

    body = client.get("/console?agent=support").json()
    assert [a["name"] for a in body["agents"]] == ["billing", "support"]
    assert body["selectedAgent"] == "support"
    assert body["agent"]["name"] == "support"
    # The api imported no handler, so it cannot claim to know the handler set.
    assert body["agent"]["handlers"] is None

    assert client.get("/console?agent=nope").status_code == 404


def test_the_console_renders_before_anything_is_published(deployment):
    """A fresh workspace is a real state, not an error — the dashboard has to
    load so the operator can find out how to publish."""
    client, _ = deployment
    body = client.get("/console").json()
    assert body["ok"] is True and body["agents"] == [] and body["selectedAgent"] is None
