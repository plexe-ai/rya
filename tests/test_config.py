"""Per-environment run configuration (D8: run inputs are declared, not ambient).

The bug these tests pin: `providers/llm.py` used to decide *which world a run
talks to* by reading `os.environ` from inside the model call — so an ambient
`ANTHROPIC_API_KEY` silently replaced the deterministic mock with a billed call to
a real model (`resolve_provider`, old `llm.py:50-54`). Resolution now happens once,
against a mapping the caller supplies, and the result says where it came from.

Every test here sets a *bogus* key in `os.environ` and asserts the resolution
ignores it. Against the old code the first one fails.
"""

import pytest

from rya import config
from rya.errors import RyaError
from rya.manifest.schema import Manifest, ModelBlock, ModelRoute


def _manifest(**model) -> Manifest:
    return Manifest(name="cfg-agent", entrypoint="agent.py", model=ModelBlock(**model))


def test_resolution_ignores_the_process_environment(monkeypatch):
    # The regression test for the live bug: a key in the ambient environment must
    # not change what the run resolves to. Only the passed mapping counts.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ambient-should-be-ignored")
    cfg = config.resolve_run_config(_manifest(), env={})
    assert cfg.route().provider == "mock"
    assert cfg.route().source == config.SOURCE_KEY_ABSENT
    # ...and the same manifest with a key *declared* resolves to the real provider,
    # carrying the declared credential rather than the ambient one.
    real = config.resolve_run_config(_manifest(), env={"ANTHROPIC_API_KEY": "sk-declared"})
    assert real.route().provider == "anthropic"
    assert real.route().api_key == "sk-declared"


def test_provider_seam_honours_an_explicit_route(monkeypatch):
    # The end of the bug at the seam it lived in: with a resolved route the model
    # call cannot be swapped for a real one by ambient state.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ambient-should-be-ignored")
    from rya.providers import respond

    route = config.resolve_run_config(_manifest(), env={}).route()
    out = respond(system="Draft.", input={"customer": {"name": "Ada"}}, route=route)
    assert out["provider"] == "mock" and "Ada" in out["text"]


