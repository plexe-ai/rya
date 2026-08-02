"""The credential boundary (D18, issue #11) — the broker.

These run a real socket, a real spawned template and a real fork, because the
property under test is where a credential *is*, and a mocked transport can only
assert the shape of code that was written to have that shape.

The load-bearing assertions are :func:`test_a_mediated_child_holds_no_database_credential`
and :func:`test_the_broker_refuses_a_run_this_dispatch_does_not_own`. Everything else
could pass with the boundary leaking.
"""

import json
import os
import socket
import time
from types import SimpleNamespace

import pytest

from rya import bundles, deployments
from rya.broker import protocol as proto
from rya.broker.client import BrokerClient, BrokerStore
from rya.broker.inventory import (
    CLASS_AMBIGUOUS,
    CLASS_PLATFORM,
    CLASS_TENANT,
    classify,
    scrub_environment,
    take_inventory,
)
from rya.broker.server import BrokerServer, _redact_connection
from rya.cli import scaffold
from rya.errors import RyaError
from rya.execution.pool import FORK_AVAILABLE
from rya.store import Store
from rya.worker import start_worker

ENTRYPOINT = '''
import os
from rya import define_agent

agent = define_agent()


@agent.on_event
async def main(ctx, event):
    """Reports on the process it runs in, which is what the tests read."""
    store = ctx.store
    return {
        "seen": event.payload.get("v"),
        "pid": os.getpid(),
        # What kind of store is the tenant actually holding?
        "storeKind": (store.describe() or {}).get("kind"),
        # Are the platform's credentials in this process's environment?
        "dsn": os.environ.get("RYA_DATABASE_URL"),
        "sealKey": os.environ.get("RYA_SECRET_KEY"),
        "providerKey": os.environ.get("ANTHROPIC_API_KEY"),
        "bucketKey": os.environ.get("AWS_SECRET_ACCESS_KEY"),
        # Is the withheld surface actually absent, not merely refused?
        "hasMeter": hasattr(store, "meter_append"),
        "hasWorkerList": hasattr(store, "worker_list"),
        "hasPolicySet": hasattr(store, "policy_set"),
    }
'''


def _project(tmp_path, name="brokered", entrypoint=ENTRYPOINT):
    root = tmp_path / name
    scaffold.write_project(root, name, template="demo")
    (root / "src" / "agent.py").write_text(entrypoint)
    return root


def _published(tmp_path, **kw):
    root = _project(tmp_path, **kw)
    store = Store(root / ".rya")
    store.ensure()
    bundle = bundles.build_bundle(root)
    bundles.store_bundle(bundle, bundles.default_archive_root(root))
    version = deployments.create_version(store, agent=bundle.agent, bundle=bundle)
    return root, store, version


def _source(store, version, agent="brokered"):
    from rya import turns

    return turns.TurnSource(store=store,
                            manifest=SimpleNamespace(name=agent, version="0.1.0"),
                            version=version)


@pytest.fixture()
def served(tmp_path):
    """A running broker over a real FileStore, plus a dispatch capability."""
    store = Store(tmp_path / ".rya")
    store.ensure()
    server = BrokerServer(store=store, project_root=tmp_path,
                          workspace="ws_test", agent="brokered",
                          body_timeout=1.0)
    server.start()
    try:
        yield server, store
    finally:
        server.close()


def _client(server, **kw):
    return BrokerClient(server.socket_path, server.mint(dispatch="d1", **kw))


# ---- the allowlist ---------------------------------------------------------

def test_the_allowlist_refuses_by_default_and_explains_a_known_method(served):
    """A denied method that EXISTS on the store gets a different message from a
    typo, because the two mean different things to whoever reads the error."""
    server, _store = served
    c = _client(server)
    try:
        with pytest.raises(RyaError) as e:
            c.call("worker_deregister", "w_1")
        assert e.value.code == "E_BROKER_METHOD_DENIED"
        assert "exists on the store but is not on the tenant-reachable allowlist" in e.value.message

        with pytest.raises(RyaError) as e2:
            c.call("nonsense_method")
        assert "exists on the store" not in e2.value.message
        assert "Allowlisted:" in (e2.value.hint or "")
    finally:
        c.close()


