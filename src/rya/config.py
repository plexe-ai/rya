"""Per-environment run configuration — D8: run inputs are declared, not ambient.

Everything a run is *given* (non-secret config, secrets, and the resolved model
routes) is decided **once**, from an explicitly supplied environment mapping, and
handed to the run as data. Nothing downstream reads the process environment to
decide which world it talks to.

The bug this exists to kill (§11 item 5, D8): ``providers/llm.py`` used to read
``os.environ`` from inside the model call — 22 reads, several of them reachable
from ``ctx.llm.respond``/``ctx.llm.run`` — so the presence of an ambient
``ANTHROPIC_API_KEY`` silently swapped a deterministic mock for a billed call to
a real model. ``resolve_run_config`` / ``resolve_model_route`` take ``env`` as an
argument and never look at ``os.environ``, so resolution is a pure function of
what the caller declared and is reproducible in a test.

Provenance is part of the result. ``ModelRoute.source`` says *why* a provider was
chosen: an explicit manifest choice, a platform-stored per-environment override,
an env flag, or — the case §10 calls out — the mere *absence* of an API key
(``SOURCE_KEY_ABSENT``). Under D8 a mock model is configuration, not a fallback;
this release does not change what ``provider: auto`` resolves to (that is a
separate decision), it only makes the inference visible so the platform can warn.

There is exactly one ambient read left in the whole config/provider path:
``legacy_env()``. It is the transitional shim for callers that have not been
re-pointed at an explicit ``RunConfig`` yet.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from .errors import RyaError

if TYPE_CHECKING:  # avoid importing pydantic/manifest at runtime — this module is a leaf
    from .manifest.schema import Manifest

# Model names that mean "no real model chosen yet" — a concrete provider resolves
# them to its own default (or the env/platform-declared one). Kept here because
# resolution, not the HTTP call, is what needs to know about them.
PLACEHOLDER_MODEL_NAMES = frozenset({"mock-llm", "mock-llm-mini", "mock", "dev"})
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"
# Bedrock model names are inference profile ids, e.g. us.anthropic.claude-haiku-4-5.
DEFAULT_BEDROCK_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# Key under which the default (unnamed) model route is stored. `route(None)`,
# `route("")` and `route("default")` all resolve to it.
DEFAULT_ROUTE = ""

# ---- provenance strings (ModelRoute.source / RunConfig.source) ---------------
SOURCE_MANIFEST = "manifest"        # the manifest declared a concrete provider
SOURCE_ENV = "env"                  # resolved from the supplied env mapping
SOURCE_KEY_ABSENT = "env:key-absent"  # `auto` + no provider key -> mock (§10: an
                                      # inference, not a declaration — log/warn on it)
PROVIDERS = frozenset({"mock", "anthropic", "openai", "bedrock", "adapter"})

# Env names that carry a credential rather than plain config. Used only to split
# an ambient mapping into values/secrets; the platform path (`overrides`) declares
# the split explicitly instead of guessing from the name.
_SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "WEBHOOK", "DSN", "CREDENTIAL")
_PRICE_PREFIX = "RYA_PRICE_"


def legacy_env() -> Mapping[str, str]:
    """**The one ambient read in the config/provider path (D8 transitional shim).**

    Call boundaries that have not been re-pointed at an explicit ``RunConfig``
    yet resolve their config from here, so the tree keeps working while callers
    migrate. Every function in this module that resolves anything takes ``env``
    explicitly; this is the only place that decides ``env`` for you. Delete it
    when ``sdk/context.py``, the engine and the CLI all pass a ``RunConfig``.
    """
    import os  # local: nothing else in this module may reach for the environment

    return os.environ


# The environment name every process in one deployment agrees on. D11 deleted
# `environment:` from the manifest because one content-hashed bundle is promoted
# BETWEEN environments — so the name is a property of the deployment, not of the
# agent. Reading it from the process environment is not the thing D8 forbids: D8
# is about a run's *inputs* (config, secrets, model routes) being declared rather
# than ambient. "Which deployment am I?" has to come from somewhere outside the
# artifact, and this is that seam.
DEFAULT_ENVIRONMENT = "dev"


def current_environment(env: Mapping[str, str] | None = None) -> str:
    env = env if env is not None else legacy_env()
    return env.get("RYA_ENVIRONMENT") or DEFAULT_ENVIRONMENT


@dataclass(frozen=True)
class ModelRoute:
    """One *resolved* model call: which world, which model, on whose credential.

    ``provider`` is always concrete — never ``auto``. Everything the provider seam
    needs to make the call is on this object, so ``providers/llm.py`` never has to
    ask the environment anything.
    """

    provider: str
    model: str
    temperature: float | None = None
    max_tokens: int | None = None
    api_key: str = ""      # anthropic/openai API key, or the adapter's Platform Token
    base_url: str = ""     # adapter: the governance inference endpoint
    region: str = ""       # bedrock: declared region ("" = the ambient AWS chain)
    source: str = SOURCE_ENV
    # Provider-specific extras that are neither credential nor model, e.g. the
    # adapter's RYA_ADAPTER_MODE. Keeps the shared shape from growing a field per
    # provider quirk.
    options: Mapping[str, str] = field(default_factory=dict)

    @property
    def is_mock_fallback(self) -> bool:
        """True when this route is the mock provider *inferred* from a missing key
        rather than declared. The platform warns on these (§10)."""
        return self.provider == "mock" and self.source == SOURCE_KEY_ABSENT


@dataclass(frozen=True)
class RunConfig:
    """Everything a run is declared to receive. Immutable for the run's lifetime."""

    values: Mapping[str, str]              # non-secret config
    secrets: Mapping[str, str]             # credentials (redacted in traces/logs)
    routes: Mapping[str, ModelRoute]       # keyed by manifest route name; "" = default
    environment: str = "dev"
    source: str = SOURCE_ENV
    # Model prices, keyed as usage.py keys them: "<MODEL>_IN" / "<MODEL>_OUT" with
    # the model name upper-cased and -/. turned into _. Use `price()`.
    prices: Mapping[str, float] = field(default_factory=dict)

    def get(self, name: str, default: str | None = None) -> str | None:
        """A declared non-secret config value."""
        return self.values.get(name, default)

    def secret(self, name: str) -> str | None:
        """A declared secret. Separate from ``get`` so the redaction vault and the
        audit trail have one obvious set of names to cover."""
        return self.secrets.get(name)

    def route(self, name: str | None = None) -> ModelRoute:
        """The resolved route ``name``, falling back to the default route.

        An unknown *named* route is an error, not a silent fall-through to the
        default model — same ``E_MODEL_ROUTE_NOT_FOUND`` the SDK raises today.
        """
        if not name or name == "default":
            return self.routes[DEFAULT_ROUTE]
        try:
            return self.routes[name]
        except KeyError:
            raise RyaError(
                "E_MODEL_ROUTE_NOT_FOUND",
                f"Model route '{name}' is not declared (have: {sorted(k for k in self.routes if k)}).",
                hint="Add it under `model.routes:` in rya.agent.yaml.",
            ) from None

    def price(self, model: str, direction: str) -> float | None:
        """Dollars per 1M tokens for ``model`` in ``direction`` (``IN``/``OUT``), or
        None when no price is declared — we never guess provider prices."""
        return self.prices.get(price_key(model, direction))


