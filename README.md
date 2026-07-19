# Rya

**The backend your AI agents deserve.** Durable runs, human approvals, memory,
tools, guardrails, and observability - as primitives, not plumbing you rebuild
every time.

> From prompt to production-grade agent backend in an afternoon.

You declare what an agent may do. The runtime enforces it, makes it durable, and
streams it live. Here is a complete agent:

```python
from rya import define_agent

agent = define_agent()

@agent.on_event
async def handle(ctx, event):
    ticket = await ctx.tools.call("crm.lookup", {"email": event.payload["email"]})
    reply  = await ctx.llm.respond(system="Draft a refund reply.", input=ticket)

    # pauses the run - durably, for days if needed - until a human approves
    await ctx.approvals.request(
        title="Issue refund", body=reply.text,
        action={"tool": "refund.issue", "input": {"ticket": ticket["id"]}},
    )
    await ctx.channels.send("email", {"to": ticket["email"], "body": reply.text})
```

Every `ctx.*` call is journaled. So this run survives a crash, resumes exactly
where it paused, streams token-by-token to your UI, and leaves a full audit
trace - and you wrote none of that.

## Quickstart

```bash
uvx rya create support-agent && cd support-agent
rya dev                                                   # validate + inspect. no keys, no database
rya events send --type message.received \
  --payload '{"email":"ada@example.com"}'                 # run pauses for approval
rya approvals approve <id>                                # resume; the email is sent
```

Offline it uses a mock model, so this just works. Set `ANTHROPIC_API_KEY` for
real Claude, `RYA_DATABASE_URL` for durable Postgres - the same agent code runs
on a laptop, a self-hosted box, and the cloud.

## Why it feels different

- **Approvals actually pause the process.** A human gate is not a prompt
  convention - `ctx.approvals.request` unwinds the coroutine, persists, and
  resumes in another process by replaying the journal. The model never sees a
  gated tool.
- **The model can act, sandboxed.** `ctx.llm.run` lets the model call tools in a
  loop - and every call goes through the same permissions, scoped credentials,
  egress firewall, and audit as your own code.
- **Governance the runtime enforces, not the prompt.** Permission tiers,
  server-side argument pinning, runtime kill switches, an egress firewall, and a
  grounding gate that blocks any number the agent did not get from a tool.
- **Durable chat, durable jobs.** Chat turns are leased and crash-reclaimed with
  resumable token streams; the queue runs background work in any language with
  retries and dead-letter. An interrupted turn is retried, not dropped.
- **Coding-agent-first.** Claude Code, Codex, and Cursor drive the whole thing
  over a CLI (`--json` everywhere), an MCP server, and skills - and `rya deploy
  --check` is a green checklist they satisfy so they ship something safe.
- **Yours to run.** Open-core, self-hostable, offline-capable. No SDK lock-in for
  callers: any app talks to it over HTTP.

## Ship it

```bash
rya deploy --check     # production-readiness gate: blocks on missing evals, ungated actions, secrets in the repo...
rya serve              # API + web console + realtime (WS/SSE) + remote MCP, one process
```

`rya serve` is the whole product in one process. Deploy it with the AWS IaC in
[`deploy/`](deploy/AGENTS.md) or `docker compose` - the hosted instance *is*
`rya serve`.

## Install

```bash
uvx rya create my-agent                    # zero-install: scaffold + run
pip install 'rya[api,mcp,postgres,llm]'    # full: control plane, MCP, Postgres, real models
```

## Learn more

- **[Repository map](src/rya/AGENTS.md)** - the codebase, module by module. Every
  directory has an `AGENTS.md` written so a coding agent can orient fast.
- **[Deep dive](docs/DEEP_DIVE.md)** and **[primitives](docs/primitives.md)** -
  the full picture and every `ctx.*` primitive.
- **[MCP setup](docs/mcp.md)** - point Claude Code / Cursor at Rya.

Honest about maturity: the durable-execution primitives are correct and tested
but young (not yet load-tested at high volume), and there is no one-click managed
cloud yet. Everything above runs today.