def test_metering_is_not_on_the_allowlist_because_the_billed_party_would_write_it(served):
    """D30's sharpest consequence. With a pooled provider key, a tenant that can
    append meter rows can write its own invoice."""
    server, _store = served
    assert "meter_append" not in proto.ALL_METHODS
    c = _client(server)
    try:
        with pytest.raises(RyaError) as e:
            c.call("meter_append", {"runId": "r_1", "inputTokens": -999999})
        assert e.value.code == "E_BROKER_METHOD_DENIED"
    finally:
        c.close()


def test_governance_writes_and_the_execution_plane_are_withheld(served):
    """One assertion per withheld group, so a future allowlist edit that reopens
    any of them fails here rather than in production."""
    server, _store = served
    c = _client(server)
    withheld = ["policy_set",          # governance the tenant would be governed by
                "worker_register",     # the registry the supervisor scales on
                "version_set_state",   # retiring a version stops sibling work
                "env_set_current",     # promoting is the control plane's
                "queue_reap",          # resetting leases affects siblings
                "queue_counts",        # sibling volume
                "queue_claim_one",     # subsumed by the claim SERVICE
                "list_runs",           # enumerating the tenant's other runs
                "journal_read",
                "reseal_connections"]  # touches the seal key
    try:
        for method in withheld:
            assert method not in proto.ALL_METHODS, method
            with pytest.raises(RyaError) as e:
                c.call(method)
            assert e.value.code == "E_BROKER_METHOD_DENIED", method
    finally:
        c.close()


def test_a_handshake_capability_authorises_nothing_on_the_data_surface(served):
    """The template holds this one, and the template has tenant code in it."""
    server, _store = served
    c = BrokerClient(server.socket_path, server.mint())  # no dispatch
    try:
        assert c.ping()["ok"] is True          # authentication works
        c.call("now_iso")                       # and the handshake set
        with pytest.raises(RyaError) as e:
            c.call("load_memory", "user:1")
        assert e.value.code == "E_BROKER_SCOPE_DENIED"
        assert "names none" in e.value.message
    finally:
        c.close()


# ---- capabilities ----------------------------------------------------------

def test_a_forged_capability_does_not_verify(served):
    server, _store = served
    real = server.mint(dispatch="d1")
    body, _, _mac = real.partition(".")
    forged = body + ".AAAA"
    c = BrokerClient(server.socket_path, forged)
    try:
        with pytest.raises(RyaError) as e:
            c.ping()
        assert e.value.code == "E_CAPABILITY_INVALID"
    finally:
        c.close()


def test_a_capability_from_another_broker_does_not_verify(tmp_path):
    """Two brokers, two signing secrets. The secret is per server and never sent,
    so a token minted by one is not authority at the other."""
    store = Store(tmp_path / ".rya")
    store.ensure()
    a = BrokerServer(store=store, workspace="ws_a", agent="x")
    b = BrokerServer(store=store, workspace="ws_a", agent="x")
    a.start()
    b.start()
    try:
        c = BrokerClient(b.socket_path, a.mint(dispatch="d1"))
        with pytest.raises(RyaError) as e:
            c.ping()
        assert e.value.code == "E_CAPABILITY_INVALID"
        c.close()
    finally:
        a.close()
        b.close()


def test_an_expired_capability_is_refused_with_its_own_code():
    """Against ``read_capability`` directly, because ``mint`` clamps the TTL to a
    positive value on purpose — a zero TTL reached by arithmetic should not silently
    produce a token that is dead on arrival."""
    secret = b"\x01" * 32
    stale = proto.Capability(workspace="ws", agent="a", dispatch="d1",
                             expires_at=time.time() - 60)
    with pytest.raises(RyaError) as e:
        proto.read_capability(secret, proto.mint_capability(secret, stale))
    assert e.value.code == "E_CAPABILITY_EXPIRED"
    assert "60s ago" in e.value.message


def test_a_capability_with_no_expiry_is_still_accepted():
    """``expires_at=0`` means unbounded, which is what a test double wants and what
    ``expired`` deliberately treats as "no deadline set" rather than "long past"."""
    secret = b"\x02" * 32
    cap = proto.Capability(workspace="ws", agent="a", dispatch="d1")
    assert proto.read_capability(secret, proto.mint_capability(secret, cap)).dispatch == "d1"


# ---- scoping ---------------------------------------------------------------

