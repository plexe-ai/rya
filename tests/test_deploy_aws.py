"""rya deploy aws - orchestrator units (fake boto3, no AWS)."""
from pathlib import Path

import pytest

import rya.deploy_aws as dx
from rya.errors import RyaError


class FakeClient:
    def __init__(self, **handlers):
        self._h = handlers
    def __getattr__(self, name):
        if name in self._h:
            return self._h[name]
        raise AttributeError(name)


def test_discover_network_default_vpc(monkeypatch):
    monkeypatch.setattr(dx, "_boto", lambda s, r: FakeClient(
        describe_vpcs=lambda **k: {"Vpcs": [{"VpcId": "vpc-1"}]},
        describe_subnets=lambda **k: {"Subnets": [{"SubnetId": f"sub-{i}"} for i in range(5)]}))
    net = dx.discover_network("us-east-1", lambda m: None)
    assert net["vpc"] == "vpc-1" and len(net["subnets"]) == 3  # capped at 3


def test_discover_network_no_default_vpc(monkeypatch):
    monkeypatch.setattr(dx, "_boto", lambda s, r: FakeClient(
        describe_vpcs=lambda **k: {"Vpcs": []}))
    with pytest.raises(RyaError) as e:
        dx.discover_network("us-east-1", lambda m: None)
    assert "--vpc" in (e.value.hint or "")


def test_update_keeps_existing_secrets(monkeypatch):
    captured = {}
    def update_stack(**kw):
        captured.update(kw)
    cfn = FakeClient(
        describe_stacks=lambda **k: {"Stacks": [{"StackStatus": "UPDATE_COMPLETE",
            "Parameters": [{"ParameterKey": "RyaSecretKey"}, {"ParameterKey": "RyaAdminToken"},
                           {"ParameterKey": "PublicSubnetIds"}],
            "Outputs": [{"OutputKey": "AlbUrl", "OutputValue": "http://x"}]}]},
        update_stack=update_stack)
    monkeypatch.setattr(dx, "_boto", lambda s, r: cfn)
    monkeypatch.setattr(dx, "wait_stack", lambda *a, **k: {"AlbUrl": "http://x"})
    out = dx.deploy_stack("s", "us-east-1", "img", {"vpc": "v", "subnets": ["a"]}, lambda m: None)
    prev = [p for p in captured["Parameters"] if p.get("UsePreviousValue")]
    assert {p["ParameterKey"] for p in prev} == {"RyaSecretKey", "RyaAdminToken", "PublicSubnetIds"}
    assert out["AlbUrl"] == "http://x"


def test_translate_failure_digs_out_ecs_reason(monkeypatch):
    cfn = FakeClient(describe_stack_events=lambda **k: {"StackEvents": [
        {"ResourceStatus": "UPDATE_FAILED", "LogicalResourceId": "LoadBalancer",
         "ResourceStatusReason": "Resource update cancelled"},
        {"ResourceStatus": "CREATE_FAILED", "LogicalResourceId": "Service",
         "ResourceStatusReason": "task failed to start"}]})
    ecs = FakeClient(
        list_clusters=lambda **k: {"clusterArns": ["arn:cluster/mystack-x"]},
        list_tasks=lambda **k: {"taskArns": ["t1"]},
        describe_tasks=lambda **k: {"tasks": [{"stoppedReason":
            "unable to pull secrets: did not contain json key ANTHROPIC_API_KEY"}]})
    monkeypatch.setattr(dx, "_boto", lambda s, r: {"ecs": ecs}.get(s, cfn))
    hint = dx.translate_failure(cfn, "mystack", "us-east-1")
    assert "Secrets Manager" in hint and "ANTHROPIC_API_KEY" in hint


def test_state_roundtrip(tmp_path):
    assert dx.load_state(tmp_path) is None
    dx.save_state(tmp_path, {"stack": "s", "url": "http://x"})
    assert dx.load_state(tmp_path)["stack"] == "s"


def test_langfuse_create_generates_and_returns_keys(monkeypatch):
    captured = {}
    cfn = FakeClient(
        describe_stacks=lambda **k: (_ for _ in ()).throw(Exception("not found")),
        create_stack=lambda **kw: captured.update(kw))
    monkeypatch.setattr(dx, "_boto", lambda s, r: cfn)
    monkeypatch.setattr(dx, "wait_stack", lambda *a, **k: {"LangfuseUrl": "http://lf"})
    info = dx.deploy_langfuse("s-langfuse", "us-east-1",
                              {"vpc": "v", "subnets": ["a", "b", "c"]}, lambda m: None)
    assert info["url"] == "http://lf"
    assert info["public_key"].startswith("pk-lf-") and info["secret_key"].startswith("sk-lf-")
    sent = {p["ParameterKey"]: p["ParameterValue"] for p in captured["Parameters"]}
    assert sent["ProjectPublicKey"] == info["public_key"]
    assert len(sent["EncryptionKey"]) == 64


