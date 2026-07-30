"""Action Guard — egress policy engine, SSRF, real enforcement, and API."""

import json
import os
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


@contextmanager
def _serving(handler_cls):
    """Run a local HTTP server that handles requests until torn down.

    Earlier this used a single-shot ``handle_request()`` on a daemon thread,
    which raced the client and intermittently surfaced 'Connection reset by
    peer'. ``serve_forever`` on a background thread (cleanly shut down on exit)
    removes that race.
    """
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)
from fastapi.testclient import TestClient

from rya.api.app import build_app
from rya.cli import scaffold
from rya.errors import RyaError
from rya.guard import (
    POLICY_KEY,
    GuardPolicy,
    _compile_secrecy,
    check_egress,
    evaluate,
    grounding_policy,
    is_ssrf,
    load_policy,
    resolve_policy,
    run_tests,
    save_policy,
    secrecy_check,
    secrecy_scrub,
    secrecy_scrub_text,
)
from rya.manifest import load_manifest
from rya.runtime import Engine, load_agent
from rya.store import Store

POLICY = {
    "ssrf": True, "default": "deny", "fail": "closed",
    "rules": [
        {"action": "allow", "kind": "prefix", "pattern": "https://api.acme.com/", "methods": ["GET", "POST"]},
        {"action": "allow", "kind": "glob", "pattern": "https://*.openai.com/*"},
        {"action": "deny", "kind": "glob", "pattern": "https://webhook.site/*", "note": "exfil host"},
    ],
}


# ---- engine --------------------------------------------------------------
def test_allow_deny_default():
    assert evaluate("https://api.acme.com/orders", "POST", POLICY)["decision"] == "allow"
    assert evaluate("https://api.openai.com/v1/x", "POST", POLICY)["decision"] == "allow"
    assert evaluate("https://elsewhere.com/x", "POST", POLICY)["decision"] == "block"  # default deny
    d = evaluate("https://webhook.site/abc", "POST", POLICY)
    assert d["decision"] == "block" and d["reason"] == "exfil host"


def test_deny_beats_allow():
    pol = {"default": "allow", "ssrf": False, "rules": [
        {"action": "allow", "kind": "glob", "pattern": "https://x.com/*"},
        {"action": "deny", "kind": "glob", "pattern": "https://x.com/secret*"},
    ]}
    assert evaluate("https://x.com/secret/1", "GET", pol)["decision"] == "block"
    assert evaluate("https://x.com/public", "GET", pol)["decision"] == "allow"


def test_method_scoping():
    pol = {"default": "deny", "ssrf": False, "rules": [
        {"action": "allow", "kind": "prefix", "pattern": "https://api.acme.com/", "methods": ["GET"]}]}
    assert evaluate("https://api.acme.com/x", "GET", pol)["decision"] == "allow"
    assert evaluate("https://api.acme.com/x", "POST", pol)["decision"] == "block"


def test_ssrf():
    for h in ("localhost", "127.0.0.1", "169.254.169.254", "10.0.0.5", "192.168.1.1", "metadata.google.internal"):
        assert is_ssrf(h) is True
    assert is_ssrf("api.anthropic.com") is False
    assert evaluate("http://169.254.169.254/latest/meta-data/", "GET", POLICY)["decision"] == "block"


def test_run_tests_metrics():
    rep = run_tests(POLICY)
    assert rep["attacksBlocked"] == rep["attacksTotal"]   # all attacks blocked
    assert rep["benignFalseBlocks"] == 0
    assert rep["accuracy"] == 100


# ---- real enforcement at the egress chokepoint ---------------------------
def _http_tool_agent(tmp_path, port):
    (tmp_path / "rya.agent.yaml").write_text(
        "name: t\nruntime: python\nentrypoint: agent.py\n"
        f"tools:\n  - id: remote.call\n    permission: allowed\n    url: http://127.0.0.1:{port}\n")
    (tmp_path / "agent.py").write_text(
        "from rya import define_agent\nagent=define_agent()\n@agent.on_event\nasync def h(ctx,e):\n"
        "    return await ctx.tools.call('remote.call', {'x':1})\n")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    return Engine(manifest, load_agent(manifest, tmp_path), Store(tmp_path), tmp_path)


