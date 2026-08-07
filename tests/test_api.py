"""Live HTTP surface: webhook trigger + token auth + signature verification."""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from rya.api.app import build_app
from rya.cli import scaffold


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("RYA_TOKEN", raising=False)
    monkeypatch.delenv("RYA_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("RYA_DATABASE_URL", raising=False)
    scaffold.write_project(tmp_path, "api-agent", template="demo")
    return TestClient(build_app(tmp_path)), tmp_path, monkeypatch


def test_healthz_is_public(client):
    c, _, _ = client
    r = c.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_webhook_triggers_real_run(client):
    c, _, _ = client
    r = c.post("/inbound", json={"email": "ada@example.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "waiting_approval"
    assert body["pendingApproval"]


def test_webhook_to_approval_over_http(client):
    c, _, _ = client
    run = c.post("/inbound", json={"email": "ada@example.com"}).json()
    apr = run["pendingApproval"]
    # No token set -> control routes are open.
    done = c.post(f"/approvals/{apr}/approve")
    assert done.status_code == 200 and done.json()["runStatus"] == "completed"


def test_token_required_when_set(client):
    c, _, monkeypatch = client
    monkeypatch.setenv("RYA_TOKEN", "secret-token")
    # Control route without token -> 401. `_` is the sole-agent alias; a made-up
    # id would now 404 on its own (D21), which would not prove anything about auth.
    assert c.get("/agents/_").status_code == 401
    # With wrong token -> 401.
    assert c.get("/agents/_", headers={"Authorization": "Bearer nope"}).status_code == 401
    # With correct token -> 200.
    ok = c.get("/agents/_", headers={"Authorization": "Bearer secret-token"})
    assert ok.status_code == 200
    # Webhook stays public (signature layer is separate).
    assert c.post("/inbound", json={"email": "a@b.com"}).status_code == 200


def test_webhook_signature_enforced(client):
    c, _, monkeypatch = client
    monkeypatch.setenv("RYA_WEBHOOK_SECRET", "whsec")
    raw = json.dumps({"email": "ada@example.com"}).encode()
    good = "sha256=" + hmac.new(b"whsec", raw, hashlib.sha256).hexdigest()

    # No signature -> 401.
    assert c.post("/inbound", content=raw).status_code == 401
    # Bad signature -> 401.
    assert c.post("/inbound", content=raw, headers={"X-Rya-Signature": "sha256=bad"}).status_code == 401
    # Correct signature -> 200.
    ok = c.post("/inbound", content=raw, headers={"X-Rya-Signature": good})
    assert ok.status_code == 200 and ok.json()["status"] == "waiting_approval"


def test_a_ryaerror_escaping_a_route_is_an_envelope_not_a_500(client):
    """`POST /agents/{id}/events` has no local try/except, so before the global
    handler existed a guard block or a quota refusal escaped as a bare
    `500 Internal Server Error` with no envelope — losing the stable code exactly
    where a client most needs it to branch. Every route is now backstopped.
    """
    c, root, _ = client
    from rya.quotas import set_quota
    from rya.store import Store

    store = Store(root)
    store.ensure()
    set_quota(store, {"maxConcurrentRuns": 0})  # admit nothing

    r = c.post("/agents/api-agent/events",
               json={"type": "message.received", "payload": {"email": "ada@example.com"}})
    assert r.status_code == 429, r.text          # not 500, and not 400
    body = r.json()
    # ONE envelope, everywhere: `{ok: false, error: {code, message, hint, exit_code}}`
    # — identical to `RyaError.to_dict()`, to `rya … --json`, to an MCP reply. It used
    # to be three shapes depending on how the failure was raised, which is why the
    # console rendered most of them as a bare "HTTP 400".
    assert body["ok"] is False
    assert body["error"]["code"] == "E_QUOTA_EXCEEDED"
    assert body["error"]["hint"]                 # tells the caller what to do next


def test_a_local_handler_still_wins_over_the_global_backstop(client):
    """The backstop must not flatten routes that deliberately choose a status."""
    c, _, _ = client
    r = c.get("/runs/run_does_not_exist")
    assert r.status_code == 404


# ---- the error envelope -----------------------------------------------------
#
# There is exactly ONE shape for a failure, and these tests are the contract:
#
#     {"ok": false, "error": {"code": "E_*", "message", "hint", "exit_code"}}
#
# Identical to `RyaError.to_dict()`, to `rya <cmd> --json` on failure
# (docs/devex.md), to an MCP tool reply and to a broker reply. Before it was
# unified the api emitted three shapes — `{"detail": {...}}` from
# `HTTPException`, the bare error object from the `RyaError` handler, and
# `{"detail": [{loc, msg}]}` from FastAPI's validator. No client can read three
# envelopes with one expression, so the console read one of them and rendered
# the other two as the literal string "HTTP 400", losing `message` and `hint`
# for the whole quota, governance, versioning and agent-addressing vocabulary.


def _assert_envelope(r, code=None, *, status=None):
    """Every failure, whatever raised it, answers this shape."""
    if status is not None:
        assert r.status_code == status, r.text
    assert r.status_code >= 400, r.text
    body = r.json()
    assert body["ok"] is False, body
    err = body["error"]
    # No stray top-level keys: `detail` in particular must be gone, or a client
    # written against the old shape would silently keep reading the wrong field.
    assert set(body) == {"ok", "error"}, body
    assert set(err) >= {"code", "message", "hint", "exit_code"}, err
    assert isinstance(err["code"], str) and err["code"].startswith("E_"), err
    assert isinstance(err["message"], str) and err["message"], err
    assert isinstance(err["exit_code"], int), err
    if code is not None:
        assert err["code"] == code, err
    return err


def test_a_ryaerror_is_the_envelope(client):
    """The `RyaError` handler — the shape that used to be emitted bare."""
    c, _, _ = client
    err = _assert_envelope(c.get("/agents/nope/guard"), "E_AGENT_NOT_FOUND", status=404)
    assert "nope" in err["message"]
    assert err["hint"]


def test_an_httpexception_is_the_same_envelope(client, monkeypatch):
    """`HTTPException(detail={...})` — 66 raise sites, none of which changed."""
    monkeypatch.setenv("RYA_TOKEN", "secret")
    c = TestClient(build_app(client[1]))
    err = _assert_envelope(c.get("/console"), "E_UNAUTHORIZED", status=401)
    assert err["hint"]


def test_starlettes_own_404_is_the_same_envelope(client):
    """A route that does not exist never had a code at all; now it has one.

    Deliberately the generic noun — a bare 404 could be a run, job, version or
    session, and guessing `E_RUN_NOT_FOUND` sends a caller hunting in the wrong
    place. Mirrors `inferCode` in clients/typescript/src/errors.ts."""
    c, _, _ = client
    _assert_envelope(c.get("/no/such/route"), "E_NOT_FOUND", status=404)


def test_a_validation_failure_is_the_same_envelope(client):
    """FastAPI's `{"detail": [{loc, msg, type}]}` array.

    Left as an array it reached the console's `body.detail.message ||
    body.detail` and rendered as the literal string `[object Object]`."""
    c, _, _ = client
    err = _assert_envelope(c.get("/agents/api-agent/guard/log?limit=abc"),
                           "E_VALIDATION", status=422)
    # Flattened to prose that names the offending parameter.
    assert "limit" in err["message"]


def test_the_envelope_carries_a_semantic_exit_code(client):
    """`exit_code` is not decoration: `docs/devex.md` documents branching on it,
    and it is what makes the HTTP body byte-identical to `rya … --json`."""
    from rya.errors import EXIT_NOT_FOUND, EXIT_VALIDATION

    c, _, _ = client
    assert _assert_envelope(c.get("/no/such/route"))["exit_code"] == EXIT_NOT_FOUND
    assert _assert_envelope(
        c.get("/agents/api-agent/guard/log?limit=abc"))["exit_code"] == EXIT_VALIDATION


def test_an_httpexception_keeps_its_response_headers(client, monkeypatch):
    """Rewrapping the body must not drop headers — Starlette's own 405 carries
    `Allow`, and a 401 may carry `WWW-Authenticate`."""
    monkeypatch.setenv("RYA_TOKEN", "secret")
    c = TestClient(build_app(client[1]))
    r = c.request("DELETE", "/healthz")
    _assert_envelope(r, status=405)
    assert "Allow" in r.headers


# ---- paged listings (audit §5.1, §5.2) --------------------------------------
# The console's Runs and Conversations tables used to filter and search inside
# `/console`'s fixed-size dashboard PREVIEW (newest 30 runs, newest 50 sessions),
# which is why a search for an older run id answered "No runs match" about a run
# that exists. These pin the window, the count, the filter and — the part a client
# cannot check for itself — that `count` describes the filtered SET and not the page.


def _seed_runs(root, agent, specs):
    """Write run documents straight to the store.

    Deliberately not via `/inbound`: these tests need chosen ids, statuses,
    triggers and an ordering, and a real dispatch gives one status.
    """
    from rya.store import Store

    store = Store(root)
    store.ensure()
    for i, (rid, status, trigger) in enumerate(specs):
        store.save_run({"id": rid, "agent": agent, "status": status, "trigger": trigger,
                        # Descending ids sort descending by createdAt, so "newest
                        # first" is checkable rather than incidental.
                        "createdAt": f"2026-08-0{1 + i // 24}T{i % 24:02d}:00:00Z",
                        "trace": [{"kind": "run.started", "label": rid}]})
    return store


def _seed_sessions(root, agent, n):
    from rya.store import Store

    store = Store(root)
    store.ensure()
    made = []
    for i in range(n):
        s = store.create_session(agent, "web", f"ext-{i:03d}", title=f"Conversation {i:03d}")
        # create_session stamps `now`, so every row would share a timestamp and the
        # order would be arbitrary. Spread them to make the window deterministic.
        doc = store.get_session(s["id"])
        doc["lastMessageAt"] = f"2026-08-01T{i % 24:02d}:{i // 24:02d}:00Z"
        store._write(store._session_path(s["id"]), doc)
        made.append(s["id"])
    return made


def test_runs_list_without_paging_params_is_the_old_contract(client):
    """`rya runs list` and the TypeScript SDK read this unparameterised.

    Paging had to be additive: full documents, traces included, no window.
    """
    c, root, _ = client
    _seed_runs(root, "api-agent", [(f"run_{i:03d}", "completed", "message.received")
                                   for i in range(3)])
    body = c.get("/agents/api-agent/runs").json()
    assert len(body["runs"]) == 3
    assert body["runs"][0]["trace"], "the unparameterised list still carries traces"
    assert body["count"] == 3
    # No window was asked for, so none is reported — `null`, not a default that
    # would silently truncate an existing caller.
    assert body["limit"] is None and body["offset"] == 0


def test_runs_page_windows_while_count_stays_the_whole_set(client):
    c, root, _ = client
    _seed_runs(root, "api-agent", [(f"run_{i:03d}", "completed", "message.received")
                                   for i in range(12)])

    first = c.get("/agents/api-agent/runs?limit=5&summary=1").json()
    assert [r["id"] for r in first["runs"]] == [f"run_{i:03d}" for i in (11, 10, 9, 8, 7)]
    assert first["count"] == 12, "count is the filtered set, not the page"

    second = c.get("/agents/api-agent/runs?limit=5&offset=5&summary=1").json()
    assert [r["id"] for r in second["runs"]] == [f"run_{i:03d}" for i in (6, 5, 4, 3, 2)]
    assert second["count"] == 12

    # Past the end is an empty page, not an error and not a wrapped-around one.
    beyond = c.get("/agents/api-agent/runs?limit=5&offset=999&summary=1").json()
    assert beyond["runs"] == [] and beyond["count"] == 12


def test_runs_page_filters_by_status_and_counts_only_matches(client):
    """The console's status pills and its row count must describe the same set."""
    c, root, _ = client
    _seed_runs(root, "api-agent",
               [("run_a", "completed", "message.received"),
                ("run_b", "failed", "message.received"),
                ("run_c", "failed", "cron.tick"),
                ("run_d", "waiting_approval", "message.received")])
    body = c.get("/agents/api-agent/runs?status=failed&summary=1").json()
    assert {r["id"] for r in body["runs"]} == {"run_b", "run_c"}
    assert body["count"] == 2


def test_runs_page_search_matches_id_or_trigger_case_insensitively(client):
    """A faithful port of the client-side filter this replaces (`filterRuns`)."""
    c, root, _ = client
    _seed_runs(root, "api-agent",
               [("run_alpha", "completed", "message.received"),
                ("run_beta", "completed", "cron.tick"),
                ("run_gamma", "failed", "webhook.inbound")])

    by_id = c.get("/agents/api-agent/runs?q=ALPHA&summary=1").json()
    assert [r["id"] for r in by_id["runs"]] == ["run_alpha"] and by_id["count"] == 1

    by_trigger = c.get("/agents/api-agent/runs?q=cron&summary=1").json()
    assert [r["id"] for r in by_trigger["runs"]] == ["run_beta"]

    # Status AND query compose, and the count follows both.
    both = c.get("/agents/api-agent/runs?status=failed&q=run_&summary=1").json()
    assert [r["id"] for r in both["runs"]] == ["run_gamma"] and both["count"] == 1

    # Whitespace-only is not a filter (the console sends what was typed).
    assert c.get("/agents/api-agent/runs?q=%20%20&summary=1").json()["count"] == 3


def test_summary_rows_drop_the_trace_and_match_the_console_preview(client):
    """One projection, shared: the paged row and `/console`'s preview row are the
    same function, so a table and the totals beside it cannot drift."""
    c, root, _ = client
    _seed_runs(root, "api-agent", [("run_only", "completed", "message.received")])

    row = c.get("/agents/api-agent/runs?limit=1&summary=1").json()["runs"][0]
    assert "trace" not in row, "a 50-row page of documents is megabytes of trace"
    assert set(row) == {"id", "status", "trigger", "createdAt", "pendingApproval",
                        "error", "traceLength", "tokens", "costUsd"}
    assert row["traceLength"] == 1
    assert row == c.get("/console").json()["runs"][0]


def test_runs_page_limit_is_clamped(client):
    """`?limit=1000000&summary=1` must not be a way to make one request project
    every run in the workspace."""
    c, root, _ = client
    _seed_runs(root, "api-agent", [("run_a", "completed", "message.received")])
    assert c.get("/agents/api-agent/runs?limit=1000000").json()["limit"] == 500
    assert c.get("/agents/api-agent/runs?limit=0").json()["limit"] == 1
    assert c.get("/agents/api-agent/runs?limit=-5").json()["limit"] == 1
    assert c.get("/agents/api-agent/runs?offset=-5").json()["offset"] == 0


def test_a_search_reaches_past_the_console_preview(client):
    """Audit §5.1, executable.

    35 runs: `/console` ships the newest 30, so the oldest is not in it. Searching
    for that id used to answer "No runs match" because the console filtered inside
    the preview. The paged route finds it.
    """
    c, root, _ = client
    _seed_runs(root, "api-agent", [(f"run_{i:03d}", "completed", "message.received")
                                   for i in range(35)])

    preview = c.get("/console").json()
    assert len(preview["runs"]) == 30
    assert preview["stats"]["runs"] == 35, "the totals were always honest"
    assert "run_000" not in {r["id"] for r in preview["runs"]}

    found = c.get("/agents/api-agent/runs?q=run_000&summary=1&limit=50").json()
    assert [r["id"] for r in found["runs"]] == ["run_000"] and found["count"] == 1


def test_sessions_list_without_paging_params_is_the_old_contract(client):
    c, root, _ = client
    _seed_sessions(root, "api-agent", 3)
    body = c.get("/agents/api-agent/sessions").json()
    assert len(body["sessions"]) == 3 and body["count"] == 3
    assert body["limit"] is None and body["offset"] == 0
    # The transcript is never in a listing — that is `GET /sessions/{id}`.
    assert all("messages" not in s for s in body["sessions"])


def test_conversation_fifty_one_is_reachable(client):
    """Audit §5.2: `/console` caps the preview at 50, so sessions 51+ could not be
    opened from the console at all."""
    c, root, _ = client
    ids = _seed_sessions(root, "api-agent", 55)

    preview = c.get("/console").json()
    assert len(preview["sessions"]) == 50 and preview["stats"]["sessions"] == 55

    page = c.get("/agents/api-agent/sessions?limit=50&offset=50").json()
    assert len(page["sessions"]) == 5 and page["count"] == 55
    assert set(ids) >= {s["id"] for s in page["sessions"]}
    # Newest-first ordering is preserved across the window, so page 2 is the tail
    # rather than an arbitrary five rows.
    assert {s["id"] for s in page["sessions"]}.isdisjoint(
        {s["id"] for s in preview["sessions"]})


def test_the_unprefixed_sessions_spelling_pages_identically(client):
    """The prefixed/unprefixed pair share one implementation on purpose: a window
    that existed on only one of them is exactly how the two drift."""
    c, root, _ = client
    _seed_sessions(root, "api-agent", 4)
    prefixed = c.get("/agents/api-agent/sessions?limit=2").json()
    plain = c.get("/sessions?limit=2").json()
    assert prefixed["count"] == plain["count"] == 4
    assert [s["id"] for s in prefixed["sessions"]] == [s["id"] for s in plain["sessions"]]


def test_posture_names_its_conditions_over_http(client):
    """Audit §5.7: the console used to hold its own list of the launch gate's
    conditions — three of them — and could not show the fourth (D32) at all, while the
    `ok`/`unmet` it rendered beside them did count it. The route sends the list, in the
    order the gate states it, so a console renders what the gate has rather than what it
    was compiled believing the gate had."""
    c, _, _ = client
    body = c.get("/posture").json()

    assert [x["key"] for x in body["conditions"]] == [
        "isolation", "broker", "egress", "topology"]
    for cond in body["conditions"]:
        assert cond["label"] and cond["detail"] and isinstance(cond["ok"], bool)
        # The flat per-condition keys are still the contract for a reader that wants
        # ONE of them, and they are the same facts.
        assert body[cond["key"]] == {"ok": cond["ok"], "detail": cond["detail"]}
    # `unmet` is the prose for exactly the conditions that are not in force, so the
    # summary and the rows on the page cannot contradict each other.
    assert len(body["unmet"]) == len([x for x in body["conditions"] if not x["ok"]])
    assert body["ok"] is (not body["unmet"])
