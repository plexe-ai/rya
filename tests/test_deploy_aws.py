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
