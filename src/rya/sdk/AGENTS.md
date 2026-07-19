# `rya.sdk` - the agent SDK and runtime context

The developer-facing surface and the heart of the runtime.

## Files

- `agent.py` - `define_agent()` and the `Agent` class. Decorators register
  handlers: `@agent.on_event`, `@agent.job(name)`, `@agent.cron(id)`,
  `@agent.tool(id, input_schema=...)`. Lookups (`event_handler()`,
  `tool_handler()`, `tool_schema()`) are used by the engine.
- `context.py` - `RuntimeContext` (`ctx`) and its sub-interfaces. This is where
  journaling, permissions, pins, secret redaction, and the Action Guard live.

## The `ctx` surface (sub-interfaces in context.py)

`ctx.llm` (respond/run, model routes, streaming), `ctx.models`, `ctx.memory`
(scoped kv + collections + vector search), `ctx.knowledge`, `ctx.tools` (call,
with permission + pin + scoped-credential enforcement), `ctx.channels`,
`ctx.jobs`, `ctx.cron`, `ctx.approvals` (request pauses the run), `ctx.sessions`
(durable conversations), `ctx.connections`, `ctx.logs`, `ctx.traces`,
`ctx.secrets`, `ctx.events`, `ctx.guard` (grounding), `ctx.emit_ui` (first-class
UI frames on the turn stream).

## Load-bearing mechanics (do not break)

- **`_step` / `_astep`**: every journaled operation goes through these. On replay
  after an approval pause, a completed step returns its memoized result and does
  NOT re-execute or re-trace. New side-effecting ctx ops MUST use them, or they
  will double-run on resume.
- **Callbacks** `on_trace` / `on_token` / `on_ui`: live subscribers (WebSocket,
  SSE, turn buffer). Threaded in from the engine. Tokens/UI are not separately
  journaled (they ride their step's memoization), so replays never re-stream.
- **Permission resolution** (`_effective_tool_permission`): manifest permission,
  unless a runtime kill switch overrides it (fail-closed if unreadable).
- **Arg pinning** (`_resolve_pin`): `ToolDecl.pin` values from event/memory/
  identity/literal overwrite caller-supplied args before a tool runs.
- **Scoped credentials** (`_authorize_connection`): a tool's required scopes must
  be within (connection scopes intersection user scopes); the secret is vaulted
  and never reaches a trace or the model.
- **Secret redaction** (`_seed_secret`/`_redact`): known secrets scrubbed from
  every trace/log before persistence.

## Tool implementation resolution order (`_Tools.call`)

1. `@agent.tool` handler (agent code), 2. HTTP tool (`url:` in manifest),
3. mock registry fallback. Schema for `ctx.llm.run` resolves manifest >
decorator > registry > `{}`.