def test_egress_blocked_never_leaves(tmp_path, monkeypatch):
    received = {"hit": False}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            received["hit"] = True
            self.rfile.read(int(self.headers.get("content-length", 0) or 0))  # drain body before replying
            self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers()
            self.wfile.write(b"{}")
        def log_message(self, *a): pass

    with _serving(H) as port:
        # Guard that denies everything by default (and would SSRF-block 127.0.0.1 anyway).
        guard = tmp_path / "rya.guard.yaml"
        guard.write_text("ssrf: true\ndefault: deny\nrules: []\n")
        monkeypatch.setenv("RYA_GUARD_PATH", str(guard))

        engine = _http_tool_agent(tmp_path, port)
        run = engine.run_event("x", {})
    assert run["status"] == "failed"
    assert run["error"]["code"] == "E_EGRESS_BLOCKED"
    assert received["hit"] is False  # the request never left the process


def test_egress_allowed_passes(tmp_path, monkeypatch):
    received = {"hit": False}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            received["hit"] = True
            self.rfile.read(int(self.headers.get("content-length", 0) or 0))  # drain body before replying
            self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers()
            self.wfile.write(b'{"ok":true}')
        def log_message(self, *a): pass

    with _serving(H) as port:
        guard = tmp_path / "rya.guard.yaml"
        guard.write_text(f"ssrf: false\ndefault: deny\nrules:\n  - {{action: allow, kind: prefix, pattern: 'http://127.0.0.1:{port}'}}\n")
        monkeypatch.setenv("RYA_GUARD_PATH", str(guard))

        engine = _http_tool_agent(tmp_path, port)
        run = engine.run_event("x", {})
    assert run["status"] == "completed"
    assert received["hit"] is True


def test_no_policy_is_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("RYA_GUARD_PATH", str(tmp_path / "missing.yaml"))
    check_egress("https://anything.com/x", "POST")  # must not raise


# ---- id-secrecy scrub ----------------------------------------------------
# The secrecy scrub redacts opaque master ids at the boundary. Over-redaction
# (eating passports / numeric account ids / phones) is as much a regression as
# under-redaction (leaking a master id), so both directions are pinned here.
SECRECY_POLICY = {
    "secrecy": {
        "enabled": True,
        "apply_on": ["tool_result", "outbound"],
        "action": "scrub",
        "patterns": [
            {"id": "opaque_master_id", "kind": "regex",
             "pattern": r"\b[A-Za-z]{3,8}\d{8,}\b", "replacement": "(id hidden)"},
        ],
    }
}
_SECRECY = _compile_secrecy(SECRECY_POLICY)


@pytest.mark.parametrize("raw,want", [
    # IS scrubbed — 3-8 leading letters + 8+ digits = an opaque master id.
    ("The master id is IjmQ1782803306 for this student.",
     "The master id is (id hidden) for this student."),
    ("YKHw1782723298 / sGog1782764730", "(id hidden) / (id hidden)"),
    # NOT scrubbed — known safe values that must survive.
    ("account 1472802 was created", "account 1472802 was created"),          # numeric account id
    ("passport MEGHA1234", "passport MEGHA1234"),                       # only 4 trailing digits
    ("passport Z1234567", "passport Z1234567"),                         # 1 letter + 7 digits
    ("September 2026, $33,800, 9903105259", "September 2026, $33,800, 9903105259"),  # year/money/phone
    ("", ""),
    ("hello", "hello"),
])
def test_secrecy_scrub_matches_production(raw, want):
    assert secrecy_scrub_text(raw, _SECRECY)[0] == want


def test_secrecy_scrub_reports_hits():
    scrubbed, hits = secrecy_scrub_text("id IjmQ1782803306 and YKHw1782723298", _SECRECY)
    assert scrubbed == "id (id hidden) and (id hidden)"
    assert hits == ["opaque_master_id", "opaque_master_id"]
    assert secrecy_scrub_text("account 1472802", _SECRECY)[1] == []


