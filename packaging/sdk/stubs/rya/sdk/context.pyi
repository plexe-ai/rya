# Type stubs for `ctx` — the SDK half of PLATFORM_DESIGN §14's `sdk/context.py` row:
# "no longer a surgical split — `ctx` stays platform code; the SDK ships type stubs".
#
# A client repo's handler is `async def handle(ctx, event)` and that repo has no
# platform installed, so this file is the only description of `ctx` its editor and
# type checker ever see. It ships ONLY in the `rya` wheel (packaging/surface.py:
# SDK_DATA_FILES) and deliberately does not sit next to `context.py` in `src/`,
# where it would shadow the real module for anyone type-checking the platform.
#
# What it covers is the surface §7 assigns to the bundle: "the platform decides
# and remembers; the bundle supplies behaviour". Journaling internals (`_step`,
# `_astep`, `_commit`, `_replay`), policy resolution and the store handle are not
# described, because §3's first property is that no client-versioned code holds a
# store handle or makes a policy decision. `tests/test_sdk_surface.py` checks every
# name here against the live class, so the stub cannot drift.

from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

@dataclass
class Event:
    """The trigger `on_event` receives as its second argument."""

    id: str
    type: str
    source: str
    agentId: str
    payload: dict
    createdAt: str
    @classmethod
    def from_dict(cls, d: dict) -> Event: ...
    def model_dump(self) -> dict: ...

@dataclass
class LLMResponse:
    text: str
    model: str
    json: Optional[dict] = ...
    provider: Optional[str] = ...

@dataclass
class GovernedCall:
    """One tool invocation resolved through the full policy path (§7)."""

    tool_id: str
    input: dict
    permission: Any
    impl: str
    decl: Any
    meta: dict
    backend: Any
    scrub: Any

class _LLM:
    async def respond(
        self,
        *,
        system: str,
        input: dict,
        schema: Optional[dict] = ...,
        route: Optional[str] = ...,
        documents: Optional[list] = ...,
        stream: bool = ...,
    ) -> LLMResponse: ...
    async def run(
        self,
        *,
        input: Any,
        system: str = ...,
        tools: Optional[List[str]] = ...,
        max_steps: int = ...,
        route: Optional[str] = ...,
        stream: bool = ...,
        history: Optional[List[dict]] = ...,
    ) -> dict: ...

class _Models:
    async def call(self, model_id: str, input: dict) -> dict: ...

class _Memory:
    async def get(self, key: str, scope: Optional[str] = ...) -> Any: ...
    async def set(self, key: str, value: Any, scope: Optional[str] = ...) -> Any: ...
    async def append(self, collection: str, item: dict, scope: Optional[str] = ...) -> dict: ...
    async def search(
        self, collection: str, query: str, scope: Optional[str] = ..., limit: int = ...
    ) -> List[dict]: ...
    async def block_set(
        self, name: str, value: str, scope: Optional[str] = ..., limit: int = ...
    ) -> dict: ...
    async def block_append(
        self, name: str, text: str, scope: Optional[str] = ..., limit: int = ...
    ) -> dict: ...
    async def block_get(self, name: str, scope: Optional[str] = ...) -> Optional[dict]: ...
    async def blocks(self, scope: Optional[str] = ...) -> List[dict]: ...
    async def remember(
        self, text: str, scope: Optional[str] = ..., dedupe_threshold: float = ...
    ) -> List[dict]: ...
    async def recall(
        self, query: str, scope: Optional[str] = ..., limit: int = ..., min_score: float = ...
    ) -> List[dict]: ...
    async def assemble(
        self, query: str, scope: Optional[str] = ..., token_budget: int = ...
    ) -> dict: ...

class _Knowledge:
    async def add(
        self,
        text: str,
        source: Optional[str] = ...,
        metadata: Optional[dict] = ...,
        chunk_size: int = ...,
        overlap: int = ...,
    ) -> dict: ...
    async def search(self, query: str, limit: int = ..., min_score: float = ...) -> List[dict]: ...
    async def documents(self) -> List[dict]: ...

class _Tools:
    def prepare(self, tool_id: str, input: dict, *, approved: bool = ...) -> GovernedCall: ...
    async def call(self, tool_id: str, input: dict) -> dict: ...
    async def call_approved(self, tool_id: str, input: dict) -> dict: ...

class _Channels:
    async def send(self, channel: str, message: dict) -> dict: ...
    async def send_approved(self, channel: str, message: dict) -> dict: ...