def price_key(model: str, direction: str) -> str:
    """``("claude-haiku-4-5", "IN") -> "CLAUDE_HAIKU_4_5_IN"`` — the suffix of the
    ``RYA_PRICE_*`` env name, so a declared price and a trace's model name meet."""
    return f"{(model or '').upper().replace('-', '_').replace('.', '_')}_{direction.upper()}"


def _is_secret_name(name: str) -> bool:
    return any(hint in name.upper() for hint in _SECRET_HINTS)


def _prices_from_env(env: Mapping[str, str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, raw in env.items():
        if not key.startswith(_PRICE_PREFIX):
            continue
        try:
            out[key[len(_PRICE_PREFIX):]] = float(raw)
        except (TypeError, ValueError):
            continue  # an unparseable price is "no price", never a wrong price
    return out


def _concrete_provider(provider: str, env: Mapping[str, str]) -> tuple[str, str]:
    """``(concrete provider, provenance)`` — the precedence ``resolve_provider``
    has always implemented, with every input read from ``env`` instead of the
    process environment:

    keyless -> adapter, forced mock, explicit manifest provider, bedrock flag,
    key presence, else mock.
    """
    if env.get("RYA_KEYLESS") == "1":
        # Keyless mode refuses anthropic/openai even with a key present, so a
        # leaked credential is never used (adapter.py, Criterion 2).
        from .providers import adapter as _adapter  # local: keeps this module a leaf

        _adapter.assert_keyless(env)
        if provider in ("auto", "anthropic", "openai"):
            return "adapter", "env:RYA_KEYLESS"
    if env.get("RYA_FORCE_MOCK") == "1":
        return "mock", "env:RYA_FORCE_MOCK"  # CI / offline evals, regardless of manifest
    if provider and provider != "auto":
        return provider, SOURCE_MANIFEST
    if env.get("RYA_BEDROCK") == "1":
        return "bedrock", "env:RYA_BEDROCK"
    if env.get("ANTHROPIC_API_KEY"):
        return "anthropic", "env:ANTHROPIC_API_KEY"
    if env.get("OPENAI_API_KEY"):
        return "openai", "env:OPENAI_API_KEY"
    # §10: this is an *inference* from an absent key, not a declared route.
    return "mock", SOURCE_KEY_ABSENT


def _model_name(provider: str, model: str, env: Mapping[str, str]) -> str:
    """A placeholder name ("mock-llm") means "whatever this provider's default is",
    which the environment may declare (RYA_LLM_MODEL / RYA_OPENAI_MODEL /
    RYA_BEDROCK_MODEL). A real name is always passed through untouched."""
    if model not in PLACEHOLDER_MODEL_NAMES:
        return model
    if provider == "anthropic":
        return env.get("RYA_LLM_MODEL") or DEFAULT_ANTHROPIC_MODEL
    if provider == "openai":
        return env.get("RYA_OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
    if provider == "bedrock":
        return env.get("RYA_BEDROCK_MODEL") or DEFAULT_BEDROCK_MODEL
    return model  # mock / adapter carry the placeholder through by design


def resolve_model_route(
    *,
    provider: str = "auto",
    model: str = "mock-llm",
    temperature: float | None = None,
    max_tokens: int | None = None,
    env: Mapping[str, str],
    environment: str = "dev",
    override: Mapping[str, Any] | None = None,
) -> ModelRoute:
    """Resolve one declared model choice into a concrete, credentialed route.

    ``env`` is required and is the *only* source of ambient-looking values — this
    function never reads ``os.environ`` (D8). ``override`` is the platform's
    per-environment config for this route (§11.10 builds the storage); it wins
    over both the manifest and ``env``.
    """
    declared = dict(override or {})
    provider = declared.get("provider") or provider or "auto"
    model = declared.get("model") or model
    if "temperature" in declared:
        temperature = declared["temperature"]
    if "max_tokens" in declared:
        max_tokens = declared["max_tokens"]

    concrete, source = _concrete_provider(provider, env)
    if declared.get("provider"):
        source = f"platform:{environment}"  # the platform declared it explicitly

    api_key = base_url = region = ""
    options: dict[str, str] = {}
    if concrete == "anthropic":
        api_key = env.get("ANTHROPIC_API_KEY", "")
    elif concrete == "openai":
        api_key = env.get("OPENAI_API_KEY", "")
    elif concrete == "bedrock":
        # No API key: auth is the ambient AWS identity (IAM role/profile). Only the
        # Rya-declared region belongs to the route; AWS_REGION/AWS_DEFAULT_REGION
        # stay boto3's own resolution chain.
        region = env.get("RYA_BEDROCK_REGION", "")
    elif concrete == "adapter":
        base_url = env.get("RYA_GOVERNANCE_URL", "")
        api_key = env.get("RYA_PLATFORM_TOKEN", "")  # the Platform Token is the sole credential
        options["adapter_mode"] = env.get("RYA_ADAPTER_MODE", "available")

    return ModelRoute(
        provider=concrete,
        model=_model_name(concrete, model, env),
        temperature=temperature,
        max_tokens=max_tokens,
        api_key=declared.get("api_key", api_key),
        base_url=declared.get("base_url", base_url),
        region=declared.get("region", region),
        source=source,
        options=options,
    )


def resolve_run_config(
    manifest: Manifest | None,
    *,
    env: Mapping[str, str],
    environment: str = "dev",
    overrides: Mapping[str, Any] | None = None,
) -> RunConfig:
    """Turn a manifest + a supplied environment into the run's declared inputs.

    The single place model routes are resolved. ``env`` is supplied by the caller
    (process env + ``.env`` locally; per-environment platform state in production)
    and is never read from ``os.environ`` here.

    ``overrides`` is the platform's per-environment config for this run and wins
    over ``env``::

        {"env":     {...},   # flat env-style values layered over `env`
         "values":  {...}, "secrets": {...},
         "routes":  {"": {"provider": "mock"}, "extract": {"model": "gpt-4.1-mini"}},
         "prices":  {"CLAUDE_HAIKU_4_5_IN": 0.8}}
    """
    over = dict(overrides or {})
    if over.get("env"):
        env = {**env, **over["env"]}
    route_over: Mapping[str, Any] = over.get("routes") or {}
    default_over = route_over.get(DEFAULT_ROUTE) or route_over.get("default")

    mb = getattr(manifest, "model", None)
    provider = getattr(mb, "provider", "auto") or "auto"
    routes: dict[str, ModelRoute] = {
        DEFAULT_ROUTE: resolve_model_route(
            provider=provider,
            model=getattr(mb, "default", "mock-llm") or "mock-llm",
            temperature=getattr(mb, "temperature", None),
            max_tokens=getattr(mb, "max_tokens", None),
            env=env, environment=environment, override=default_over,
        )
    }
    # Named routes inherit every unset field from the parent ModelBlock — the same
    # inheritance `_LLM._params` applies, resolved once here instead of per call.
    for name, r in (getattr(mb, "routes", None) or {}).items():
        routes[name] = resolve_model_route(
            provider=r.provider or provider,
            model=r.model,
            temperature=r.temperature if r.temperature is not None else getattr(mb, "temperature", None),
            max_tokens=r.max_tokens or getattr(mb, "max_tokens", None),
            env=env, environment=environment, override=route_over.get(name),
        )

    values = {k: v for k, v in env.items() if not _is_secret_name(k)}
    secrets = {k: v for k, v in env.items() if _is_secret_name(k)}
    values.update(over.get("values") or {})
    secrets.update(over.get("secrets") or {})
    prices = _prices_from_env(env)
    prices.update(over.get("prices") or {})

    return RunConfig(
        values=values, secrets=secrets, routes=routes, environment=environment,
        source=f"platform:{environment}" if over else SOURCE_ENV, prices=prices,
    )


def with_model(route: ModelRoute, model: str) -> ModelRoute:
    """The same route pointed at a different model — the manifest's ``fallback``
    after a provider failure. Provider and credential are unchanged, so a fallback
    can never quietly move the call to another world."""
    return replace(route, model=model or route.model)


def with_call_params(route: ModelRoute, *, temperature: float | None = None,
                     max_tokens: int | None = None) -> ModelRoute:
    """A copy of ``route`` with per-call inference parameters applied. Provider and
    credentials are never overridable per call — only how the model is sampled."""
    if temperature is None and max_tokens is None:
        return route
    return replace(
        route,
        temperature=route.temperature if temperature is None else temperature,
        max_tokens=route.max_tokens if max_tokens is None else max_tokens,
    )