def test_secrecy_scrub_per_leaf_of_dict():
    # A tool result is a parsed object, not a JSON blob: scrub string LEAVES,
    # leave keys and non-string values (ints, bools) untouched, never corrupt JSON.
    obj = {
        "masterId": "IjmQ1782803306",
        "accountId": "1472802",            # numeric account id preserved
        "count": 3,                      # non-string leaf untouched
        "nested": {"note": "see sGog1782764730"},
        "list": ["YKHw1782723298", "plain text", 42],
    }
    out = secrecy_scrub(obj, _SECRECY)
    assert out == {
        "masterId": "(id hidden)",
        "accountId": "1472802",
        "count": 3,
        "nested": {"note": "see (id hidden)"},
        "list": ["(id hidden)", "plain text", 42],
    }


def test_secrecy_disabled_is_noop():
    assert _compile_secrecy({"secrecy": {"enabled": False}}) == []
    assert _compile_secrecy({}) == []
    assert secrecy_scrub({"masterId": "IjmQ1782803306"}, []) == {"masterId": "IjmQ1782803306"}


def test_secrecy_check_reports_without_mutating_caller():
    r = secrecy_check("id IjmQ1782803306", SECRECY_POLICY)
    assert r["ok"] is False and r["hits"] == ["opaque_master_id"]
    assert r["scrubbed"] == "id (id hidden)"
    assert secrecy_check("account 1472802", SECRECY_POLICY)["ok"] is True


def _secrecy_agent(tmp_path):
    """Agent whose one tool returns a record carrying a master id AND a numeric
    account id, so we can prove the boundary scrub redacts the former and preserves
    the latter — end-to-end through the engine + trace."""
    (tmp_path / "rya.agent.yaml").write_text(
        "name: t\nruntime: python\nentrypoint: agent.py\n"
        "tools:\n  - id: lookup\n    permission: allowed\n")
    (tmp_path / "agent.py").write_text(
        "from rya import define_agent\n"
        "agent = define_agent()\n"
        "@agent.tool('lookup')\n"
        "async def lookup(inp):\n"
        "    return {'masterId': 'IjmQ1782803306', 'accountId': '1472802'}\n"
        "@agent.on_event\n"
        "async def h(ctx, e):\n"
        "    return await ctx.tools.call('lookup', {})\n")
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    return Engine(manifest, load_agent(manifest, tmp_path), Store(tmp_path), tmp_path)


def test_secrecy_scrub_fires_at_tool_boundary(tmp_path, monkeypatch):
    guard = tmp_path / "rya.guard.yaml"
    guard.write_text(json.dumps({"ssrf": False, "default": "allow", "rules": [], **SECRECY_POLICY}))
    monkeypatch.setenv("RYA_GUARD_PATH", str(guard))

    engine = _secrecy_agent(tmp_path)
    run = engine.run_event("x", {})
    assert run["status"] == "completed"

    # The scrub runs INSIDE the journaled tool step, so neither the returned
    # result nor the trace should ever carry the raw master id — but the numeric
    # account id survives untouched.
    blob = json.dumps(run, default=str)
    assert "IjmQ1782803306" not in blob
    assert "(id hidden)" in blob
    assert "1472802" in blob


# ---- injected policy source (PLATFORM_DESIGN §5.1, D7, D8, §12 risk 7) ----
# The guard is governance, so its policy must be INJECTED, not discovered. These
# pin the seam: an explicit policy wins over anything ambient, two tenants never
# share a compiled-rule cache, a broken policy source denies while an absent one
# no-ops, and every write produces an audit record.

class _FakeStore:
    """The duck-typed policy source `store.py` is growing: ``policy_get(key)`` /
    ``policy_set(key, value, actor=None)`` keyed ``"guard"``. Nothing else about a
    store matters to the guard, which is the point of the protocol."""

    def __init__(self, record=None, raises=None):
        self.record, self.raises, self.writes = record, raises, []

    def policy_get(self, key):
        assert key == POLICY_KEY
        if self.raises:
            raise self.raises
        return self.record

    def policy_set(self, key, value, actor=None):
        self.writes.append((key, value, actor))
        self.record = value
        return value


