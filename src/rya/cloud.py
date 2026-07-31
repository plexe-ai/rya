"""Client + credential store for pointing the CLI/agent at a hosted Rya.

`rya login <url> --key <rya_sk_…>` stores a connection so the CLI can drive a
*hosted* instance (or your cloud) instead of the local project, and so a coding
agent can connect its remote MCP to the same place. This is the opt-in "use the
cloud" path — nothing here runs unless the user explicitly logs in (or sets
``RYA_REMOTE_URL`` / ``RYA_API_KEY``); the local runtime never phones home.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from .errors import RyaError


def _config_dir() -> Path:
    # RYA_HOME overrides for tests / non-standard setups.
    return Path(os.environ.get("RYA_HOME") or (Path.home() / ".rya"))


def _config_path() -> Path:
    return _config_dir() / "config.json"


def load_cloud_config() -> Optional[dict]:
    """Return ``{cloudUrl, apiKey}`` if logged in (env vars win over the file),
    else None (use the local runtime)."""
    env_url = os.environ.get("RYA_REMOTE_URL")
    if env_url:
        return {"cloudUrl": env_url.rstrip("/"), "apiKey": os.environ.get("RYA_API_KEY")}
    p = _config_path()
    if p.is_file():
        try:
            data = json.loads(p.read_text())
            if data.get("cloudUrl"):
                return data
        except (OSError, json.JSONDecodeError):
            return None
    return None


def save_cloud_config(url: str, key: Optional[str]) -> Path:
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"cloudUrl": url.rstrip("/"), "apiKey": key}, indent=2))
    try:
        os.chmod(p, 0o600)  # the API key lives here
    except OSError:  # pragma: no cover - non-POSIX
        pass
    return p


def clear_cloud_config() -> bool:
    p = _config_path()
    if p.is_file():
        p.unlink()
        return True
    return False


class RemoteClient:
    """Thin stdlib HTTP client for a hosted Rya control plane."""

    def __init__(self, url: str, key: Optional[str] = None) -> None:
        self.url = url.rstrip("/")
        self.key = key

    def _send(self, method: str, path: str, *, data: Optional[bytes] = None,
              content_type: str = "application/json", timeout: float = 30) -> Any:
        """One request, one error mapping. Bytes in, parsed JSON out.

        Split out of ``_req`` so the bundle upload can send ``application/gzip``
        without a second copy of the error handling — there is exactly one place
        that turns an HTTP failure into a ``RyaError``.
        """
        headers = {"content-type": content_type}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:600]
            raise self._http_error(e.code, detail)
        except urllib.error.URLError as e:
            raise RyaError("E_REMOTE", f"could not reach {self.url}: {e.reason}",
                           hint="Is the hosted instance up and the URL correct?")

    def _http_error(self, status: int, detail: str) -> RyaError:
        """Re-raise the SERVER's error code when it sent one.

        Collapsing the api's error bodies into ``E_REMOTE`` would end the stable
        error-code contract (docs/devex.md) at the network boundary: a caller
        could not tell a content-hash mismatch from an unreachable bucket. Three
        shapes are accepted because the api emits three — the RyaError handler
        sends ``to_dict()["error"]`` flat, ``HTTPException(detail={...})`` nests
        under ``detail``, and a hand-built body may nest under ``error``.
        Anything else (a proxy's HTML 413, say) keeps the old behaviour.
        """
        try:
            body = json.loads(detail)
        except (json.JSONDecodeError, TypeError, ValueError):
            body = None
        if isinstance(body, dict):
            for candidate in (body, body.get("error"), body.get("detail")):
                if isinstance(candidate, dict) and candidate.get("code"):
                    return RyaError(str(candidate["code"]),
                                    str(candidate.get("message") or f"HTTP {status}"),
                                    hint=candidate.get("hint"))
        code = "E_UNAUTHORIZED" if status in (401, 403) else "E_REMOTE"
        return RyaError(code, f"hosted Rya returned HTTP {status}: {detail[:300]}",
                        hint="Check the URL and API key (`rya login <url> --key …`).")

    def _req(self, method: str, path: str, body: Optional[dict] = None):
        return self._send(method, path,
                          data=json.dumps(body).encode() if body is not None else None)

    # operations the hosted control plane exposes
    def info(self) -> dict:
        return self._req("GET", "/v1/info")

    def send_event(self, type: str, payload: dict) -> dict:
        return self._req("POST", "/agents/_/events", {"type": type, "payload": payload})

    def list_runs(self) -> dict:
        return self._req("GET", "/agents/_/runs")

    def get_trace(self, run_id: str) -> dict:
        return self._req("GET", f"/runs/{run_id}/trace")

    def list_approvals(self, status: Optional[str] = None) -> dict:
        return self._req("GET", "/approvals" + (f"?status={status}" if status else ""))

    def approve(self, approval_id: str) -> dict:
        return self._req("POST", f"/approvals/{approval_id}/approve")

    def reject(self, approval_id: str) -> dict:
        return self._req("POST", f"/approvals/{approval_id}/reject")

    def publish(self, agent: str, archive: bytes, *, hash: str,
                env: Optional[str] = None, promote: bool = True,
                actor: Optional[str] = None, metadata: Optional[dict] = None,
                timeout: float = 300) -> dict:
        """Upload a packed bundle as a new immutable version of ``agent``.

        The archive goes up as a raw ``application/gzip`` body with the scalars in
        the query string — the same shape as ``POST /files`` — because the client
        SDK is stdlib-only and multipart would buy nothing here.

        ``hash`` is the content hash this client computed. The platform rebuilds
        it from the bytes it received and refuses a mismatch (D12), so this is a
        claim to be checked, not a label to be trusted. The default timeout is
        long because this is the one call that moves megabytes.
        """
        query = {"hash": hash, "promote": "true" if promote else "false"}
        if env:
            query["env"] = env
        if actor:
            query["actor"] = actor
        for key, value in (metadata or {}).items():
            query[f"meta.{key}"] = str(value)
        path = f"/agents/{urllib.parse.quote(agent, safe='')}/versions?{urllib.parse.urlencode(query)}"
        return self._send("POST", path, data=archive,
                          content_type="application/gzip", timeout=timeout)


def mcp_config_snippet(url: str, key_env: str = "RYA_TOKEN") -> dict:
    """The `.mcp.json` block a coding agent uses to connect its remote MCP to this
    hosted instance (key passed via env, not hardcoded)."""
    return {"mcpServers": {"rya": {
        "type": "http", "url": url.rstrip("/") + "/mcp",
        "headers": {"Authorization": "Bearer ${%s}" % key_env}}}}