def test_the_broker_refuses_a_run_this_dispatch_does_not_own(served):
    """The core property: identity arguments are not the caller's to supply.

    A run this connection neither claimed nor minted is refused rather than
    silently re-scoped — substituting the id would write one run's journal under
    another run's identity, which is worse than a refusal.
    """
    server, store = served
    victim = {"id": "run_victim", "status": "completed", "journal": {}, "trace": []}
    store.save_run(victim)
    c = _client(server)
    try:
        with pytest.raises(RyaError) as e:
            c.call("save_run", {"id": "run_victim", "status": "failed"})
        assert e.value.code == "E_BROKER_SCOPE_DENIED"
        with pytest.raises(RyaError):
            c.call("get_run", "run_victim")
        with pytest.raises(RyaError):
            c.call("journal_append", "run_victim", {"seq": 0, "kind": "x"})
        # And the victim is untouched.
        assert store.get_run("run_victim")["status"] == "completed"
    finally:
        c.close()


def test_a_minted_run_id_becomes_writable_and_nothing_else_does(served):
    """``new_run_id`` is how a fan-out sub-run comes into existence, so the broker
    records what it minted — the second route to legitimate ownership."""
    server, store = served
    c = _client(server)
    try:
        rid = c.call("new_run_id")
        c.call("save_run", {"id": rid, "status": "running", "journal": {}, "trace": []})
        assert store.get_run(rid)["status"] == "running"
        # A DIFFERENT plausible id, minted by nobody, is still refused.
        with pytest.raises(RyaError) as e:
            c.call("save_run", {"id": rid[:-1] + "z", "status": "running"})
        assert e.value.code == "E_BROKER_SCOPE_DENIED"
    finally:
        c.close()


def test_the_agent_on_a_created_job_is_forced_not_accepted(served):
    """D22 from the other side: a tenant enqueueing work tagged as a SIBLING's
    agent would have that sibling's worker claim it and fail on the handler."""
    server, store = served
    c = _client(server, agent="brokered")
    try:
        rid = c.call("new_run_id")
        c.call("create_job", run_id=rid, handler="h", payload={},
               run_at="2020-01-01T00:00:00Z", agent="the-sibling")
        job = store.list_jobs("pending")[0]
        assert job["agent"] == "brokered"
    finally:
        c.close()


def test_a_connection_secret_never_crosses_the_boundary(served):
    """The seal key stays broker-side, so the opened secret must too. The scopes
    survive, because the tenant-side check still produces the friendly error."""
    server, store = served
    store.upsert_connection("stripe", ["charge:write"], secret="sk_live_TOPSECRET")
    c = _client(server)
    try:
        record = c.call("get_connection", "stripe")
        assert "secret" not in record
        assert record["hasSecret"] is True and record["brokered"] is True
        assert record["scopes"] == ["charge:write"]
        assert "TOPSECRET" not in json.dumps(record)
        listed = c.call("list_connections")
        assert all("secret" not in r for r in listed)
    finally:
        c.close()


def test_redaction_keeps_metadata_and_drops_every_named_credential_field():
    """A denylist is right for a record that is mostly metadata — the inverse of
    the method surface, and the docstring on `_redact_connection` says why."""
    out = _redact_connection({"id": "c1", "provider": "p", "scopes": ["a"],
                              "status": "active", "createdAt": "t",
                              "secret": "s", "refreshToken": "r",
                              "accessToken": "a", "secretRef": "ref"})
    assert out["provider"] == "p" and out["createdAt"] == "t"
    for field in ("secret", "refreshToken", "accessToken", "secretRef"):
        assert field not in out
    assert out["hasSecret"] is True


# ---- the claim service ----------------------------------------------------

def test_claiming_happens_on_the_platform_side_with_the_filters_forced(served):
    """``queue.claim`` applies D22's agent filter in Python AFTER the claim, so
    with the claim loop inside a fork that filter is the tenant's own code. The
    service moves it across the boundary."""
    from rya import queue as q

    server, store = served
    q.enqueue(store, "chat-turn", {"runId": "r_mine"},
              metadata={"agent": "brokered"})
    q.enqueue(store, "chat-turn", {"runId": "r_sibling"},
              metadata={"agent": "the-sibling"})
    c = _client(server, agent="brokered")
    try:
        first = c.queue_claim(types=["chat-turn"])
        assert len(first) == 1
        assert first[0]["payload"]["runId"] == "r_mine"
        # The sibling's item is released, not handed over, however many times asked.
        assert c.queue_claim(types=["chat-turn"]) == []
    finally:
        c.close()