def _envelope(policy, version):
    return {"key": POLICY_KEY, "version": version, "policy": policy}


@pytest.fixture
def hostile_ambient(tmp_path, monkeypatch):
    """A cwd policy AND an env-var policy that both allow everything — the exact
    ambient state the old cwd/mtime loader would have obeyed."""
    permissive = "ssrf: false\ndefault: allow\nrules: []\n"
    (tmp_path / "rya.guard.yaml").write_text(permissive)
    elsewhere = tmp_path / "elsewhere.guard.yaml"
    elsewhere.write_text(permissive)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RYA_GUARD_PATH", str(elsewhere))
    return tmp_path


def test_explicit_policy_beats_cwd_and_env(hostile_ambient):
    # The regression test for the rewrite: a worker's cwd is an artefact of where a
    # bundle got extracted (§5.1) and ambient env is what D8 kills, so neither may
    # influence a verdict once a policy is supplied.
    with pytest.raises(RyaError) as e:
        check_egress("https://elsewhere.com/x", "POST", POLICY)
    assert e.value.code == "E_EGRESS_BLOCKED"
    check_egress("https://api.acme.com/orders", "POST", POLICY)  # allowed by POLICY only

    # ...and the inverse direction: a restrictive ambient file cannot block a
    # request the injected policy allows.
    (hostile_ambient / "rya.guard.yaml").write_text("ssrf: true\ndefault: deny\nrules: []\n")
    check_egress("https://api.acme.com/orders", "POST", POLICY)


def test_explicit_store_and_path_sources_beat_cwd_and_env(hostile_ambient):
    store = _FakeStore(_envelope(POLICY, "v1"))
    with pytest.raises(RyaError):
        check_egress("https://elsewhere.com/x", "POST", store)
    check_egress("https://api.acme.com/x", "GET", store)

    strict = hostile_ambient / "strict.guard.yaml"
    strict.write_text("ssrf: true\ndefault: deny\nrules: []\n")
    with pytest.raises(RyaError):
        check_egress("https://api.acme.com/x", "GET", str(strict))


def test_resolved_policy_carries_provenance(hostile_ambient):
    store = _FakeStore(_envelope(POLICY, "v7"))
    gp = resolve_policy(store)
    assert gp.source == "store" and gp.version == "v7" and gp.etag and gp.error is None
    assert resolve_policy(POLICY).source == "explicit"
    assert resolve_policy(str(hostile_ambient / "rya.guard.yaml")).source.startswith("file:")
    assert resolve_policy(None).source.startswith("file:")   # LEGACY ambient fallback

    # A verdict can be attributed to the policy that produced it — that is what
    # makes "who changed this allowlist" answerable after the fact (§12 risk 7).
    verdict = evaluate("https://webhook.site/abc", "POST", gp)
    assert verdict["decision"] == "block"
    assert verdict["policy"]["source"] == "store" and verdict["policy"]["version"] == "v7"


def test_two_tenants_do_not_share_a_compiled_rule_cache():
    # Same *version string*, different rules: the compiled cache must key on the
    # policy CONTENT, because two tenants both calling their policy "1" is normal.
    a = _FakeStore(_envelope({"ssrf": False, "default": "deny", "rules": [
        {"action": "allow", "kind": "glob", "pattern": "https://a.example.com/*"}]}, "1"))
    b = _FakeStore(_envelope({"ssrf": False, "default": "deny", "rules": [
        {"action": "allow", "kind": "glob", "pattern": "https://b.example.com/*"}]}, "1"))

    for _ in range(3):  # interleaved, so a stale cache would surface
        assert evaluate("https://a.example.com/x", "GET", resolve_policy(a))["decision"] == "allow"
        assert evaluate("https://a.example.com/x", "GET", resolve_policy(b))["decision"] == "block"
        assert evaluate("https://b.example.com/x", "GET", resolve_policy(b))["decision"] == "allow"
        assert evaluate("https://b.example.com/x", "GET", resolve_policy(a))["decision"] == "block"
    assert resolve_policy(a).etag != resolve_policy(b).etag