def test_langfuse_update_preserves_all_secrets(monkeypatch):
    captured = {}
    cfn = FakeClient(
        describe_stacks=lambda **k: {"Stacks": [{"StackStatus": "CREATE_COMPLETE"}]},
        update_stack=lambda **kw: captured.update(kw))
    monkeypatch.setattr(dx, "_boto", lambda s, r: cfn)
    monkeypatch.setattr(dx, "wait_stack", lambda *a, **k: {"LangfuseUrl": "http://lf"})
    prior = {"public_key": "pk-lf-x", "secret_key": "sk-lf-y", "url": "http://old"}
    info = dx.deploy_langfuse("s-langfuse", "us-east-1",
                              {"vpc": "v", "subnets": ["a"]}, lambda m: None, prior=prior)
    assert all(p.get("UsePreviousValue") for p in captured["Parameters"])
    assert info["public_key"] == "pk-lf-x" and info["url"] == "http://lf"


def test_langfuse_update_without_state_fails_helpfully(monkeypatch):
    cfn = FakeClient(describe_stacks=lambda **k: {"Stacks": [{"StackStatus": "CREATE_COMPLETE"}]})
    monkeypatch.setattr(dx, "_boto", lambda s, r: cfn)
    with pytest.raises(RyaError) as e:
        dx.deploy_langfuse("s-langfuse", "us-east-1", {"vpc": "v", "subnets": []}, lambda m: None)
    assert "deploy.json" in str(e.value)


def test_main_stack_forwards_and_preserves_langfuse_params(monkeypatch):
    captured = {}
    cfn = FakeClient(
        describe_stacks=lambda **k: {"Stacks": [{"StackStatus": "UPDATE_COMPLETE",
            "Parameters": [{"ParameterKey": "LangfuseHost"}, {"ParameterKey": "RyaSecretKey"}],
            "Outputs": []}]},
        update_stack=lambda **kw: captured.update(kw))
    monkeypatch.setattr(dx, "_boto", lambda s, r: cfn)
    monkeypatch.setattr(dx, "wait_stack", lambda *a, **k: {})
    # explicit extra params win
    dx.deploy_stack("s", "us-east-1", "img", {"vpc": "v", "subnets": ["a"]}, lambda m: None,
                    extra_params={"LangfuseHost": "http://lf", "LangfusePublicKey": "pk",
                                  "LangfuseSecretKey": "sk"})
    sent = {p["ParameterKey"]: p for p in captured["Parameters"]}
    assert sent["LangfuseHost"]["ParameterValue"] == "http://lf"
    # no extra params -> existing wiring preserved
    dx.deploy_stack("s", "us-east-1", "img", {"vpc": "v", "subnets": ["a"]}, lambda m: None)
    sent = {p["ParameterKey"]: p for p in captured["Parameters"]}
    assert sent["LangfuseHost"].get("UsePreviousValue") is True


def test_langfuse_keys_persist_before_create(monkeypatch):
    order = []
    cfn = FakeClient(
        describe_stacks=lambda **k: (_ for _ in ()).throw(Exception("not found")),
        create_stack=lambda **kw: order.append("create"))
    monkeypatch.setattr(dx, "_boto", lambda s, r: cfn)
    monkeypatch.setattr(dx, "wait_stack", lambda *a, **k: {"LangfuseUrl": "http://lf"})
    saved = {}
    def persist(info):
        order.append("persist")
        saved.update(info)
    dx.deploy_langfuse("s-lf", "us-east-1", {"vpc": "v", "subnets": ["a"]},
                       lambda m: None, persist=persist)
    assert order == ["persist", "create"]
    assert saved["public_key"].startswith("pk-lf-") and saved["stack"] == "s-lf"


def test_skip_build_falls_back_when_tag_missing(monkeypatch):
    calls = []
    ecr = FakeClient(
        create_repository=lambda **k: None,
        describe_images=lambda **k: (_ for _ in ()).throw(Exception("ImageNotFound")))
    monkeypatch.setattr(dx, "_boto", lambda s, r: ecr)
    monkeypatch.setattr(dx.subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": "abc1234", "stderr": ""})())
    img = dx.build_and_push(Path(dx.__file__).resolve().parents[2] / "examples" / "loan-renewal",
                            "loan-renewal", "1", "us-east-1",
                            lambda m: calls.append(m), skip_build=True)
    assert img.endswith("loan-renewal-abc1234")
    assert any("not in ECR" in m for m in calls)
