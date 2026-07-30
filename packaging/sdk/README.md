# rya — the client SDK

Build an agent in your own repo. Run it on a [Rya](https://github.com/plexe-ai/rya)
platform deployment.

```python
from rya import define_agent

agent = define_agent()

@agent.on_event
async def handle(ctx, event):
    ticket = await ctx.tools.call("crm.lookup", {"email": event.payload["email"]})
    reply  = await ctx.llm.respond(system="Draft a refund reply.", input=ticket)

    # pauses the run — durably, for days if needed — until a human approves
    await ctx.approvals.request(
        title="Issue refund", body=reply.text,
        action={"tool": "refund.issue", "input": {"ticket": ticket["id"]}},
    )
    await ctx.channels.send("email", {"to": ticket["email"], "body": reply.text})
```

```bash
uvx rya create support-agent && cd support-agent
rya check --json      # validate the manifest and the handler set
rya bundle            # the content hash CI diffs on
```

Every `ctx.*` call is journaled by the platform, so the run survives a crash,
resumes exactly where it paused, streams token-by-token, and leaves an audit
trail — and none of that code is yours.

## What this package is, and is not

This is the **thin client SDK**: agent declaration (`define_agent`, `@agent.tool`,
`@agent.job`), manifest authoring and validation, bundling, and the client CLI.
Four dependencies, no server, no database.

It is **not** the runtime. `ctx` is implemented by the platform, at the platform's
version — which is the point: governance, permissions, kill switches, guard
verdicts and the journal cannot be forked, lagged or pinned by a client, and a
runtime fix does not require every client to rebuild. Type stubs ship here so your
handlers type-check and autocomplete without the platform installed.

To *operate* a deployment — `rya serve`, `rya worker`, the console, the store,
Postgres, the MCP server — install **`rya-server`** instead. The two are mutually
exclusive alternatives that both own the `rya` import namespace: install one or the
other, never both.

Apache-2.0. Docs, architecture and self-hosting:
<https://github.com/plexe-ai/rya>