def test_two_tenants_do_not_share_a_compiled_secrecy_cache():
    # The old secrecy cache was keyed on `id()` of the policy's `secrecy` sub-dict —
    # an address, not a version. Same version string + same pattern + different
    # replacement token is the case that catches it.
    def pol(token):
        return {"secrecy": {"enabled": True, "patterns": [
            {"id": "mid", "kind": "regex", "pattern": r"\b[A-Za-z]{3,8}\d{8,}\b",
             "replacement": token}]}}

    a = _FakeStore(_envelope(pol("(A hidden)"), "1"))
    b = _FakeStore(_envelope(pol("(B hidden)"), "1"))
    for _ in range(3):
        assert secrecy_check("id IjmQ1782803306", a)["scrubbed"] == "id (A hidden)"
        assert secrecy_check("id IjmQ1782803306", b)["scrubbed"] == "id (B hidden)"
    assert secrecy_scrub({"m": "IjmQ1782803306"}, a) == {"m": "(A hidden)"}
    assert secrecy_scrub({"m": "IjmQ1782803306"}, b) == {"m": "(B hidden)"}


def test_unreadable_store_policy_fails_closed(monkeypatch, tmp_path):
    # D7: a governance policy that EXISTS but cannot be read must DENY. A guard
    # that silently switches itself off when its source breaks is not a guard.
    monkeypatch.setenv("RYA_GUARD_PATH", str(tmp_path / "missing.yaml"))
    broken = _FakeStore(raises=RuntimeError("connection reset"))
    gp = resolve_policy(broken)
    assert gp.enforced is True and gp.error and "connection reset" in gp.error
    for url in ("https://api.acme.com/orders", "https://anything.com/x"):
        with pytest.raises(RyaError) as e:
            check_egress(url, "POST", broken)
        assert e.value.code == "E_EGRESS_BLOCKED"
        assert "failing closed" in str(e.value)

    # A corrupt record is the same failure, not a no-op.
    for bad in ("not a mapping", {"version": "1", "policy": "nope"}):
        with pytest.raises(RyaError):
            check_egress("https://api.acme.com/orders", "POST", _FakeStore(bad))

    # ...whereas ABSENCE stays a no-op, which is what keeps the guard opt-in.
    assert resolve_policy(_FakeStore(None)).enforced is False
    check_egress("https://anything.com/x", "POST", _FakeStore(None))   # must not raise
    assert load_policy(source=_FakeStore(None)) is None


def test_absent_file_no_ops_but_broken_file_fails_closed(tmp_path):
    assert resolve_policy(str(tmp_path / "nope.yaml")).source == "none"
    check_egress("https://anything.com/x", "POST", str(tmp_path / "nope.yaml"))

    broken = tmp_path / "broken.guard.yaml"
    broken.write_text("default: deny\nrules: [unclosed\n")
    with pytest.raises(RyaError):
        check_egress("https://anything.com/x", "POST", str(broken))


def test_store_without_the_protocol_degrades_to_the_file_fallback(tmp_path, monkeypatch):
    guard = tmp_path / "rya.guard.yaml"
    guard.write_text("ssrf: false\ndefault: deny\nrules: []\n")
    monkeypatch.setenv("RYA_GUARD_PATH", str(guard))

    class _OldStore:                    # no policy_get/policy_set at all
        pass

    gp = resolve_policy(_OldStore())
    assert gp.source == f"file:{guard}"
    with pytest.raises(RyaError):
        check_egress("https://anything.com/x", "POST", _OldStore())


def test_file_fallback_still_hot_reloads_without_relying_on_mtime(tmp_path):
    guard = tmp_path / "rya.guard.yaml"
    guard.write_text("ssrf: false\ndefault: deny\nrules: []\n")
    with pytest.raises(RyaError):
        check_egress("https://api.acme.com/x", "POST", str(guard))

    # Rewrite and force the mtime back to what it was: the cache is keyed on the
    # policy's content etag, so the edit still takes effect. The old mtime cache
    # would have served the stale policy here.
    before = os.stat(guard)
    guard.write_text("ssrf: false\ndefault: allow\nrules: []\n")
    os.utime(guard, (before.st_atime, before.st_mtime))
    check_egress("https://api.acme.com/x", "POST", str(guard))       # must not raise


