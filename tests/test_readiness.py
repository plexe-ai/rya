"""Production-readiness checks: the green checklist a coding agent satisfies."""

import json as jsonlib
import os

from typer.testing import CliRunner

from rya.cli import scaffold
from rya.cli.main import app
from rya.manifest import load_manifest
from rya.readiness import check_readiness
from rya.runtime import load_agent
from rya.store import Store

runner = CliRunner()


def _agent(tmp_path, manifest_yaml, agent_py="from rya import define_agent\nagent=define_agent()\n@agent.on_event\nasync def h(ctx,e):\n    pass\n"):
    (tmp_path / "rya.agent.yaml").write_text(manifest_yaml)
    (tmp_path / "agent.py").write_text(agent_py)
    manifest = load_manifest(tmp_path / "rya.agent.yaml")
    agent = load_agent(manifest, tmp_path)
    return manifest, agent, Store(tmp_path)


def test_scaffold_ready_with_warnings(project, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    manifest = load_manifest(project / "rya.agent.yaml")
    agent = load_agent(manifest, project)
    rep = check_readiness(manifest, Store(project), agent, project)
    # scaffold: email.send is approval_required, secrets in .env, handler present.
    assert rep["ready"] is True
    assert rep["blocks"] == []
    # …but it's on file store + mock LLM, so it warns.
    codes = {w["code"] for w in rep["warnings"]}
    assert "W_STORE_FILE" in codes and "W_LLM_MOCK" in codes


def test_ungated_side_effect_blocks(tmp_path):
    # email.send has external side effects; permission 'allowed' must be blocked.
    manifest, agent, store = _agent(tmp_path,
        "name: t\nruntime: python\nentrypoint: agent.py\n"
        "tools:\n  - id: email.send\n    permission: allowed\n")
    rep = check_readiness(manifest, store, agent, tmp_path)
    assert rep["ready"] is False
    block = next(b for b in rep["blocks"] if b["code"] == "E_UNGATED_SIDE_EFFECT")
    assert block["tool"] == "email.send" and "approval_required" in block["fix"]


def test_missing_secret_blocks(tmp_path):
    # crm.lookup requires CRM_API_KEY; no .env here -> block.
    manifest, agent, store = _agent(tmp_path,
        "name: t\nruntime: python\nentrypoint: agent.py\n"
        "tools:\n  - id: crm.lookup\n    permission: allowed\n")
    rep = check_readiness(manifest, store, agent, tmp_path)
    assert any(b["code"] == "E_SECRET_UNSET" and b["secret"] == "CRM_API_KEY" for b in rep["blocks"])


def test_no_handler_blocks(tmp_path):
    manifest, agent, store = _agent(tmp_path,
        "name: t\nruntime: python\nentrypoint: agent.py\n",
        agent_py="from rya import define_agent\nagent=define_agent()\n")  # no @on_event
    rep = check_readiness(manifest, store, agent, tmp_path)
    assert any(b["code"] == "E_NO_EVENT_HANDLER" for b in rep["blocks"])


# ---- CLI: --check exit codes + deploy gating -----------------------------
def _run(args, cwd):
    old = os.getcwd(); os.chdir(cwd)
    try:
        return runner.invoke(app, args)
    finally:
        os.chdir(old)


def test_cli_check_exit_codes(project):
    r = _run(["deploy", "--check", "--json"], project)
    assert r.exit_code == 0
    assert jsonlib.loads(r.stdout)["ready"] is True

    # Break it: ungate email.send.
    (project / "rya.agent.yaml").write_text(
        "name: console-agent\nruntime: python\nentrypoint: src/agent.py\n"
        "tools:\n  - id: email.send\n    permission: allowed\n")
    r2 = _run(["deploy", "--check", "--json"], project)
    assert r2.exit_code == 7  # EXIT_VALIDATION
    body = jsonlib.loads(r2.stdout)
    assert body["ready"] is False
    assert any(b["code"] == "E_UNGATED_SIDE_EFFECT" for b in body["blocks"])


def test_cli_deploy_gated_on_readiness(project):
    (project / "rya.agent.yaml").write_text(
        "name: console-agent\nruntime: python\nentrypoint: src/agent.py\n"
        "tools:\n  - id: email.send\n    permission: allowed\n")
    blocked = _run(["deploy", "--target", "docker", "--json"], project)
    assert blocked.exit_code == 7
    assert jsonlib.loads(blocked.stdout)["error"]["code"] == "E_NOT_PRODUCTION_READY"
    # --force overrides.
    forced = _run(["deploy", "--target", "docker", "--force", "--json"], project)
    assert forced.exit_code == 0
    assert jsonlib.loads(forced.stdout)["validated"] is True


def test_context_includes_readiness(project):
    r = _run(["context", "--json"], project)
    body = jsonlib.loads(r.stdout)
    assert "readiness" in body and body["readiness"]["ready"] is True