def test_a_claim_establishes_ownership_of_its_run(served):
    """Which is the only way a fork legitimately gets a run it did not mint."""
    from rya import queue as q

    server, store = served
    store.save_run({"id": "r_mine", "status": "queued", "journal": {}, "trace": []})
    job = q.enqueue(store, "chat-turn", {"runId": "r_mine"},
                    metadata={"agent": "brokered"})
    c = _client(server, agent="brokered")
    try:
        with pytest.raises(RyaError):
            c.call("get_run", "r_mine")        # before the claim: not ours
        c.queue_claim(types=["chat-turn"])
        assert c.call("get_run", "r_mine")["status"] == "queued"   # after: ours
        # And the item it claimed can be completed, by id, through the service.
        assert c.queue_complete(job["id"], {"ok": True})["status"] == "completed"
    finally:
        c.close()


def test_a_queue_row_this_dispatch_did_not_claim_is_not_transitionable(served):
    """The whole lease lifecycle is a service, because ``queue._check_holder`` is
    caller-side: a fork checking whether it holds its own lease is not a check."""
    from rya import queue as q

    server, store = served
    other = q.enqueue(store, "chat-turn", {"runId": "r_other"},
                      metadata={"agent": "brokered"})
    c = _client(server, agent="brokered")
    try:
        for call in (lambda: c.queue_complete(other["id"], {"forged": True}),
                     lambda: c.queue_fail(other["id"], "forged"),
                     lambda: c.queue_heartbeat(other["id"], 999999)):
            with pytest.raises(RyaError) as e:
                call()
            assert e.value.code == "E_BROKER_SCOPE_DENIED"
        assert store.queue_get(other["id"])["status"] == "pending"
        # The raw row methods are not reachable at all — the services replaced them.
        for method in ("queue_get", "queue_save"):
            assert method not in proto.ALL_METHODS
    finally:
        c.close()


def test_a_fork_cannot_grant_itself_an_unbounded_lease(served):
    """A week-long lease would make the reclaim path — the thing that recovers a
    wedged run — ineffective for a week."""
    from rya import queue as q

    server, store = served
    job = q.enqueue(store, "chat-turn", {"runId": "r"}, metadata={"agent": "brokered"})
    c = _client(server, agent="brokered")
    try:
        c.queue_claim(types=["chat-turn"])
        out = c.queue_heartbeat(job["id"], 60 * 60 * 24 * 7)
        held = store.queue_get(job["id"])["leaseExpiresAt"]
        assert out["leaseExpiresAt"] == held
        # Clamped to the ordinary lease, not the week that was asked for.
        assert held < q._iso_plus(q.DEFAULT_LEASE_SECONDS + 60)
    finally:
        c.close()


def test_the_worker_id_on_a_lease_is_the_brokers_not_the_tenants(served):
    """The worker registry is what the supervisor reaps and scales on, so a fork
    naming itself something else would make that registry tenant-writable."""
    from rya import queue as q

    server, store = served
    q.enqueue(store, "chat-turn", {"runId": "r"}, metadata={"agent": "brokered"})
    c = _client(server, agent="brokered")
    try:
        claimed = c.queue_claim(types=["chat-turn"])
        assert claimed[0]["workerId"].startswith("brokered:")
    finally:
        c.close()


# ---- the LLM proxy (D30) --------------------------------------------------

def test_inference_cannot_be_attributed_to_another_run(served):
    """The meter row is the invoice line, so where it points matters."""
    server, _store = served
    c = _client(server)
    try:
        with pytest.raises(RyaError) as e:
            c.llm_call(kind="respond", system="s", input={}, runId="r_someone_else")
        assert e.value.code == "E_BROKER_SCOPE_DENIED"
    finally:
        c.close()


def test_the_broker_writes_the_meter_row_for_a_call_it_made(tmp_path):
    """D30: the billing record is written by the party that made the call, and it
    is marked so an audit can tell it from a legacy trusted-posture row."""
    from rya.config import ModelRoute, RunConfig

    store = Store(tmp_path / ".rya")
    store.ensure()
    config = RunConfig(values={}, secrets={},
                       routes={"": ModelRoute(provider="mock", model="mock-llm",
                                              api_key="sk-POOLED")})
    server = BrokerServer(store=store, workspace="ws_a", agent="a", config=config)
    server.start()
    c = _client(server)
    try:
        rid = c.call("new_run_id")
        out = c.llm_call(kind="respond", system="hi", input={"q": 1}, runId=rid)
        assert out["text"]
        rows = store.meter_read(rid)
        assert len(rows) == 1
        assert rows[0]["source"] == "broker" and rows[0]["runId"] == rid
        assert rows[0]["kind"] == "llm.respond"
    finally:
        c.close()
        server.close()