def test_audit_record_from_a_store_write():
    store = _FakeStore()
    first = save_policy(POLICY, source=store, actor="alice@acme")

    assert first["key"] == "guard"
    assert first["previousVersion"] is None
    assert first["actor"] == "alice@acme"
    assert first["changedAt"].endswith("Z")
    assert first["policy"]["default"] == "deny" and first["policy"]["fail"] == "closed"
    assert len(first["diff"]["added"]) == 3 and first["diff"]["removed"] == []
    json.dumps(first)                       # the value written must be JSON-able
    assert store.writes == [("guard", first, "alice@acme")]

    # A second write diffs against the version it replaces.
    loosened = {**POLICY, "default": "allow", "rules": POLICY["rules"][:2]}
    second = save_policy(loosened, source=store, actor="bob@acme")
    assert second["previousVersion"] == first["version"]
    assert second["version"] != first["version"]
    assert second["diff"]["removed"] == ["deny glob https://webhook.site/* [*]"]
    assert second["diff"]["added"] == [] and "default" in second["diff"]["changed"]

    # ...and the store now resolves to exactly that version.
    assert resolve_policy(store).version == second["version"]
    assert evaluate("https://elsewhere.com/x", "POST", resolve_policy(store))["decision"] == "allow"


def test_audit_record_from_a_file_write_and_a_legacy_store(tmp_path):
    rec = save_policy(POLICY, str(tmp_path / "rya.guard.yaml"), actor="carol")
    assert rec["path"] == str(tmp_path / "rya.guard.yaml")
    assert rec["actor"] == "carol" and rec["previousVersion"] is None
    assert (tmp_path / "rya.guard.yaml").exists()

    class _NoActorStore(_FakeStore):
        def policy_set(self, key, value):        # predates the `actor` argument
            self.writes.append((key, value, None))
            self.record = value

    store = _NoActorStore()
    rec = save_policy(POLICY, source=store, actor="dave")
    assert rec["actor"] == "dave"                 # actor survives inside the record
    assert len(store.writes) == 1                 # and is not written twice


def test_grounding_policy_reads_the_injected_source(tmp_path):
    store = _FakeStore(_envelope({"grounding": {"enabled": True}}, "1"))
    assert grounding_policy(store) == {"enabled": True}
    assert grounding_policy({"rules": []}) == {}
    assert grounding_policy(str(tmp_path / "nope.yaml")) == {}
    # An unreadable policy reports the gate as ON — failing closed here means
    # checking, not skipping.
    assert grounding_policy(_FakeStore(raises=RuntimeError("boom")))["enabled"] is True


def test_run_tests_accepts_any_source_and_reports_provenance():
    store = _FakeStore(_envelope(POLICY, "v9"))
    rep = run_tests(store)
    assert rep["accuracy"] == 100 and rep["policy"]["version"] == "v9"
    assert run_tests(GuardPolicy(**{**resolve_policy(POLICY).__dict__}))["accuracy"] == 100


# ---- API -----------------------------------------------------------------
def test_guard_api(tmp_path, monkeypatch):
    for k in ("RYA_TOKEN", "RYA_MULTITENANT", "RYA_DATABASE_URL", "RYA_GUARD_PATH"):
        monkeypatch.delenv(k, raising=False)
    scaffold.write_project(tmp_path, "guard-agent", template="demo")  # writes a default rya.guard.yaml
    c = TestClient(build_app(tmp_path))

    g = c.get("/guard").json()
    assert g["exists"] is True
    assert any(r["pattern"].startswith("https://api.anthropic") for r in g["policy"]["rules"])
    assert g["tests"]["accuracy"] == 100

    new = {"ssrf": True, "default": "deny", "fail": "closed", "rules": [
        {"action": "deny", "kind": "glob", "pattern": "https://evil.com/*", "note": "blocked"}]}
    r = c.put("/guard", json={"policy": new})
    assert r.status_code == 200
    assert (tmp_path / "rya.guard.yaml").exists()
    assert c.get("/guard").json()["policy"]["rules"][0]["pattern"] == "https://evil.com/*"