def test_explicit_manifest_provider_beats_key_presence(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ambient-should-be-ignored")
    # `provider: mock` is a declaration, not a fallback (§10) — a declared mock
    # stays mock even when a real key is available in the run's own config.
    cfg = config.resolve_run_config(_manifest(provider="mock"),
                                    env={"ANTHROPIC_API_KEY": "sk-declared"})
    assert cfg.route().provider == "mock"
    assert cfg.route().source == config.SOURCE_MANIFEST
    # ...and the reverse: an explicit provider is honoured with no key at all, so
    # the call fails loudly at request time instead of silently mocking.
    explicit = config.resolve_run_config(_manifest(provider="openai"), env={})
    assert explicit.route().provider == "openai" and explicit.route().api_key == ""
    assert explicit.route().source == config.SOURCE_MANIFEST


def test_force_mock_and_keyless_precedence_come_from_the_passed_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ambient-should-be-ignored")
    # RYA_FORCE_MOCK wins over the manifest's explicit provider (CI / offline evals).
    forced = config.resolve_model_route(provider="anthropic",
                                        env={"RYA_FORCE_MOCK": "1", "ANTHROPIC_API_KEY": "k"})
    assert forced.provider == "mock" and forced.source == "env:RYA_FORCE_MOCK"
    # Keyless mode outranks everything and pins the Governance Adapter, carrying
    # the Platform Token as the sole credential.
    keyless = config.resolve_model_route(provider="anthropic", env={
        "RYA_KEYLESS": "1", "RYA_GOVERNANCE_URL": "https://gov.example/infer",
        "RYA_PLATFORM_TOKEN": "pt_test"})
    assert keyless.provider == "adapter" and keyless.source == "env:RYA_KEYLESS"
    assert keyless.base_url == "https://gov.example/infer" and keyless.api_key == "pt_test"
    # mock holds no key, so it stays available for offline dev even when keyless.
    assert config.resolve_model_route(provider="mock", env={"RYA_KEYLESS": "1"}).provider == "mock"
    # RYA_BEDROCK routes `auto` to Bedrock's ambient IAM identity, key or not.
    bedrock = config.resolve_model_route(env={"RYA_BEDROCK": "1", "ANTHROPIC_API_KEY": "k"})
    assert bedrock.provider == "bedrock" and bedrock.source == "env:RYA_BEDROCK"


def test_keyless_leak_check_reads_the_declared_env_not_the_process(monkeypatch):
    # The keyless guarantee (adapter.py Criterion 2) has to inspect the same mapping
    # the model call will use: an ambient key the run was never given is not a leak,
    # and a declared one is — the old code could only see the process environment.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ambient-should-be-ignored")
    assert config.resolve_model_route(env={"RYA_KEYLESS": "1"}).provider == "adapter"
    with pytest.raises(RyaError) as ei:
        config.resolve_model_route(env={"RYA_KEYLESS": "1", "ANTHROPIC_API_KEY": "sk-leaked"})
    assert ei.value.code == "E_KEYLESS_VIOLATION"


def test_named_routes_inherit_unset_fields_from_the_model_block():
    manifest = _manifest(
        provider="anthropic", default="claude-haiku-4-5", temperature=0.2, max_tokens=500,
        routes={"extract": ModelRoute(model="gpt-4.1-mini", provider="openai", temperature=0.0),
                "cheap": ModelRoute(model="claude-haiku-4-5-mini")},
    )
    cfg = config.resolve_run_config(manifest, env={"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o"})
    extract = cfg.route("extract")
    assert extract.provider == "openai" and extract.api_key == "o"
    assert extract.temperature == 0.0        # the route's own value
    assert extract.max_tokens == 500         # inherited from the ModelBlock
    cheap = cfg.route("cheap")
    assert cheap.provider == "anthropic" and cheap.api_key == "a"
    assert cheap.temperature == 0.2 and cheap.max_tokens == 500  # both inherited
    # The default route is reachable under None / "" / "default"...
    assert cfg.route() is cfg.route("") is cfg.route("default")
    assert cfg.route().model == "claude-haiku-4-5"
    # ...and a route nobody declared is an error, not a silent fall-through to the
    # default model (same code the SDK raises today).
    with pytest.raises(RyaError) as ei:
        cfg.route("classify")
    assert ei.value.code == "E_MODEL_ROUTE_NOT_FOUND"


def test_provenance_distinguishes_a_declaration_from_an_inference():
    # §10: a mock model is configuration, not a fallback. This release does not
    # change what `auto` resolves to — it makes the inference visible so the
    # platform can warn on a run that only *happens* to be mocked.
    inferred = config.resolve_run_config(_manifest(provider="auto"), env={}).route()
    declared = config.resolve_run_config(_manifest(provider="mock"), env={}).route()
    assert inferred.provider == declared.provider == "mock"
    assert inferred.source == config.SOURCE_KEY_ABSENT and inferred.is_mock_fallback
    assert declared.source == config.SOURCE_MANIFEST and not declared.is_mock_fallback
    # A key present in the declared env is provenance too, not just a boolean.
    keyed = config.resolve_model_route(env={"ANTHROPIC_API_KEY": "k"})
    assert keyed.source == "env:ANTHROPIC_API_KEY"


def test_placeholder_model_names_resolve_per_provider():
    # "mock-llm" means "no real model chosen yet": a concrete provider resolves it
    # to the env-declared default, else its own.
    assert config.resolve_model_route(provider="anthropic", env={}).model == config.DEFAULT_ANTHROPIC_MODEL
    assert config.resolve_model_route(provider="openai", env={}).model == config.DEFAULT_OPENAI_MODEL
    assert config.resolve_model_route(
        provider="bedrock", env={"RYA_BEDROCK_MODEL": "us.anthropic.claude-sonnet-4-6"}
    ).model == "us.anthropic.claude-sonnet-4-6"
    assert config.resolve_model_route(
        provider="anthropic", model="mock-llm", env={"RYA_LLM_MODEL": "claude-opus-4-1"}
    ).model == "claude-opus-4-1"
    # A real name is never rewritten, and mock keeps the placeholder it was given.
    assert config.resolve_model_route(provider="anthropic", model="claude-x", env={}).model == "claude-x"
    assert config.resolve_model_route(provider="mock", env={}).model == "mock-llm"


def test_platform_overrides_win_over_the_environment():
    # Where per-environment platform state lands (§11.10 builds the storage). A
    # declared provider from the platform reads as a declaration, not an inference.
    cfg = config.resolve_run_config(
        _manifest(provider="auto"),
        env={"ANTHROPIC_API_KEY": "sk-env"},
        environment="prod",
        overrides={"routes": {"": {"provider": "openai", "model": "gpt-4.1", "api_key": "sk-platform"}},
                   "values": {"TONE": "formal"}, "secrets": {"CRM_TOKEN": "t"},
                   "prices": {"GPT_4_1_IN": 2.0}},
    )
    route = cfg.route()
    assert (route.provider, route.model, route.api_key) == ("openai", "gpt-4.1", "sk-platform")
    assert route.source == "platform:prod" and cfg.source == "platform:prod"
    assert cfg.environment == "prod" and cfg.get("TONE") == "formal"
    assert cfg.secret("CRM_TOKEN") == "t"
    assert cfg.price("gpt-4.1", "IN") == 2.0
    # `overrides["env"]` layers flat env-style values under the same precedence.
    layered = config.resolve_run_config(_manifest(), env={"ANTHROPIC_API_KEY": "sk-env"},
                                       overrides={"env": {"RYA_FORCE_MOCK": "1"}})
    assert layered.route().provider == "mock"


def test_values_secrets_and_prices_come_from_the_declared_env(monkeypatch):
    monkeypatch.setenv("RYA_PRICE_AMBIENT_IN", "99")
    cfg = config.resolve_run_config(_manifest(), env={
        "TONE": "warm", "ANTHROPIC_API_KEY": "sk-declared", "SLACK_WEBHOOK_URL": "https://x",
        "RYA_PRICE_CLAUDE_HAIKU_4_5_IN": "0.8", "RYA_PRICE_CLAUDE_HAIKU_4_5_OUT": "4",
        "RYA_PRICE_BROKEN_IN": "not-a-number",
    })
    # Credentials are separated from plain config so the redaction vault and the
    # audit trail have one set of names to cover.
    assert cfg.get("TONE") == "warm" and cfg.get("ANTHROPIC_API_KEY") is None
    assert cfg.secret("ANTHROPIC_API_KEY") == "sk-declared"
    assert cfg.secret("SLACK_WEBHOOK_URL") == "https://x"
    # Prices for observability/usage.py: declared per environment, keyed by model.
    assert cfg.price("claude-haiku-4-5", "IN") == 0.8
    assert cfg.price("claude-haiku-4-5", "OUT") == 4.0
    assert cfg.price("broken", "IN") is None      # unparseable price = no price
    assert cfg.price("ambient", "IN") is None     # never read from os.environ
    assert cfg.price("claude-haiku-4-5", "IN") == cfg.prices[config.price_key("claude-haiku-4-5", "IN")]


def test_a_route_is_immutable_and_per_call_params_are_a_copy():
    route = config.resolve_model_route(provider="anthropic", env={"ANTHROPIC_API_KEY": "k"},
                                       temperature=0.3, max_tokens=100)
    with pytest.raises(AttributeError):
        route.provider = "openai"  # frozen: a run's inputs cannot drift mid-run
    tuned = config.with_call_params(route, max_tokens=2048)
    assert tuned.max_tokens == 2048 and tuned.temperature == 0.3
    assert route.max_tokens == 100 and tuned.provider == route.provider
    assert config.with_call_params(route) is route  # no params = no copy
    # A manifest `fallback` retargets the model, never the provider/credential.
    fallback = config.with_model(route, "claude-opus-4-1")
    assert fallback.model == "claude-opus-4-1"
    assert (fallback.provider, fallback.api_key) == (route.provider, route.api_key)
