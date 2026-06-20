"""Multi-provider LLM seam + secret redaction (patterns from openclaw)."""

import pytest

from rya.errors import RyaError
from rya.providers import resolve_provider, respond


def test_provider_auto_resolves_to_mock_without_keys(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert resolve_provider("auto") == "mock"
    out = respond(system="Draft.", input={"customer": {"name": "Ada"}}, provider="auto")
    assert out["provider"] == "mock"
    assert "Ada" in out["text"]


def test_provider_auto_prefers_anthropic_when_key_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-xxxx")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert resolve_provider("auto") == "anthropic"


def test_explicit_provider_without_key_errors_clearly(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RyaError) as exc:
        respond(system="x", input={}, provider="anthropic")
    assert exc.value.code == "E_VALIDATION"
    assert "ANTHROPIC_API_KEY" in exc.value.message


def test_secret_redaction_in_trace_and_logs(engine, monkeypatch, capsys):
    """If a handler logs or traces a secret value, it must be scrubbed."""
    # Use the engine's context machinery directly via a crafted run.
    from rya.sdk.context import RuntimeContext

    secret = "supersecret-APIKEY-123456"
    run = {"id": "run_x", "agent": "t", "trace": [], "journal": {}}
    ctx = RuntimeContext(store=engine.store, manifest=engine.manifest, run=run,
                         tools=engine.tools, models=engine.models, project_root=engine.project_root)
    ctx._seed_secret(secret)

    # A log line that leaks the secret...
    ctx.logs.info("token is", value=secret)
    # ...must be redacted in the trace.
    blob = str(run["trace"])
    assert secret not in blob
    assert "«redacted»" in blob


def test_manifest_provider_validation(tmp_path):
    from rya.manifest import load_manifest
    (tmp_path / "rya.agent.yaml").write_text(
        "name: x\nruntime: python\nmodel:\n  provider: gemini\n  default: g\n"
    )
    with pytest.raises(RyaError) as exc:
        load_manifest(tmp_path / "rya.agent.yaml")
    assert exc.value.code == "E_MANIFEST_INVALID"