def test_a_model_off_the_allowlist_is_refused_before_quota_or_the_call(tmp_path):
    """The allowlist is platform policy, not manifest config: with a pooled key the
    model choice is a cost decision the tenant does not own."""
    from rya.config import ModelRoute, RunConfig

    store = Store(tmp_path / ".rya")
    store.ensure()
    store.policy_set("models", {"allow": ["cheap-model"]})
    config = RunConfig(values={}, secrets={},
                       routes={"": ModelRoute(provider="mock", model="expensive-model")})
    server = BrokerServer(store=store, workspace="ws_a", agent="a", config=config)
    server.start()
    c = _client(server)
    try:
        rid = c.call("new_run_id")
        with pytest.raises(RyaError) as e:
            c.llm_call(kind="respond", system="s", input={}, runId=rid)
        assert e.value.code == "E_MODEL_NOT_ALLOWED"
        assert store.meter_read(rid) == []      # refused before it could cost anything
    finally:
        c.close()
        server.close()


def test_quota_is_enforced_on_the_call_path_not_only_at_admission(tmp_path):
    """#21. Admission-only was right while the tenant held the key; with a pooled
    key an unbounded tenant spends the platform's money."""
    from rya.config import ModelRoute, RunConfig
    from rya.quotas import require_admission

    store = Store(tmp_path / ".rya")
    store.ensure()
    store.policy_set("quotas", {"maxTokensPerDay": 1})
    store.meter_append({"runId": "r_earlier", "inputTokens": 50})
    config = RunConfig(values={}, secrets={},
                       routes={"": ModelRoute(provider="mock", model="mock-llm")})
    server = BrokerServer(
        store=store, workspace="ws_a", agent="a", config=config,
        quota_check=lambda ws, ctx: require_admission(store, kind="model"))
    server.start()
    c = _client(server)
    try:
        rid = c.call("new_run_id")
        with pytest.raises(RyaError) as e:
            c.llm_call(kind="respond", system="s", input={}, runId=rid)
        assert e.value.code == "E_QUOTA_EXCEEDED"
        assert "tokens today" in e.value.message
    finally:
        c.close()
        server.close()


def test_a_streamed_mediated_call_still_delivers_tokens(tmp_path):
    """Turning on mediation must not convert a streaming turn into a
    wait-then-dump — that would be a user-visible regression disguised as a
    security improvement."""
    from rya.config import ModelRoute, RunConfig

    store = Store(tmp_path / ".rya")
    store.ensure()
    config = RunConfig(values={}, secrets={},
                       routes={"": ModelRoute(provider="mock", model="mock-llm")})
    server = BrokerServer(store=store, workspace="ws_a", agent="a", config=config)
    server.start()
    c = _client(server)
    chunks = []
    try:
        rid = c.call("new_run_id")
        out = c.llm_call(kind="respond", system="s", input={"q": 1}, runId=rid,
                         on_token=chunks.append)
        assert chunks and "".join(chunks) == out["text"]
    finally:
        c.close()
        server.close()


# ---- the tenant's own secrets ---------------------------------------------

def test_the_tenants_own_secrets_are_still_served_one_at_a_time(tmp_path):
    """D18 removes the PLATFORM's credentials. A value the tenant declared for its
    own handler is theirs, and withholding it would break ctx.secrets for no gain."""
    from rya.config import RunConfig

    store = Store(tmp_path / ".rya")
    store.ensure()
    config = RunConfig(values={}, secrets={"STRIPE_KEY": "sk_tenant"}, routes={})
    server = BrokerServer(store=store, workspace="ws_a", agent="a", config=config)
    server.start()
    c = _client(server)
    try:
        assert c.secret_get("STRIPE_KEY") == "sk_tenant"
        assert c.secret_get("RYA_SECRET_KEY") is None   # not a tenant secret
    finally:
        c.close()
        server.close()


