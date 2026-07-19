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
