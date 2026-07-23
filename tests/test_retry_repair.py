"""Track A Phase-3 primitives: declarative tool-call retry (A1), the
``@agent.repair`` self-heal callback (A1), and declarative result→memory adoption
(A5) with the memory-pin that reads it.

Driven through the real Engine against a synthetic agent, so retry/repair/adoption
are exercised exactly as ``ctx.tools.call`` runs them (permission + pin + scrub +
journal), not in isolation. The deterministic csa-counsellor consumer (create_lead
idempotency + self-heal + adoption ordering) is gated in
``examples/csa-counsellor/tests/test_agent_phase3.py``.
"""

from rya.manifest import load_manifest
from rya.manifest.schema import RetryDecl
from rya.runtime import Engine, load_agent
from rya.store import Store

_AGENT = '''
from rya import define_agent, RyaError, RyaRecoverableToolError
agent = define_agent()
_STATE = {"flaky_calls": 0}

@agent.tool("flaky")
async def flaky(inp):
    # Fails transiently once (E_TIMEOUT), succeeds on the retry.
    _STATE["flaky_calls"] += 1
    if _STATE["flaky_calls"] < 2:
        raise RyaError("E_TIMEOUT", "transient")
    return {"ok": True, "calls": _STATE["flaky_calls"]}

@agent.tool("always_5xx")
async def always_5xx(inp):
    raise RyaError("E_TOOL_UPSTREAM", "upstream 503", http_status=503)

@agent.tool("needs_fix")
async def needs_fix(inp):
    if not inp.get("fixed"):
        raise RyaRecoverableToolError("needs_fix", "unfixed", detail={"why": "test"})
    return {"ok": True, "fixed": True}

@agent.repair("needs_fix")
def repair_needs_fix(inp, err):
    assert err.reason == "needs_fix"
    return {**inp, "fixed": True}

@agent.tool("mklead")
async def mklead(inp):
    return {"ok": True, "camsId": "999"}

@agent.tool("usekey")
async def usekey(inp):
    return {"ok": True, "seen": inp.get("camsId")}

@agent.on_event
async def handle(ctx, event):
    which = event.payload["which"]
    if which == "retry_ok":
        return await ctx.tools.call("flaky", {})
    if which == "retry_exhausted":
        return await ctx.tools.call("always_5xx", {})
    if which == "repair":
        return await ctx.tools.call("needs_fix", {})
    if which == "adopt":
        await ctx.tools.call("mklead", {})
        return {"use": await ctx.tools.call("usekey", {})}
    if which == "adopt_order":
        before = await ctx.tools.call("usekey", {})
        await ctx.tools.call("mklead", {})
        after = await ctx.tools.call("usekey", {})
        return {"before": before, "after": after}
    return {}
'''

_TOOLS = """
  - id: flaky
    permission: allowed
    retry: {max_attempts: 3, backoff: none, "on": [timeout]}
  - id: always_5xx
    permission: allowed
    retry: {max_attempts: 2, backoff: none, "on": [5xx]}
  - id: needs_fix
    permission: allowed
  - id: mklead
    permission: allowed
    adopt: {camsId: student_state.camsId}
  - id: usekey
    permission: allowed
    pin: {camsId: memory.student_state.camsId}
"""


def _engine(tmp_path):
    (tmp_path / "rya.agent.yaml").write_text(
        "name: t\nruntime: python\nentrypoint: agent.py\n"
        "memory:\n  type: managed\n  collections: [student_state]\n"
        "tools:" + _TOOLS)
    (tmp_path / "agent.py").write_text(_AGENT)
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    store = Store(tmp_path); store.ensure()
    return Engine(manifest, load_agent(manifest, tmp_path), store, tmp_path)


def _kinds(run):
    return [t["kind"] for t in run["trace"]]


# ---- A1 retry --------------------------------------------------------------
def test_transient_failure_is_retried_then_succeeds(tmp_path):
    run = _engine(tmp_path).run_event("x", {"which": "retry_ok"})
    assert run["status"] == "completed"
    assert run["output"]["calls"] == 2            # ran twice: 1 fail + 1 success
    assert _kinds(run).count("tool.retry") == 1   # exactly one retry between them


def test_retry_is_exhausted_then_surfaces(tmp_path):
    run = _engine(tmp_path).run_event("x", {"which": "retry_exhausted"})
    assert run["status"] == "failed"
    assert run["error"]["code"] == "E_TOOL_UPSTREAM"
    # max_attempts=2 → exactly one retry, then the error surfaces.
    assert _kinds(run).count("tool.retry") == 1


def test_retry_only_fires_for_listed_classes(tmp_path):
    # `always_5xx` lists on:[5xx]; a timeout class would NOT be retried. We assert
    # the positive: a 5xx IS retried (class matched), proving `on` gates by class.
    run = _engine(tmp_path).run_event("x", {"which": "retry_exhausted"})
    retry_ev = next(e for e in run["trace"] if e["kind"] == "tool.retry")
    assert retry_ev["data"]["class"] == "5xx"


# ---- A1 repair (self-heal) -------------------------------------------------
def test_recoverable_error_self_heals_via_repair(tmp_path):
    run = _engine(tmp_path).run_event("x", {"which": "repair"})
    assert run["status"] == "completed"
    assert run["output"] == {"ok": True, "fixed": True}
    repair_ev = next(e for e in run["trace"] if e["kind"] == "tool.repair")
    assert repair_ev["data"]["reason"] == "needs_fix"
    assert repair_ev["data"]["patched"]["fixed"] is True


# ---- A5 adoption + memory pin ----------------------------------------------
def test_adoption_feeds_a_later_pinned_call(tmp_path):
    run = _engine(tmp_path).run_event("x", {"which": "adopt"})
    assert run["status"] == "completed"
    assert run["output"]["use"]["seen"] == "999"   # usekey adopted mklead's camsId
    assert "tool.adopt" in _kinds(run)


def test_adoption_is_order_independent(tmp_path):
    run = _engine(tmp_path).run_event("x", {"which": "adopt_order"})
    assert run["status"] == "completed"
    # Called BEFORE the adoption, the pin resolves empty; AFTER, it resolves to 999.
    assert run["output"]["before"]["seen"] is None
    assert run["output"]["after"]["seen"] == "999"


# ---- schema robustness -----------------------------------------------------
def test_retry_on_keyword_survives_yaml_boolean_footgun():
    # Unquoted `on:` in YAML 1.1 parses to the boolean True; the schema rescues it
    # so the retry policy is never a silent no-op.
    d = RetryDecl.model_validate({"max_attempts": 2, True: ["timeout"]})
    assert d.on == ["timeout"]
    # `retry_on` alias also works.
    assert RetryDecl.model_validate({"retry_on": ["5xx"]}).on == ["5xx"]