# ---- the inventory --------------------------------------------------------

def test_the_inventory_separates_platform_credentials_from_tenant_secrets():
    """An inventory that reported the tenant's own declared secret as a violation
    would fail on every deployment and therefore be ignored."""
    assert classify("RYA_DATABASE_URL")[0] == CLASS_PLATFORM
    assert classify("RYA_SECRET_KEY")[0] == CLASS_PLATFORM
    assert classify("ANTHROPIC_API_KEY")[0] == CLASS_PLATFORM
    assert classify("AWS_SECRET_ACCESS_KEY")[0] == CLASS_PLATFORM
    assert classify("RYA_ADMIN_TOKEN")[0] == CLASS_PLATFORM
    assert classify("STRIPE_KEY", tenant_names=["STRIPE_KEY"])[0] == CLASS_TENANT
    assert classify("SOMETHING_TOKEN")[0] == CLASS_AMBIGUOUS
    assert classify("PATH")[0] == ""


def test_the_platform_list_wins_over_a_tenant_declaration():
    """Otherwise a tenant declaring a secret named RYA_SECRET_KEY would launder a
    platform credential into the class that is never reported."""
    cls, group = classify("RYA_SECRET_KEY", tenant_names=["RYA_SECRET_KEY"])
    assert cls == CLASS_PLATFORM and group == "sealKey"


def test_a_connection_string_is_a_dsn_whatever_the_variable_is_called():
    """The rule that catches the variable nobody added to the list."""
    cls, group = classify("MY_INNOCENT_URL", "postgres://u:p@host:5432/db")
    assert cls == CLASS_PLATFORM and group == "dsn"


def test_the_inventory_finds_a_provider_key_on_a_resolved_route():
    """A provider key does not have to be in the environment to be in the process.
    An env-only inventory would report a mediated process clean while it held it."""
    from rya.config import ModelRoute, RunConfig

    config = RunConfig(values={}, secrets={},
                       routes={"": ModelRoute(provider="anthropic", model="m",
                                              api_key="sk-POOLED")})
    inv = take_inventory(env={}, config=config, mediated=True)
    assert not inv.clean
    assert any(f.name == "routes[default].api_key" and f.group == "providerKey"
               for f in inv.violations)


def test_the_inventory_reports_a_trusted_worker_honestly(tmp_path):
    """`clean` is independent of `mediated` on purpose: conflating them would make
    the inventory unable to show the difference between the two postures."""
    store = Store(tmp_path / ".rya")
    inv = take_inventory(env={"RYA_DATABASE_URL": "postgres://u:p@h/d"},
                         store=store, mediated=False)
    assert inv.mediated is False and not inv.clean
    assert {f.group for f in inv.violations} == {"dsn"}


def test_the_scrub_and_the_audit_use_the_same_list():
    """A scrubber and an auditor working from two lists is how a credential ends up
    removed in one build and reported clean in the next."""
    env = {"RYA_DATABASE_URL": "postgres://u:p@h/d", "RYA_SECRET_KEY": "k",
           "ANTHROPIC_API_KEY": "sk", "AWS_SECRET_ACCESS_KEY": "a",
           "RYA_BROKER_SOCKET": "/tmp/s", "STRIPE_KEY": "sk_tenant", "PATH": "/bin"}
    removed = scrub_environment(env)
    assert set(removed) == {"RYA_DATABASE_URL", "RYA_SECRET_KEY",
                            "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY"}
    # Mediation's own variables survive, or the boundary stops working.
    assert env["RYA_BROKER_SOCKET"] == "/tmp/s"
    # An AMBIGUOUS name is reported, never removed: breaking a handler on a
    # heuristic is worse than a line in a report.
    assert env["STRIPE_KEY"] == "sk_tenant"
    assert take_inventory(env=env, mediated=True).clean


# ---- the wire -------------------------------------------------------------

def test_the_wire_is_json_not_pickle(served):
    """A pickle from a hostile peer is arbitrary code execution in the PARENT —
    the exact direction D18 exists to close."""
    server, _store = served
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(server.socket_path))
    try:
        body = b"\x80\x04\x95nonsense-pickle"
        sock.sendall(proto.HEADER.pack(len(body)) + body)
        reply = proto.recv_frame(sock)
        assert reply["ok"] is False
        assert reply["error"]["code"] == "E_BROKER_PROTOCOL"
        assert "not JSON" in reply["error"]["message"]
    finally:
        sock.close()


