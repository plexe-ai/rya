# Rya Primitives

Rya exposes agent backend primitives, kept simple and composable.

## Agent identity & manifest

Every agent is declared in `rya.agent.yaml`:

```yaml
name: support-followup-agent
runtime: python
entrypoint: src/agent.py
version: 0.1.0

model:
  default: mock-llm
  fallback: mock-llm-mini

memory:
  type: managed
  collections: [conversations, customer_context]

tools:
  - id: crm.lookup
    permission: allowed
  - id: calendar.read
    permission: read_only
  - id: email.send
    permission: approval_required

models:
  - id: churn-risk-v1
    type: custom
    permission: allowed

channels:
  - type: webhook
    path: /inbound

triggers:
  - id: daily-followups
    type: cron
    schedule: "0 9 * * *"
    handler: daily_followup

approvals:
  default: required_for_external_actions

observability:
  logs: true
  traces: true
  audit: true
```

## Runtime context (`ctx`)

Handlers receive a context exposing every primitive:

| Interface | Purpose |
|-----------|---------|
| `ctx.llm.respond(system, input)` | Call the default LLM (mock) |
| `ctx.models.call(id, input)` | Call a registered model |
| `ctx.memory.get/set/append/search` | State, conversation history, collections |
| `ctx.tools.call(id, input)` | Call a permissioned tool |
| `ctx.channels.send(channel, message)` | Send via a channel |
| `ctx.jobs.schedule(handler, payload, delay_seconds)` | Queue background work |
| `ctx.cron.schedules()` | Inspect cron triggers |
| `ctx.approvals.request(title, body, action)` | Human gate (pauses the run) |
| `ctx.logs.info/debug/warning/error` | Structured logs |
| `ctx.traces.event(name, data)` | Custom trace spans |
| `ctx.secrets.get(name)` | Secret values (never persisted/traced) |
| `ctx.events.emit(type, payload)` | Emit an event |

## Tool permissions

`read_only` · `allowed` · `approval_required` · `disabled`.

`approval_required` tools cannot be called directly — they must flow through
`ctx.approvals.request(action={"tool": ..., "input": ...})`, which executes the
tool only after a human approves.

## Approval lifecycle

`pending → approved | rejected | expired | cancelled`. The runtime pauses a run
on `pending` and resumes it on `approved` by replaying the handler against the
durable journal.

## Memory scopes

`agent` (default) · `user` · `customer` · `workspace` · `environment`. Pass
`scope=` to any memory call.
