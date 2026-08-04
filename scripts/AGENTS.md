# `scripts/`

Operational scripts that are not part of either shipped distribution.

- `e2e_platform.py` — the end-to-end proof that an agent authored against the thin
  `rya` SDK runs on the platform. Builds both wheels, installs them into two
  separate virtualenvs, authors an agent in the client one, hands the platform a
  content-hashed bundle, admits it through a promotion gate, and executes it
  across an `api` + `worker` pair with a durable human pause. `phase_publish` then
  ships the same tree the *other* way — `rya publish` over HTTP from the SDK-only
  venv — and asserts both paths resolve to the same version id.

## Running the e2e

```bash
python scripts/e2e_platform.py            # hermetic: offline mock model, ~2 min
python scripts/e2e_platform.py --live     # allow ambient provider keys (costs money)
python scripts/e2e_platform.py --keep     # leave the workdir to poke at
```

Needs `uv` on PATH and a free port (`RYA_E2E_PORT`, default 8791). Exit code is 0
only when there are no FAILs.

**Hermetic is the default on purpose.** An ambient `ANTHROPIC_API_KEY` turns the
offline mock into a paid API call, which both costs money and means the
"works with no keys" claim went untested. `--live` opts into real providers.

## What the outcomes mean

- **PASS** — the behaviour holds.
- **FAIL** — a regression. Fails the run.
- **GAP** — a check that documents a *known platform defect*, not a broken test.
  Printed and summarised, but does not fail the run, so the harness stays useful
  in CI while the gap is open. Close a gap by fixing the platform and deleting
  the `gap=True`; never by deleting the check.

**There are currently no open gaps.** The four the script carried are all closed,
and the history is worth keeping because the gap list is how the multi-tenant plan
measured itself (MULTITENANT_PLAN §11):

| Former gap | Closed by |
| --- | --- |
| `POST /agents/{id}/events` ran the handler inline, with zero workers alive | Phase 2 (D21) — it writes a `queued` run and enqueues |
| …and unpinned, so it ran the api's working tree rather than the promoted bundle | Phase 2 (D21) — the control plane decides the pin from the environment pointer |
| `POST /approvals/{id}/approve` resumed the run inline and returned a terminal status | Phase 2 (D21) — it records the decision and enqueues the resume |
| A SIGKILLed worker still reported `status: alive` | Phase 3 — liveness is derived from heartbeat age |

**A phase now has to bring its own assertions**, since there are none left to flip.
Phase 2 added `phase_multi_agent`; Phase 3 added `phase_supervisor` (the fleet
schedules itself, and untrusted tenancy on a driver that cannot isolate refuses to
start) and `phase_fork_execution` (a run executes in a fork of a warm interpreter,
against the real published bundle rather than a fixture). Phase 4 added
`phase_posture`, `phase_mediation` and `phase_lifecycle`. Phase 5 added
`phase_tenant_scope` and `phase_supervisor_lease` — and **rewrote two of Phase 4's**,
which had asserted that a sandboxed, mediated, network-restricted `kubernetes`
deployment *passes* the launch gate. It did not (D32), so those two asserted the
refusal instead. Phase 6 added `phase_template_host` and **rewrote the same two a third
time**, back to a pass, because the template host they were waiting for got built.

That pair has now been rewritten in three consecutive phases and the sequence is worth
keeping: Phase 4 asserted a pass it had not earned, Phase 5 asserted the refusal and
named what was missing, Phase 6 built it. **The refusal was load-bearing** — it is what
made the gap impossible to forget, and it is why the third rewrite deletes a workaround
rather than discovering a two-phase-old bug.

## Phase 4's phases are adversarial, and that is the point

`phase_mediation` publishes a **deliberately hostile agent** — one that reads
`os.environ` for every credential D18 names, tries to forge a metering row, reaches for
the execution plane's methods, calls `set_config` to re-scope the connection, and issues
a raw `urllib` request to the cloud metadata endpoint — and then asserts against *its
own report*. That is stronger than the platform's view of itself: "the handler could not
find a DSN" beats "we believe we removed the DSN", and it is the only way to check a
property whose whole content is what an attacker can reach.