def test_a_half_sent_frame_does_not_pin_a_broker_thread(served):
    """Announce a length, send less, and stop. Without a body deadline this holds a
    thread for the life of the process — a denial of service any tenant could run,
    which is not what a mediated surface is for."""
    server, _store = served
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(server.socket_path))
    try:
        sock.sendall(proto.HEADER.pack(4096) + b"{")
        # A short deadline for the test; the production default is BODY_TIMEOUT.
        sock.settimeout(10.0)
        reply = proto.recv_frame(sock)
        assert reply["error"]["code"] == "E_BROKER_PROTOCOL"
        assert "sent less within" in reply["error"]["message"]
    finally:
        sock.close()


def test_an_oversized_frame_is_refused_before_it_is_allocated(served):
    server, _store = served
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(server.socket_path))
    try:
        sock.sendall(proto.HEADER.pack(proto.MAX_FRAME + 1))
        reply = proto.recv_frame(sock)
        assert reply["error"]["code"] == "E_BROKER_PROTOCOL"
    finally:
        sock.close()


def test_an_error_keeps_its_code_and_hint_across_the_wire(served):
    """A handler's ``except RyaError as e: if e.code == ...`` must not stop working
    the moment mediation is turned on."""
    server, _store = served
    c = _client(server)
    try:
        with pytest.raises(RyaError) as e:
            c.call("save_run", {"id": "nope"})
        assert e.value.code == "E_BROKER_SCOPE_DENIED"
        assert e.value.hint
    finally:
        c.close()


def test_the_socket_is_not_world_reachable(served):
    server, _store = served
    mode = os.stat(server.socket_path.parent).st_mode & 0o777
    assert mode == 0o700


def test_a_mediated_store_reports_what_it_actually_is(served):
    """`describe` is how the platform says what backs a deployment, and a mediated
    store must not claim to be the thing behind the broker."""
    server, _store = served
    c = _client(server)
    try:
        s = BrokerStore(c, workspace="ws_test")
        assert s.describe()["kind"] == "broker"
        # Absent, not refused: RuntimeContext branches on `getattr(store, ..., None)`
        # and a callable that raises on the wire would look like metering worked.
        assert not hasattr(s, "meter_append")
        assert not hasattr(s, "worker_list")
        assert s.load_memory is not None      # allowlisted, so present
        s.ensure()                            # answered locally, never on the wire
    finally:
        c.close()


def test_the_refusal_log_records_what_a_tenant_reached_for(served):
    """Prevented is not the same as invisible: an operator needs to see a handler
    probing outside its surface."""
    server, _store = served
    c = _client(server)
    try:
        for method in ("policy_set", "worker_list", "meter_append"):
            with pytest.raises(RyaError):
                c.call(method)
        codes = {r["code"] for r in server.refusals}
        assert codes == {"E_BROKER_METHOD_DENIED"}
        assert len(server.refusals) == 3
    finally:
        c.close()


# ---- end to end, in a real fork ------------------------------------------

@pytest.mark.skipif(not FORK_AVAILABLE, reason="needs os.fork")
def test_a_mediated_child_holds_no_database_credential(tmp_path, monkeypatch):
    """Exit criterion 1, asserted from inside the tenant's own handler.

    The handler reports its environment and its store, so this is the tenant's own
    view of what it holds — not the platform's claim about it.
    """
    from rya import turns

    root, store, version = _published(tmp_path)
    # Every credential D18 names, present in the CLAIMER's environment.
    monkeypatch.setenv("RYA_DATABASE_URL", "postgres://u:p@h/d")
    monkeypatch.setenv("RYA_SECRET_KEY", "Zm9vYmFyYmF6cXV1eGZvb2JhcmJhemZvb2JhcmJhego=")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-POOLED")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "bucket-secret")

    w = start_worker(project_root=root, store=store, version_id=version["id"],
                     agent_name="brokered", fork=True, mediated=True)
    try:
        assert w.executor.broker is not None
        turns.enqueue_event(_source(store, version), "message.received", {"v": 7})
        tick = w.drain_once()
        assert tick["count"] == 1

        run = next(r for r in store.list_runs() if r.get("status") == "completed")
        out = run["output"]
        assert out["seen"] == 7
        assert out["pid"] != os.getpid()
        # The tenant's store is the socket, not the database.
        assert out["storeKind"] == "broker"
        # None of the four credentials the exit criterion names.
        assert out["dsn"] is None and out["sealKey"] is None
        assert out["providerKey"] is None and out["bucketKey"] is None
        # And the withheld surface is absent rather than merely refused.
        assert out["hasMeter"] is False
        assert out["hasWorkerList"] is False
        assert out["hasPolicySet"] is False
    finally:
        w.executor.close()