class _Jobs:
    async def schedule(
        self, handler: str, payload: dict, delay_seconds: int = ..., max_attempts: int = ...
    ) -> dict: ...
    async def schedule_group(
        self, jobs: list, on_complete: tuple, max_attempts: int = ...
    ) -> dict: ...

class _Cron:
    def schedules(self) -> List[dict]: ...

class _Approvals:
    async def request(self, *, title: str, body: str, action: dict) -> dict: ...

class _Sessions:
    async def get_or_create(
        self, channel: str, external_id: str, title: Optional[str] = ...
    ) -> dict: ...
    async def append(self, session_id: str, role: str, content: str, **extra: Any) -> dict: ...
    async def history(self, session_id: str, limit: int = ...) -> List[dict]: ...
    async def get(self, session_id: str) -> Optional[dict]: ...
    async def search(self, session_id: str, query: str, limit: int = ...) -> List[dict]: ...

class _Files:
    async def get(self, file_id: str) -> Optional[dict]: ...
    async def list(self, tags: Optional[dict] = ...) -> List[dict]: ...
    async def read(self, file_id: str) -> bytes: ...
    async def as_document(self, file_id: str) -> dict: ...

class _Connections:
    async def get(self, provider: str) -> Optional[dict]: ...
    # §7.1 layer 2: the scoped hand-off. Returns the raw bearer to handler code
    # after scope intersection; deliberately not journaled.
    async def secret(self, provider: str, *, scopes: Optional[List[str]] = ...) -> Optional[str]: ...
    async def list(self) -> List[dict]: ...
    async def upsert(
        self,
        provider: str,
        *,
        secret: str,
        scopes: Optional[List[str]] = ...,
        label: Optional[str] = ...,
    ) -> dict: ...

class _Logs:
    def debug(self, message: str, **f: Any) -> Any: ...
    def info(self, message: str, **f: Any) -> Any: ...
    def warning(self, message: str, **f: Any) -> Any: ...
    def error(self, message: str, **f: Any) -> Any: ...

class _Traces:
    def event(self, name: str, data: Optional[dict] = ...) -> Any: ...

class _Secrets:
    def get(self, name: str) -> Optional[str]: ...

class _Events:
    async def emit(self, type: str, payload: dict) -> dict: ...

class _Guard:
    def policy(self) -> Any: ...
    def describe(self) -> dict: ...
    def check_grounding(self, text: str) -> dict: ...
    def check_secrecy(self, text: str) -> dict: ...
    def scrub(self, obj: Any) -> Any: ...

class _Egress:
    """The sanctioned outbound request (MULTITENANT D24).

    New in Phase 4, and the replacement for the "may do real IO" half of the
    leaf-tool rule: a sandboxed handler has no network route, so a raw `urllib`
    request fails at connect(). This one is mediated — guard verdict, network
    verdict, audit record — and journaled, so a replay after an approval pause
    returns the memoized response rather than re-issuing the call.
    """

    def fetch(self, url: str, *, method: str = ..., headers: Optional[dict] = ...,
              body: Any = ..., timeout: float = ...) -> dict: ...

class RuntimeContext:
    """What a handler is handed. Constructed by the platform, never by a client.

    There is no `__init__` here on purpose: §3 puts the engine, journal, replay,
    policy, guard, vault and store on the platform side of the boundary, so a
    client repo has no way to build one and should not have a signature that
    suggests otherwise. Annotate handlers as `ctx: RuntimeContext` under
    `if TYPE_CHECKING:` — at runtime the SDK does not define this class.
    """

    # Read-only context about the run in flight.
    manifest: Any
    run: dict
    project_root: Path
    identity: Any
    # Platform objects, exposed but not part of the client contract: §3 —
    # "no client-versioned code holds a store handle or makes a policy decision".
    store: Any
    config: Any

    # The ctx.* surface (context.py:252-269).
    llm: _LLM
    models: _Models
    memory: _Memory
    knowledge: _Knowledge
    tools: _Tools
    channels: _Channels
    jobs: _Jobs
    cron: _Cron
    approvals: _Approvals
    sessions: _Sessions
    files: _Files
    connections: _Connections
    logs: _Logs
    traces: _Traces
    secrets: _Secrets
    events: _Events
    guard: _Guard
    egress: _Egress

    def emit_ui(self, component: str, data: Optional[dict] = ...) -> dict: ...