It also found the phase's most important bug. `phase_posture` asserts the launch gate
**refuses**, rather than asserting the gate function exists — and the refusal did not
happen, because Phase 3 had wired the isolation check into `rya supervisor` only. A
hand-started `rya worker` walked straight past it. The general lesson: a gate is only
as good as its least-guarded entry point, and the way to find that out is to attack it
rather than to read it.

## Phase 5's phase is the opposite kind, and both are needed

`phase_tenant_scope` publishes nothing special. It drives the **scaffolded** agents
through one tenant claimer, and that is why it found something the hostile agent could
not: a mediated `ctx.llm.respond` deadlocked the fork, and had since Phase 4, because
`turns.py` wires `on_token` to `store.stream_append` and the broker client held one lock
across the whole exchange. The hostile agent never calls the model.

So the pair is the lesson rather than either half: **an adversarial fixture proves what
an attacker cannot reach, and an ordinary one proves the product works.** Phase 4 shipped
only the first, and a green scoreboard hid a deadlock on the D30 path for a phase.

Phase 5 also spent four checks red before they were right, and every one was the
*harness* being wrong about the product: a precondition timeout set to exactly
`WORKER_LOST_SECONDS` (this script SIGKILLs workers, so their registrations read `alive`
until the liveness window demotes them), a pre-warm assertion that did not expect a third
promoted agent, a plan assertion that counted `reap` actions as `start`s, and a provider
key that turned an offline stub into a real network call. On a red check, suspect the
harness first here — it is usually right.

## Phase ordering is load-bearing

Eight phases depend on running where they do:

- `phase_isolation` kills every worker, and the run it posts is deliberately left
  stranded — it is the input `phase_supervisor` needs.
- `phase_supervisor` launches a worker that is a *grandchild* the harness cannot
  kill, so it is given a short `--idle-exit` and `phase_fork_execution` waits for it
  to leave. An in-process claimer left running would race the fork claimer and the
  phase would assert nothing about where execution happened.
- `phase_posture` runs **before** `phase_mediation`, so a failure reads as "the gate is
  wrong" rather than as a mediation failure.
- `phase_multi_agent` runs after everything that uses an unprefixed agent-scoped route,
  because it leaves the deployment serving two agents — which makes those routes
  ambiguous by design (D28 Rule 6).
- `phase_tenant_scope` runs **after** `phase_multi_agent` and **before**
  `phase_mediation`. After, because the property needs more than one agent to exist —
  with one, the narrow and wide scopes want the same number of workers and the phase
  asserts nothing. Before, because mediation publishes a third agent whose promoted
  version would then be pre-warmed too: true and correct, and it makes "one claimer,
  both of this tenant's agents" a weaker sentence.
- `phase_tenant_scope` also sets **no provider key**, unlike `phase_mediation`. Its
  agents actually call the model and the scaffolded manifests declare `provider: auto`,
  which resolves to the real provider the moment a key is present.
- `phase_mediation` runs **after** `phase_multi_agent`, because it publishes a *third*
  agent and that phase asserts exactly two are served.
- `phase_lifecycle` is absolutely last: it purges the deployment's own workspace, so
  any phase after it would be testing a deleted tenant.

A phase that needs a run list after `phase_multi_agent` must use the **agent-prefixed**
route. `GET /runs` returns `E_AGENT_AMBIGUOUS` once two agents exist, and reading
`body["runs"]` off an error response yields an empty list that looks like data.

## Rules

- The client agent source is inlined in the script, not scaffolded. The bundle
  hash is an assertion target, so the bytes must not drift with the templates.
- Anything the client side does must run with only the SDK wheel installed. If a
  step needs `rya-server`, it belongs on the platform side of the harness.
- The platform side must never read the client's project directory — only the
  bundle archive. That separation is the point of the test.