@pytest.mark.skipif(not FORK_AVAILABLE, reason="needs os.fork")
def test_the_template_reports_what_it_scrubbed(tmp_path, monkeypatch):
    """Asserted rather than assumed: a template that scrubbed nothing in mediated
    mode is a finding, not a detail."""
    root, store, version = _published(tmp_path)
    monkeypatch.setenv("RYA_DATABASE_URL", "postgres://u:p@h/d")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-POOLED")
    w = start_worker(project_root=root, store=store, version_id=version["id"],
                     agent_name="brokered", fork=True, mediated=True)
    try:
        tmpl = w.executor.template()
        assert tmpl.mediated is True
        assert "RYA_DATABASE_URL" in tmpl.scrubbed
        assert "ANTHROPIC_API_KEY" in tmpl.scrubbed
    finally:
        w.executor.close()


@pytest.mark.skipif(not FORK_AVAILABLE, reason="needs os.fork")
def test_mediation_requires_fork_because_inline_has_no_boundary(tmp_path):
    """Structural, not a missing feature: in inline mode the claimer IS the process
    that imports the bundle, so there is nothing for a broker to sit between."""
    root, store, version = _published(tmp_path)
    with pytest.raises(RyaError) as e:
        start_worker(project_root=root, store=store, version_id=version["id"],
                     agent_name="brokered", fork=False, mediated=True)
    assert e.value.code == "E_BROKER_UNAVAILABLE"
    assert "nothing for a broker to mediate" in (e.value.hint or "")


@pytest.mark.skipif(not FORK_AVAILABLE, reason="needs os.fork")
def test_the_mediated_and_direct_paths_produce_the_same_run(tmp_path):
    """The mediated path has to be behaviourally identical, or every handler
    becomes posture-dependent and the two deployments diverge into two products."""
    from rya import turns

    results = {}
    for mediated in (False, True):
        root, store, version = _published(tmp_path / ("m" if mediated else "d"))
        w = start_worker(project_root=root, store=store, version_id=version["id"],
                         agent_name="brokered", fork=True, mediated=mediated)
        try:
            turns.enqueue_event(_source(store, version), "message.received", {"v": 11})
            assert w.drain_once()["count"] == 1
            run = next(r for r in store.list_runs() if r.get("status") == "completed")
            results[mediated] = run["output"]["seen"]
        finally:
            w.executor.close()
    assert results[False] == results[True] == 11


def test_ctx_connections_secret_refuses_under_mediation_rather_than_returning_none(tmp_path):
    """Returning None would be indistinguishable from "no such connection", so a
    handler would build a client with no credential and fail upstream with a 401.
    The refusal names the migration instead."""
    import asyncio
    from types import SimpleNamespace

    from rya.sdk.context import RuntimeContext

    store = Store(tmp_path / ".rya")
    store.ensure()
    store.upsert_connection("stripe", ["charge:write"], secret="sk_live_X")
    manifest = SimpleNamespace(name="a", version="0.1.0", tools=[], secrets={},
                               model=SimpleNamespace(provider="mock", default="mock-llm",
                                                     routes={}, fallback=None))
    ctx = RuntimeContext(store=store, manifest=manifest,
                         run={"id": "r", "agent": "a", "journal": {}, "trace": []},
                         tools={}, models={}, project_root=tmp_path,
                         broker=object())     # mediated
    with pytest.raises(RyaError) as e:
        asyncio.run(ctx.connections.secret("stripe"))
    assert e.value.code == "E_BROKER_METHOD_DENIED"
    assert "`url:` tool" in (e.value.hint or "")

    # Unmediated, it still works exactly as before.
    direct = RuntimeContext(store=store, manifest=manifest,
                            run={"id": "r", "agent": "a", "journal": {}, "trace": []},
                            tools={}, models={}, project_root=tmp_path)
    assert asyncio.run(direct.connections.secret("stripe")) == "sk_live_X"
