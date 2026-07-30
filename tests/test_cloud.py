"""`rya login` + the `rya cloud` client — driving a hosted Rya end to end."""

import json as _json
import socket
import threading
import time

from typer.testing import CliRunner

from rya import cloud
from rya.cli import scaffold
from rya.cli.main import app as cli

runner = CliRunner()


def test_cloud_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("RYA_HOME", str(tmp_path))
    for k in ("RYA_REMOTE_URL", "RYA_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert cloud.load_cloud_config() is None
    p = cloud.save_cloud_config("https://rya.host/", "rya_sk_abc")
    assert p.exists()
    cfg = cloud.load_cloud_config()
    assert cfg["cloudUrl"] == "https://rya.host" and cfg["apiKey"] == "rya_sk_abc"
    # env var overrides the file
    monkeypatch.setenv("RYA_REMOTE_URL", "https://other.host")
    assert cloud.load_cloud_config()["cloudUrl"] == "https://other.host"
    monkeypatch.delenv("RYA_REMOTE_URL")
    assert cloud.clear_cloud_config() is True
    assert cloud.load_cloud_config() is None


def test_mcp_config_snippet():
    snip = cloud.mcp_config_snippet("https://rya.host/")
    s = snip["mcpServers"]["rya"]
    assert s["url"] == "https://rya.host/mcp" and s["type"] == "http"
    assert s["headers"]["Authorization"] == "Bearer ${RYA_TOKEN}"


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def _out(result):
    # the last JSON object printed by a --json command
    for line in reversed(result.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return _json.loads(line)
    return None


def test_login_and_drive_hosted_agent_end_to_end(tmp_path, monkeypatch):
    import uvicorn
    from rya.api.app import build_app

    for k in ("RYA_TOKEN", "RYA_MULTITENANT", "RYA_DATABASE_URL", "RYA_JWT_SECRET",
              "RYA_REMOTE_URL", "RYA_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RYA_HOME", str(tmp_path / "home"))

    project = tmp_path / "agent"
    scaffold.write_project(project, "hosted", template="demo")
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(build_app(project), host="127.0.0.1",
                                           port=port, log_level="warning", lifespan="on"))
    t = threading.Thread(target=server.run, daemon=True); t.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    url = f"http://127.0.0.1:{port}"

    try:
        # 1. login → verifies, stores, prints the .mcp.json snippet
        r = runner.invoke(cli, ["login", url, "--json"])
        assert r.exit_code == 0, r.stdout
        out = _out(r)
        assert out["mode"] == "cloud" and out["remoteMcp"].endswith("/mcp")
        assert out["mcpConfig"]["mcpServers"]["rya"]["url"].endswith("/mcp")

        # 2. whoami reflects the hosted connection
        who = _out(runner.invoke(cli, ["whoami", "--json"]))
        assert who["mode"] == "cloud" and who["cloudUrl"] == url

        # 3. drive the hosted agent: trigger a run (pauses for approval)
        sent = _out(runner.invoke(cli, ["cloud", "send", "--type", "message.received",
                                        "--payload", '{"email":"ada@acme.io"}', "--json"]))
        assert sent["status"] == "waiting_approval" and sent["pendingApproval"]
        apr_id = sent["pendingApproval"]

        # 4. runs + approvals visible remotely
        runs = _out(runner.invoke(cli, ["cloud", "runs", "--json"]))["runs"]
        assert any(x["id"] == sent["runId"] for x in runs)
        apprs = _out(runner.invoke(cli, ["cloud", "approvals", "--json"]))["approvals"]
        assert any(a["id"] == apr_id for a in apprs)

        # 5. approve remotely → the real hosted run resumes to completion
        appr = _out(runner.invoke(cli, ["cloud", "approve", apr_id, "--json"]))
        assert appr.get("runStatus") == "completed"

        # 6. logout → back to local
        runner.invoke(cli, ["logout", "--json"])
        assert cloud.load_cloud_config() is None
    finally:
        server.should_exit = True
        t.join(timeout=5)


def test_publish_over_http_end_to_end(tmp_path, monkeypatch):
    """`rya publish` against a real control plane: the whole point of the HTTP
    path is that a client repo ships code with no database or bucket access."""
    import uvicorn

    from rya import bundles
    from rya.api.app import build_app
    from rya.store import open_store

    for k in ("RYA_TOKEN", "RYA_MULTITENANT", "RYA_DATABASE_URL", "RYA_JWT_SECRET",
              "RYA_REMOTE_URL", "RYA_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RYA_HOME", str(tmp_path / "home"))
    # The endpoint refuses to publish to an unauthenticated control plane.
    monkeypatch.setenv("RYA_ALLOW_UNAUTHENTICATED_PUBLISH", "1")

    served = tmp_path / "platform"
    scaffold.write_project(served, "hosted", template="minimal")
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(build_app(served), host="127.0.0.1",
                                           port=port, log_level="warning", lifespan="on"))
    t = threading.Thread(target=server.run, daemon=True); t.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    url = f"http://127.0.0.1:{port}"

    # A separate tree, as a client repo would be.
    client_repo = tmp_path / "clientrepo"
    scaffold.write_project(client_repo, "hosted", template="minimal")
    expected = bundles.build_bundle(client_repo).hash

    try:
        monkeypatch.chdir(client_repo)
        r = runner.invoke(cli, ["publish", "--url", url, "--env", "prod",
                                "--actor", "ada@example.com",
                                "--metadata", "gitSha=deadbee", "--json"])
        assert r.exit_code == 0, r.stdout
        out = _out(r)
        assert out["ok"] is True
        assert out["bundleHash"] == expected      # client hash == recorded hash
        assert out["promoted"] is True and out["environment"] == "prod"
        assert out["attested"] is False           # honest about what HTTP cannot do

        version = open_store(served).version_get(out["versionId"])
        assert version["bundleHash"] == expected
        assert version["metadata"]["gitSha"] == "deadbee"
        assert version["metadata"]["check"] == "passed"

        # The artifact is where the worker will look for it.
        assert bundles.bundle_archive_path(
            expected, bundles.default_archive_root(served)).is_file()
    finally:
        server.should_exit = True
        t.join(timeout=5)


