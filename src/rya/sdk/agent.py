"""The agent definition surface.

Agent code does::

    from rya import define_agent

    agent = define_agent()

    @agent.on_event
    async def handle_event(ctx, event):
        ...

    @agent.job("daily_followup")
    async def daily_followup(ctx, job):
        ...

The runtime loader imports the entrypoint module and grabs the most recently
defined agent (see ``runtime.engine.load_agent``).
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

# Populated as a side effect of define_agent() so the loader can find the agent
# defined by an imported entrypoint module.
_DEFINED_AGENTS: List["Agent"] = []


class Agent:
    def __init__(self, name: Optional[str] = None) -> None:
        self.name = name
        self._event_handler: Optional[Callable] = None
        self._job_handlers: Dict[str, Callable] = {}
        self._cron_handlers: Dict[str, Callable] = {}
        self._tool_handlers: Dict[str, Callable] = {}
        self._tool_schemas: Dict[str, dict] = {}
        self._repair_handlers: Dict[str, Callable] = {}

    # ---- decorators ----------------------------------------------------
    def on_event(self, fn: Callable) -> Callable:
        """Register the primary event handler: ``async def handler(ctx, event)``."""
        self._event_handler = fn
        return fn

    def job(self, name: str) -> Callable:
        """Register a background job handler: ``async def handler(ctx, job)``."""

        def deco(fn: Callable) -> Callable:
            self._job_handlers[name] = fn
            return fn

        return deco

    def tool(self, tool_id: str, input_schema: Optional[dict] = None) -> Callable:
        """Register a real tool implementation: ``async def fn(input) -> dict``.

        The handler is a leaf — it may do real IO (HTTP, DB, read env for secrets)
        but must not call journaled ``ctx`` operations (durable-replay rule).

        Pass ``input_schema`` (a JSON Schema) to declare the tool's arguments, so
        the model in ``ctx.llm.run`` uses the right argument names/types instead
        of guessing. A manifest ``input_schema`` on the same tool takes
        precedence over this.

        .. deprecated:: the "real IO" half, in the **untrusted** posture only

           MULTITENANT_DESIGN D18 removes the platform's credentials from the process
           a handler runs in, and §9 risk 2 is explicit that this is "a breaking SDK
           change … the migration is a tenant-facing deprecation, not an internal
           refactor". So it is deprecated here rather than silently altered, and the
           three affected capabilities are named individually because they change by
           different amounts:

           **Direct HTTP.** Still legal, and it stops *working* in a sandbox — not
           because this rule changed but because D24 gives the sandbox no route out.
           Use a manifest ``url:`` tool (the platform makes the call, applies the
           allowlist and injects the connection credential) or ``ctx.egress`` from a
           non-leaf handler.

           **Reading a platform credential from the environment.** Gone. ``os.environ``
           in a sandbox carries no DSN, seal key, provider key or bucket credential:
           they are not scrubbed, they are never added (see
           ``drivers.ContainerDriver.sandbox_env``). A *tenant's own* declared secret is
           unaffected and reaches the handler through ``ctx.secrets.get``, which is why
           D18's list of removed credentials is specifically the platform's.

           **Direct database access.** Was always outside the rule — PLATFORM_DESIGN §14
           lists "reaching the platform's own store from inside leaf tools" among the
           things the boundary exists to stop — and is now unreachable rather than
           discouraged.

           **What has not changed, and will not.** The trusted posture. A self-hosted
           deployment with one trusted tenant runs none of D18/D23/D24 and every leaf
           tool keeps working exactly as written. This deprecation applies where
           ``RYA_UNTRUSTED_TENANTS`` is declared, which is a hosted-product decision, so
           the migration is "your agent behaves differently on our cloud than on your
           laptop" — stated plainly rather than discovered.
        """

        def deco(fn: Callable) -> Callable:
            self._tool_handlers[tool_id] = fn
            if input_schema is not None:
                self._tool_schemas[tool_id] = input_schema
            return fn

        return deco

    def repair(self, tool_id: str) -> Callable:
        """Register a self-heal callback for a tool: ``def repair(input, error)``.

        When the tool raises a ``RyaRecoverableToolError``, the runtime calls this
        ONCE with the input it was given and the error (whose ``.reason`` the
        callback switches on), and retries the tool with the returned **patched
        input**. Returning ``None`` retries with the original input; re-raising
        surfaces the error. Like a tool handler it is a leaf — no journaled ``ctx``
        calls. It may be sync or async.
        """

        def deco(fn: Callable) -> Callable:
            self._repair_handlers[tool_id] = fn
            return fn

        return deco

    def cron(self, trigger_id: str) -> Callable:
        """Register a cron handler keyed by a manifest trigger id."""

        def deco(fn: Callable) -> Callable:
            self._cron_handlers[trigger_id] = fn
            return fn

        return deco

    # ---- lookups -------------------------------------------------------
    def event_handler(self) -> Optional[Callable]:
        return self._event_handler

    def job_handler(self, name: str) -> Optional[Callable]:
        return self._job_handlers.get(name)

    def cron_handler(self, trigger_id: str) -> Optional[Callable]:
        return self._cron_handlers.get(trigger_id)

    def tool_handler(self, tool_id: str) -> Optional[Callable]:
        return self._tool_handlers.get(tool_id)

    def tool_schema(self, tool_id: str) -> Optional[dict]:
        return self._tool_schemas.get(tool_id)

    def repair_handler(self, tool_id: str) -> Optional[Callable]:
        return self._repair_handlers.get(tool_id)


def define_agent(name: Optional[str] = None) -> Agent:
    agent = Agent(name=name)
    _DEFINED_AGENTS.append(agent)
    return agent
