"""Real, general-purpose built-in tools — actual IO, not mocks.

These are the universal primitives an agent reaches for: an HTTP call and a web
fetch. Both go through the **Action Guard** before any byte leaves the process
(``check_egress``), so SSRF/egress policy applies to whatever the agent — or the
model in ``ctx.llm.run`` — decides to request.
"""

from __future__ import annotations

import html as _html
import json
import re
import urllib.error
import urllib.request

from ..errors import RyaError

_DROP = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _do_http(method: str, url: str, headers=None, body=None, timeout: int = 30):
    from ..guard import check_egress
    check_egress(url, method)  # Action Guard — raises E_EGRESS_BLOCKED if denied
    h = {k: str(v) for k, v in (headers or {}).items()}
    data = None
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            h.setdefault("content-type", "application/json")
        else:
            data = str(body).encode()
    req = urllib.request.Request(url, data=data, method=method.upper(), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace"), \
                r.headers.get("content-type", "")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read().decode("utf-8", "replace"), \
            (e.headers.get("content-type", "") if e.headers else "")
    except urllib.error.URLError as e:
        raise RyaError("E_RUNTIME", f"http request failed: {e.reason}",
                       hint="Check the URL and that egress to the host is allowed.")


def http_request(inp: dict) -> dict:
    """Make a real HTTP request (governed by the Action Guard)."""
    url = inp.get("url")
    if not url:
        raise RyaError("E_VALIDATION", "http.request requires a 'url'.")
    method = (inp.get("method") or "GET").upper()
    status, headers, raw, ctype = _do_http(method, url, inp.get("headers"), inp.get("body"),
                                            int(inp.get("timeout", 30)))
    limit = int(inp.get("maxChars", 20000))
    out = {"status": status, "url": url, "contentType": ctype,
           "body": raw[:limit], "truncated": len(raw) > limit}
    if "json" in (ctype or ""):
        try:
            out["json"] = json.loads(raw)
        except ValueError:
            pass
    return out


def web_fetch(inp: dict) -> dict:
    """GET a URL and return readable text (HTML stripped). Governed by the guard."""
    url = inp.get("url")
    if not url:
        raise RyaError("E_VALIDATION", "web.fetch requires a 'url'.")
    status, headers, raw, ctype = _do_http("GET", url, inp.get("headers"), None,
                                           int(inp.get("timeout", 30)))
    text = raw
    if "html" in (ctype or "") or raw.lstrip().startswith("<"):
        text = _WS.sub(" ", _html.unescape(_TAGS.sub(" ", _DROP.sub(" ", raw)))).strip()
    limit = int(inp.get("maxChars", 8000))
    return {"url": url, "status": status, "contentType": ctype,
            "text": text[:limit], "truncated": len(text) > limit}
