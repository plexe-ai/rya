<div align="center">

<img src="docs/assets/banner.svg" alt="Rya - the backend your AI agents deserve" width="820">

<br/>

[![License](https://img.shields.io/badge/license-Apache%202.0-191918?style=flat-square&labelColor=37352f)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10+-191918?style=flat-square&labelColor=37352f)](pyproject.toml)
![Self-hosted](https://img.shields.io/badge/open--core-self--hostable-191918?style=flat-square&labelColor=37352f)
![Coding-agent-first](https://img.shields.io/badge/coding--agent-first-191918?style=flat-square&labelColor=37352f)

**[Quickstart](#quickstart) · [Why it's different](#why-it-feels-different) · [Docs](docs/DEEP_DIVE.md) · [Deploy](deploy/AGENTS.md) · [Repository map](src/rya/AGENTS.md)**

<br/>

<img src="docs/assets/demo.gif" alt="A durable agent turn on Rya: streaming, a governed tool loop, recommendation cards, and a pause for human approval" width="820">

</div>

---

> Durable runs, human approvals, memory, tools, guardrails, and observability -
> as primitives, not plumbing you rebuild every time. From prompt to
> production-grade agent backend in an afternoon.

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
rya dev --check                                           # validate + inspect. no keys, no database
rya events send --type message.received \
  --payload '{"email":"ada@example.com"}'                 # run pauses for approval
rya approvals approve <id>                                # resume; the email is sent
```

`rya dev` (without `--check`) starts the real thing locally: an `api` process
and one `worker`, the same two processes as production, with the working tree as
the bundle.

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
rya deploy --check              # readiness gate: missing evals, ungated actions, secrets in the repo...
rya deploy --env prod           # bundle + record an immutable version + promote
rya rollback --env prod         # a pointer flip back
```

From a **client repo** — one that installed only the `rya` SDK and has no database
or bucket access — the same pipeline runs over HTTP:

```bash
rya login https://rya.yourco.com --key rya_sk_…
rya publish --env prod          # content-hash + upload + record + promote
```

The platform rebuilds the hash from the bytes it received and refuses a mismatch,
so the content is the address either way. What `publish` cannot do is attest
readiness — see the honesty list below.

A deploy bundles your source, lockfile, manifest and SDK version into an
**immutable, content-hashed version**, records it, and flips the environment's
current-version pointer. New runs go to the new version; in-flight runs finish
on theirs, and a version is retained while any run is still pinned to it — a
run can only be replayed against the code that wrote its journal.

```bash
rya versions list               # every version, newest first
rya envs list                   # what each environment points at
rya bundle                      # just the content hash — the CI "did anything change" check
```

**Gate what reaches production.** A promotion gate is a server-side admission
check, not a client-side courtesy: it refuses unless *evidence* exists that the
checks passed against **this exact content**.

```bash
rya gate set --env prod --require-readiness --require-evals --require-provenance gitSha
rya eval --attest               # files the result against the version under test
rya promote --env prod --version <id>
```

Evidence is bound to the version, so a green eval run on a different tree cannot
admit this one. Rollback is deliberately never gated — a missing attestation must
not hold an outage open. `--force` works and is recorded against the version.

**Bound what a workspace can consume.** Quotas are admission checks too, so an
exhausted budget refuses the *next* run rather than killing one mid-journal:

```bash
rya quotas set --max-concurrent-runs 10 --max-cost-usd-per-day 25
rya quotas show                 # consumption against each ceiling
```

The platform runs as **two processes**, both the same image against the same
Postgres:

```bash
rya serve      # api    — REST/WS/SSE, auth, policy, guard, vault, console, MCP
rya worker     # worker — loads the bundle, owns the journal, executes handlers
```

They are run modes, not microservices: one deployable, one database, no
service-to-service call — they coordinate through the queue. On the durable path
(`POST /agents/{id}/turns`) the api process executes no handler code, which is
what makes per-tenant isolation mean something — though two routes still bypass
that, see below. Deploy both with the AWS IaC in [`deploy/`](deploy/AGENTS.md) or
`docker compose`.

A third mode is optional, and it is the one that means you stop declaring workers
by hand:

```bash
rya supervisor            # watches claimable depth; starts, scales and reaps workers
rya supervisor --plan      # what it would do, and why — the real decision, no effects
```

Without it a worker is started by a human, a compose file or an ECS
`DesiredCount`, so scale-to-zero is one-way: a key exits idle and stays unserved.
With it, work arriving is what brings the key back. Scheduling policy is ours;
only the launch mechanism is pluggable (`RYA_EXECUTION_DRIVER`: `local`, `docker`
or `kubernetes`).

Two more commands exist for the hosted posture, and both are read-first:

```bash
rya posture                # is this deployment safe for untrusted tenants? all four conditions
rya orgs budget <org> --usd-per-month 500   # the billing boundary above a workspace (D29)
rya orgs reconcile         # recompute every org's rollup; run it from a cron
rya posture --verify       # ...and probe the substrate rather than trusting its declaration
rya keyring show           # which key provider — and therefore whether a purge can crypto-shred
rya workspaces disable ws  # stop scheduling, refuse claims, revoke keys. Reversible
rya workspaces purge ws    # shred the key, delete objects and rows. Not reversible
```

## Install

Two distributions, and they are **alternatives, not halves** — both own the `rya`
import namespace, so install one or the other:

```bash
uvx rya create my-agent                           # zero-install: scaffold + run
pip install rya                                   # client SDK: build an agent in your repo
pip install 'rya-server[api,mcp,postgres,llm]'    # the platform: serve, worker, console, store
```

A client repo needs `rya` and a deploy token. It never imports the runtime, never
runs a server, and never knows which deployment it is running in — `ctx` is
implemented by the platform, at the platform's version, which is what stops
governance being forked or pinned by a client. The SDK ships `ctx` type stubs so
your handlers still type-check. See [packaging](docs/PACKAGING.md).

## Learn more

- **[Repository map](src/rya/AGENTS.md)** - the codebase, module by module. Every
  directory has an `AGENTS.md` written so a coding agent can orient fast.
- **[Deep dive](docs/DEEP_DIVE.md)** and **[primitives](docs/primitives.md)** -
  the full picture and every `ctx.*` primitive.
- **[MCP setup](docs/mcp.md)** - point Claude Code / Cursor at Rya.
- **[TypeScript SDK](docs/typescript-sdk.md)** - drive the platform from TS/JS:
  events, resumable turn streams, approvals, and the SDK-free durable job API.
- **[Packaging](docs/PACKAGING.md)** - `rya` vs `rya-server`, and the enforced
  boundary between them.
- **[End-to-end test](scripts/AGENTS.md)** - `python scripts/e2e_platform.py`
  builds both wheels into two separate virtualenvs, authors an agent with only
  the SDK, and runs it on a real `api` + `worker` pair: bundle handoff, promotion
  gate, durable approval, crash-resume in a different process.
- **[Langfuse](docs/langfuse.md)** - self-host it in one compose; every run and
  eval score lands there, deep evals via DeepEval.
- **[RWAP on Rya](docs/integrations/rwap.md)** - running a visual agent builder's
  workflows on Rya's durable queue (architecture + AWS).

Honest about maturity. Everything above runs today, and the durable-execution
primitives are correct and tested but young — not yet load-tested at high volume.
Specifically not done:

- **No managed cloud.** Self-host it; that is also what makes self-hosting a
  residency control.
- **Publishing over HTTP cannot attest readiness.** `rya publish` uploads a bundle
  to `POST /agents/{id}/versions` and needs neither the database nor the bucket, so
  a client repo with only the SDK can ship. But the control plane does not import
  bundles (D13), so it cannot evaluate readiness and files no attestation — the
  response says `"attested": false`, and an environment gated on
  `--require-readiness` will refuse the version. There is also no
  `rya attest readiness`, so `rya deploy --env` from a machine with `rya-server`
  remains the only way to satisfy that gate.
- **The AWS mutator Lambda is a pattern, not an implementation.** It returns 501
  by design rather than pretending; see [`deploy/aws`](deploy/aws/README.md).
- ~~**Two routes still execute handler code in the api process.**~~ **Fixed
  (D21).** `POST /agents/{id}/events` now writes a `queued` run — pinned to
  whatever the environment points at — and hands it to a worker; the caller still
  gets a run id synchronously and an over-quota call is still a 429 rather than a
  silently failed run. `POST /approvals/{id}/approve` records the decision and
  enqueues the resume, pinned to the run's own version. `/reject` stays
  synchronous because it runs no tenant code at all.

  That also ends the `E_JOURNAL_DRIFT` failure this entry used to describe. The
  api imported its mounted entrypoint at startup, so once a bundle could be
  published from elsewhere the code resuming an approval could differ from the
  code that paused it — including by nothing more than an edit made after the api
  booted. The resume job is pinned to `run["versionId"]`, so the process
  continuing a run is on the hash that paused it, by construction.

  **One seam is deliberate and unchanged:** a bare single-tenant `rya serve` still
  executes inline, because there the api *is* the whole deployment and silently
  running nothing would be the worse failure. `RYA_API_INLINE_WORKER=0` (what
  `rya dev` and compose set) turns it off, and multi-tenant never executes.
- ~~**Crashed workers are still reported `alive`.**~~ **Fixed (Phase 3).**
  Liveness is derived from heartbeat age, so a SIGKILLed worker comes back `lost`
  rather than `alive` — and it is still *listed*, because an empty worker list means
  scale-to-zero and a crash must not look like one. This was worse than a cosmetic
  defect: `quotas` counts live workers against `maxWorkers`, so every crash leaked a
  slot permanently.
- **Node isolation is an accepted residual *in the default posture*.** Process
  isolation plus RLS contains a buggy tenant, not a hostile one — workers share a
  kernel. Phase 4 built the hostile-tenant posture (no credentials in the tenant
  process, a gVisor sandbox, egress enforced by the network), but it is **declared,
  not default**: `RYA_UNTRUSTED_TENANTS=1`. Without it, this bullet is what you have,
  which is the right answer for a self-host with one tenant. `rya posture` prints
  which one you are in.
- **`rya worker` is one agent per process** — the api is not. `build_app` no
  longer reads a manifest at all (D21): it learns what agents exist from published
  versions and environment pointers, so one control plane serves as many as the
  workspace has and `rya publish` accepts an agent it has never heard of. The
  limit that remains is in the execution plane: `load_agent` mutates `sys.path`
  and never unloads, so a second agent costs a second **worker** — not a second
  api, port, database or bundle store. See
  [docs/architecture.md](docs/architecture.md).

  `rya worker --fork` (Phase 3, D27) moves the import out of the claiming process
  into a warm interpreter it forks per run, so the long-lived process holds no
  tenant code at all. It does not lift the one-agent limit — a fork is still one
  agent on one version, which is the point of D3.

  **Phase 5 lifted the limit on the *claimer*, and it was the configuration change
  D27 promised.** `rya worker --scope tenant --fork` serves every agent a workspace
  owns from one process: it reads each item's pinned version, materialises that bundle,
  and forks an interpreter for it. Five agents with two live versions each is **one**
  worker holding ten warm interpreters, not ten workers. A promotion costs no extra
  process, and an approval resuming on a retired version is a fork rather than a
  deployment. D3 is untouched: each fork still ran exactly one bundle's import.
- **The fleet can span more than one box, and has not been run doing it.**
  `rya supervisor` starts, scales and reaps workers on demand through the
  `ExecutionDriver` seam, and `--all-workspaces` ticks every tenant. Phase 4 added the
  `docker` and `kubernetes` drivers, so `local` is no longer the only one — but see the
  gVisor caveat below. `ecs` is still unwritten.
- **Untrusted tenancy is enforced by a refusal, not by documentation.**
  `RYA_UNTRUSTED_TENANTS=1` makes the platform check all four of: a sandbox that
  contains a kernel escape, a tenant process holding no credentials, egress enforced by
  the network, and a driver that can put the broker somewhere the tenant is not. Any
  one missing and it refuses to start, naming every unmet condition — because half a
  security boundary is not a security boundary. The refusal is reachable from
  `rya worker` as well as `rya supervisor`, which was a real gap until Phase 4: the
  check existed and only the supervisor called it.
- **What a container driver launches is a *pair*, and that took two phases to get
  right.** Phase 5 found that the container drivers build the sandbox's environment
  from nothing — correct for the process that imports tenant code, and impossible for
  the process that has to open the database and *be* the broker. They were the same
  container, so a `docker` or `kubernetes` claimer would have started, opened an empty
  local store, and claimed nothing while looking healthy. The gate refused for a phase.
  Phase 6 built the missing piece: `rya template-host`, a credential-free process that
  serves warm interpreters over a socket, so the sandbox container can run tenant code
  without the claimer having to be its parent. A launch is now a credentialed claimer
  container beside a credential-free sandbox container sharing an in-memory volume, and
  the credential boundary is a container boundary rather than a process one. The
  framing turned out to be off by one: nothing was wrong with either environment
  builder — the second container was missing.
- **Two supervisors no longer double your fleet.** A supervisor takes a per-workspace
  lease before it applies a plan; a second one goes passive, keeps observing, and logs
  the plan it did not apply. That last part is deliberate: "why is nothing scaling" is
  answered by reading a correct plan going unapplied, not by silence. `--no-lease` opts
  out. Two supervisors over many tenants *split* the fleet rather than duplicating it.
- **gVisor has now been run, and running it broke something reading it never would.**
  `scripts/verify_gvisor.sh` puts a real `runsc` sentry under `cryptography`,
  `pydantic-core`, `psycopg`, `yaml`, `httpx` and `os.fork`; all six work, so D23's
  third-party-wheel question is answered. The isolation probe was not so lucky. Its
  `/proc/version` marker was the literal `4.4.0`, copied from a fixture; a real sentry
  says `4.19.0-gvisor`. That is not a missed signal but an inverted one — a version
  string that is not gVisor's counts as evidence of a *host* kernel, so a genuine
  sandbox was actively refuted and the launch gate refused it. And it refused in
  exactly the configuration the platform ships, because the `--cap-drop=ALL` hardening
  is what makes the other signal (`dmesg`) unreadable. **A fixture is a recording of an
  assumption; it confirms that assumption forever.** The platform still will not claim
  what it cannot verify: an inconclusive probe fails the launch gate.

  What is still not measured is *cost*. The sentry runs nested in a privileged
  container with `--ignore-cgroups`, because this host has no `runsc`, no passwordless
  sudo, and AppArmor blocks unprivileged user namespaces. Correctness is unaffected —
  the syscall interception is real — but the timing numbers keep their caveats, and
  nothing has been measured on `x86_64` at all.
