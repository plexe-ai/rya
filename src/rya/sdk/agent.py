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

    def tool(self, tool_id: str) -> Callable:
        """Register a real tool implementation: ``async def fn(input) -> dict``.

        The handler is a leaf — it may do real IO (HTTP, DB, read env for secrets)
        but must not call journaled ``ctx`` operations (durable-replay rule).
        """

        def deco(fn: Callable) -> Callable:
            self._tool_handlers[tool_id] = fn
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


def define_agent(name: Optional[str] = None) -> Agent:
    agent = Agent(name=name)
    _DEFINED_AGENTS.append(agent)
    return agent
