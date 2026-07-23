"""Pydantic schema for ``rya.agent.yaml``.

This is the declarative contract a coding agent writes. Validation is strict
enough to catch typos early but permissive about optional production concerns
(channels, models, observability) so a minimal manifest still runs locally.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class Permission(str, Enum):
    read_only = "read_only"
    allowed = "allowed"
    approval_required = "approval_required"
    disabled = "disabled"


class ModelRoute(BaseModel):
    """A named per-purpose model choice (e.g. a cheap extractor next to the main
    composer). Handlers pick one with ``ctx.llm.respond(..., route="extract")``;
    unset fields inherit from the parent ModelBlock."""
    model: str
    provider: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


class ModelBlock(BaseModel):
    # provider drives ctx.llm. "auto" (default) = real if an API key is present,
    # else mock — keeps zero-config local dev working. "mock" forces the offline
    # deterministic stub; "anthropic"/"openai" require the matching API key.
    # "adapter" is the keyless Governance Adapter path: no provider key, only a
    # governance-minted Platform Token; fails closed (see providers/adapter.py).
    # "bedrock" calls AWS Bedrock's Converse API via the ambient IAM identity
    # (no API key at all) - model names are inference profile ids, e.g.
    # us.anthropic.claude-haiku-4-5. Needs boto3: pip install 'rya[bedrock]'.
    provider: str = "auto"
    default: str = "mock-llm"  # model name (e.g. claude-haiku-4-5-20251001, gpt-4.1-mini)
    fallback: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    # Named routes for per-purpose model selection: compose vs extract vs
    # classify each get the right model without hand-rolling sidecar clients.
    routes: dict[str, ModelRoute] = Field(default_factory=dict)

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, v):
        allowed = {"auto", "mock", "anthropic", "openai", "adapter", "bedrock"}
        if v not in allowed:
            raise ValueError(f"model.provider must be one of {sorted(allowed)}")
        return v

    @field_validator("temperature")
    @classmethod
    def _temp_range(cls, v):
        if v is not None and not (0.0 <= v <= 2.0):
            raise ValueError("model.temperature must be between 0 and 2")
        return v


class MemoryBlock(BaseModel):
    type: str = "managed"
    collections: List[str] = Field(default_factory=list)


class RetryDecl(BaseModel):
    """Declarative tool-call retry policy (A1). The runtime re-invokes the tool
    on a transient failure whose class is listed in ``on``, up to ``max_attempts``
    total attempts, sleeping between tries per ``backoff``.

    Error classes (matched by the runtime): ``timeout`` (E_TIMEOUT / TimeoutError),
    ``5xx`` (an HTTP tool upstream 5xx). ``recoverable`` errors are handled by the
    separate @agent.repair path, not here."""
    max_attempts: int = 1
    backoff: str = "none"  # none | fixed | exponential
    on: List[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _rescue_on_keyword(cls, data):
        # YAML 1.1 footgun: an unquoted `on:` key parses to the boolean True, so
        # `on: [timeout]` silently becomes {True: [...]} and the list is lost.
        # Recover it (and accept a `retry_on` alias) so the policy is never a
        # silent no-op. Quoting `"on":` in the manifest also works.
        if isinstance(data, dict):
            data = dict(data)
            if "on" not in data and True in data:
                data["on"] = data.pop(True)
            if "on" not in data and "retry_on" in data:
                data["on"] = data.pop("retry_on")
        return data

    @field_validator("max_attempts")
    @classmethod
    def _min_attempts(cls, v):
        if v < 1:
            raise ValueError("retry.max_attempts must be >= 1")
        return v

    @field_validator("backoff")
    @classmethod
    def _known_backoff(cls, v):
        if v not in ("none", "fixed", "exponential"):
            raise ValueError("retry.backoff must be one of none|fixed|exponential")
        return v


class ToolDecl(BaseModel):
    id: str
    permission: Permission = Permission.allowed
    description: Optional[str] = None
    url: Optional[str] = None  # if set, the tool is an HTTP endpoint (POST input as JSON)
    # Declarative transient-retry policy (A1). None = no retry (attempt once).
    retry: Optional[RetryDecl] = None
    # Adoption (A5): on a SUCCESSFUL call, copy a field of the tool's result into
    # scoped memory, so a later pinned tool in the SAME turn adopts it. Each entry
    # maps a result field to a "scope.key" memory target, e.g.
    #   adopt: {camsId: student_state.camsId}
    # means "write result['camsId'] to memory[student_state]['camsId'] on success".
    # Pins that read `memory.student_state.camsId` then resolve to the adopted id.
    adopt: dict[str, str] = Field(default_factory=dict)
    # Scoped connected credentials: if `provider` is set, the runtime resolves a
    # vaulted connection for (provider, requesting-user), enforces that the
    # caller's effective authority covers `scopes`, and injects the credential
    # into the call — the handler/model never sees the secret.
    provider: Optional[str] = None
    scopes: List[str] = Field(default_factory=list)
    # Fail closed on missing identity: when true, the tool refuses to run without a
    # verified user (identity.sub). Prevents falling through to a workspace-shared
    # connection when the caller's user token is absent — a per-user credential
    # (e.g. a per-counsellor Crizac bearer) must never be silently shared.
    require_user: bool = False
    # Server-side arg pinning: never trust the model (or handler input) for these
    # arguments. Each entry maps an input field to a trusted source, resolved by
    # the runtime at call time and overwriting whatever the caller supplied:
    #   "event.<path>"          - dotted path into the triggering event payload
    #   "memory.<scope>.<key>"  - a value from scoped memory
    #   "identity.sub"          - the verified caller identity
    #   anything else           - a literal value
    pin: dict[str, str] = Field(default_factory=dict)
    # JSON Schema for the tool's arguments. Given to the model in ctx.llm.run so
    # it uses the RIGHT argument names/types instead of guessing. Pinned args are
    # server-supplied, so they need not appear here.
    input_schema: Optional[dict] = None


class ModelDecl(BaseModel):
    id: str
    type: str = "external"  # external | custom
    permission: Permission = Permission.allowed


class ChannelDecl(BaseModel):
    type: str
    path: Optional[str] = None
    enabled: bool = True


class TriggerDecl(BaseModel):
    id: str
    type: str  # cron | webhook | manual
    handler: str
    schedule: Optional[str] = None  # cron expression when type == cron

    @model_validator(mode="after")
    def _cron_needs_schedule(self):
        if self.type == "cron" and not self.schedule:
            raise ValueError("cron triggers require a 'schedule'")
        return self


class ApprovalsBlock(BaseModel):
    default: str = "required_for_external_actions"


class ObservabilityBlock(BaseModel):
    logs: bool = True
    traces: bool = True
    audit: bool = True


class Manifest(BaseModel):
    name: str
    runtime: str = "python"
    entrypoint: str = "src/agent.py"
    version: str = "0.1.0"
    environment: str = "local"
    owner: Optional[str] = None
    instructions: Optional[str] = None

    timeout_seconds: int = 300  # per-run-segment execution timeout

    model: ModelBlock = Field(default_factory=ModelBlock)
    memory: MemoryBlock = Field(default_factory=MemoryBlock)
    tools: List[ToolDecl] = Field(default_factory=list)
    models: List[ModelDecl] = Field(default_factory=list)
    channels: List[ChannelDecl] = Field(default_factory=list)
    triggers: List[TriggerDecl] = Field(default_factory=list)
    approvals: ApprovalsBlock = Field(default_factory=ApprovalsBlock)
    observability: ObservabilityBlock = Field(default_factory=ObservabilityBlock)

    @field_validator("runtime")
    @classmethod
    def _runtime_supported(cls, v):
        if v not in ("python", "node"):
            raise ValueError(f"unsupported runtime '{v}' (local runtime supports: python)")
        return v

    def tool_permission(self, tool_id: str) -> Optional[Permission]:
        for t in self.tools:
            if t.id == tool_id:
                return t.permission
        return None

    def model_permission(self, model_id: str) -> Optional[Permission]:
        for m in self.models:
            if m.id == model_id:
                return m.permission
        return None