def test_publish_without_a_url_or_login_is_a_validation_error(tmp_path, monkeypatch):
    for k in ("RYA_REMOTE_URL", "RYA_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RYA_HOME", str(tmp_path / "home"))
    project = tmp_path / "agent"
    scaffold.write_project(project, "nowhere", template="minimal")
    monkeypatch.chdir(project)

    r = runner.invoke(cli, ["publish", "--json"])
    assert r.exit_code != 0
    out = _out(r)
    assert out["error"]["code"] == "E_VALIDATION"
    assert "rya login" in out["error"]["hint"]


def test_the_server_error_code_survives_the_network(monkeypatch):
    """A hash mismatch must not reach the caller as "HTTP 409": the stable error
    codes are the CLI's contract, and collapsing them at the boundary would make
    "the bucket is down" indistinguishable from "your artifact changed"."""
    import urllib.error

    from rya.errors import RyaError

    body = _json.dumps({"detail": {"code": "E_BUNDLE_MISMATCH", "message": "no match",
                                   "hint": "re-bundle"}}).encode()

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, __import__("io").BytesIO(body))

    monkeypatch.setattr("urllib.request.urlopen", boom)
    client = cloud.RemoteClient("http://example.invalid", "k")
    try:
        client.publish("a", b"x", hash="0" * 64)
        raise AssertionError("expected a RyaError")
    except RyaError as e:
        assert e.code == "E_BUNDLE_MISMATCH"
        assert e.message == "no match" and e.hint == "re-bundle"


def test_a_non_rya_error_body_still_maps_to_e_remote(monkeypatch):
    """A proxy's HTML 413 carries no Rya code, so the old behaviour must hold."""
    import urllib.error

    from rya.errors import RyaError

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 413, "Too Large", {},
                                     __import__("io").BytesIO(b"<html>too big</html>"))

    monkeypatch.setattr("urllib.request.urlopen", boom)
    try:
        cloud.RemoteClient("http://example.invalid").publish("a", b"x", hash="0" * 64)
        raise AssertionError("expected a RyaError")
    except RyaError as e:
        assert e.code == "E_REMOTE" and "413" in e.message
