"""Remote MCP over HTTP — mounted on the control plane, auth-gated, plus a real
end-to-end MCP-client handshake against a live server."""

import asyncio
import socket
import threading
import time

from fastapi.testclient import TestClient
from starlette.routing import Mount

from rya.api.app import build_app
from rya.cli import scaffold


def _app(tmp_path, monkeypatch, token=None):
    for k in ("RYA_TOKEN", "RYA_MULTITENANT", "RYA_DATABASE_URL", "RYA_JWT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    if token:
        monkeypatch.setenv("RYA_TOKEN", token)
    scaffold.write_project(tmp_path, "mcp-remote", template="demo")
    return build_app(tmp_path)


def test_mcp_mounted_and_discovery(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    assert any(isinstance(r, Mount) and r.path == "/mcp" for r in app.routes)
    c = TestClient(app)
    info = c.get("/v1/info").json()
    assert info["service"] == "rya"
    assert info["remoteMcp"].endswith("/mcp")
    assert info["console"].endswith("/")
    assert info["websocket"].startswith("ws://") and info["websocket"].endswith("/ws")


def test_remote_mcp_requires_operator_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, token="sek")
    c = TestClient(app)
    # no token → 401 from the security middleware (before the mount even runs)
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "E_UNAUTHORIZED"


def test_project_provisioning_is_multitenant_only(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    c = TestClient(app)
    r = c.post("/v1/projects", json={"name": "acme"})
    assert r.status_code == 400  # single-tenant instance → not available


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_remote_mcp_end_to_end(tmp_path, monkeypatch):
    """Start a real uvicorn server, then drive it with the actual MCP HTTP client:
    initialize → list tools → call a tool. This proves remote MCP works over the
    wire, not just that the route is mounted."""
    import uvicorn

    app = _app(tmp_path, monkeypatch)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning", lifespan="on"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(200):  # wait up to ~10s for startup
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"

    async def go():
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = await s.list_tools()
                names = {tool.name for tool in tools.tools}
                assert "rya_get_agent" in names
                assert "rya_provision" in names and "rya_connect" in names
                result = await s.call_tool("rya_get_agent", {"project_dir": str(tmp_path)})
                return result, len(names)

    try:
        result, n_tools = asyncio.run(asyncio.wait_for(go(), 20))
        assert result is not None and not result.isError
        assert n_tools >= 25  # the full Rya tool surface over remote MCP
    finally:
        server.should_exit = True
        t.join(timeout=5)
