"""`rya provision` — the base-infra assembler/inventory."""

from rya.cli import scaffold
from rya.manifest import load_manifest
from rya.provision import provision
from rya.runtime import load_agent
from rya.store import Store


def _setup(tmp_path):
    scaffold.write_project(tmp_path, "prov", template="demo")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    store = Store(tmp_path)
    store.ensure()
    return manifest, store, agent


def test_provision_local_inventory(tmp_path, monkeypatch):
    for k in ("RYA_MULTITENANT", "RYA_DATABASE_URL", "RYA_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    manifest, store, agent = _setup(tmp_path)
    rep = provision(manifest, store, agent, tmp_path, target="local")

    keys = {c["key"] for c in rep["components"]}
    # every named base-infra primitive is present
    assert {"runtime", "database", "memory", "sessions", "auth", "guardrails",
            "realtime", "jobs", "scale", "observability", "secrets", "readiness"} <= keys
    assert rep["target"] == "local"
    # the real-time websocket channel is reported with its endpoint
    rt = next(c for c in rep["components"] if c["key"] == "realtime")
    assert rt["endpoint"] == "WS /ws" and rt["status"] == "ready"
    # jobs report retry + dead-letter
    jobs = next(c for c in rep["components"] if c["key"] == "jobs")
    assert jobs["retryMaxAttempts"] == 3 and jobs["deadLetter"] is True
    assert rep["connection"]["websocket"].endswith("/ws")


def test_provision_writes_guard_policy(tmp_path, monkeypatch):
    monkeypatch.delenv("RYA_MULTITENANT", raising=False)
    manifest, store, agent = _setup(tmp_path)
    # remove the scaffold's guard file so provision has to write one
    (tmp_path / "rya.guard.yaml").unlink()
    rep = provision(manifest, store, agent, tmp_path, target="local", apply=True)
    assert (tmp_path / "rya.guard.yaml").exists()
    g = next(c for c in rep["components"] if c["key"] == "guardrails")
    assert g["status"] == "provisioned" and g["testsTotal"] >= 1
    assert any("egress policy" in a for a in rep["actions"])


def test_provision_production_target_generates_token_and_flags_file_store(tmp_path, monkeypatch):
    for k in ("RYA_MULTITENANT", "RYA_DATABASE_URL", "RYA_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    manifest, store, agent = _setup(tmp_path)
    rep = provision(manifest, store, agent, tmp_path, target="postgres")
    # no DB url + production target → file store is flagged as below the bar
    db = next(c for c in rep["components"] if c["key"] == "database")
    assert db["status"] == "warn" and db["durable"] is False
    # no auth configured + production target → an operator token is generated
    auth = next(c for c in rep["components"] if c["key"] == "auth")
    assert auth["status"] == "provisioned" and auth["token"].startswith("rya_")
    assert rep["connection"]["operatorToken"] == auth["token"]


def test_provision_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("RYA_MULTITENANT", raising=False)
    manifest, store, agent = _setup(tmp_path)
    (tmp_path / "rya.guard.yaml").unlink()
    rep = provision(manifest, store, agent, tmp_path, target="local", apply=False)
    assert not (tmp_path / "rya.guard.yaml").exists()
    assert rep["actions"] == []
