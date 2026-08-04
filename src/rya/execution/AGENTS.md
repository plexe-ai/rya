# `rya.execution` - scheduling the execution plane

The half of PLATFORM_DESIGN §6 that was never built. Registration, handler
advertisement, `preflight`, heartbeat, `--idle-exit` and `COLD_START_TARGET_MS` all
lived in `worker.py`; what did not exist was anything that **decided**. Every worker
was started by a human, a compose file or an ECS `DesiredCount`, so scale-to-zero
was one-way: a key exited idle and stayed unserved.

Four decisions, four files, and the split between them is the design:

| File | Decision | Answers |
|---|---|---|
| `drivers.py` | **D26**, **D32** | *How* a worker gets launched, and *what* the launched process is |
| `supervisor.py` | **D25**, **D34** | *What* to launch and how many, and *who gets to decide* |
| `pool.py` | **D27** | *Where the run happens* once a worker is up |
| `scope.py` | **D27's other half**, **D33** | *How much* one claimer serves, and in what order |
| `host.py` | **D32** | *Which container* the run happens in — the sandbox's own half of the pair |

`worker.py` is not in here on purpose: it is what gets scheduled, not what
schedules.

## The two scopes

`RYA_CLAIMER_SCOPE` is `version` (default) or `tenant`. The same drain loop runs at
both; what changes is what the claimer knows at startup.

| | `version` | `tenant` |
|---|---|---|
| key | `ws:agent:vid` | `ws:*:*` |
| bundles resolved | one, before the loop | one per version, on demand |
| preflight | the import, at startup | the import, per group, **before its claim** |
| pool entries | 1 (+3 headroom) | one per hot version (default 12) |
| a broken bundle | the claimer does not start | that agent does not claim; siblings run |
| fairness | between claimers (`concurrency_key`) | **also** inside one (`FairOrder`, D33) |

**The ordering at tenant scope is the whole design and it is three verbs:** peek
(a read — nothing claimed, nothing held), warm (the import, and therefore the
preflight), fork (the claim). §7.1 predicted the wide scope would lose the
before-claiming guarantee; it does not, because the peek is allowed to be stale. A
sibling claimer taking the item first costs nothing — the fork's claim is still atomic
and still filtered to its own group.

**`limit` counts items, not attempts.** A group that turns out empty is retired and its
slot goes to the next group. Spending the slot made a tick with five groups and four
already drained do almost no work while reporting a full budget.

## The rule that makes the seam worth having

**Scheduling policy is platform code. Only the launch mechanism is pluggable.**

Delegating policy to a substrate scheduler would mean KEDA on Kubernetes, service
autoscaling on ECS and something else on a laptop - three behaviours, three test
matrices, for the component MULTITENANT_DESIGN §9 already calls the subtlest here.
One supervisor plus thin drivers is less total work and one behaviour. Same
instinct D5 applied to api<->worker: no wire protocol, nothing to version between
them.

So: **nothing in `supervisor.py` may branch on which driver is in use.**
`test_the_same_policy_produces_the_same_plan_on_every_driver` is what holds that
line - it parametrizes one scenario over four drivers and asserts the *plan* is
byte-identical. The launches are not identical; that is what a driver is for.

## Declared isolation is enforced, verified, and gated

Once launching is pluggable, "is Rya safe for untrusted tenants" stops having one
answer. Every driver declares what it contains (`none` < `shared-kernel` <
`sandboxed` < `microvm`) and the launch gate refuses to start when
`RYA_UNTRUSTED_TENANTS=1` asks for more than the deployment provides.

Two defaults point the safe way and both matter:

- a driver that forgets to declare `isolation` inherits `none`;
- an isolation level this build has never heard of ranks as the **weakest**, so a
  driver from a future release cannot be trusted more than one we understand.

**Isolation depends on configuration, so it is a per-INSTANCE attribute on the
container drivers.** `docker` resolves `sandboxed` only with `--runtime=runsc`,
`kubernetes` only with a RuntimeClass (`RYA_K8S_RUNTIME_CLASS=none` opts out, because
an empty env var is indistinguishable from an unset one in this codebase's `or`
chains). A class-level answer would be a class-level average, and the gate reads the
declaration.

**The declaration is verified rather than trusted.** `verify_isolation` asks the
sandbox what kernel it is on, which is the residual §9 risk 8 named - nothing checked
that pods really landed on a gVisor node. Three states, and they are not the same
question as "is it safe":

| `verified` | meaning | the gate |
|---|---|---|
| `True` | the probe found gVisor | passes |
| `False` | the probe found a **host** kernel; the declaration is wrong | refuses, and downgrades `effective` to `shared-kernel` |
| `None` | no probe was possible: honest ignorance | **refuses** - same direction `isolation_rank` takes for an unknown level |

`read_isolation_signals` is pure so the interesting half runs in CI, which matters
more than usual here: the alternative is a security check that only executes on a
machine with gVisor and is therefore never exercised.

**And being pure is not the same as being right.** Its confirming path was tested
against a captured fixture for three phases, and the fixture had the wrong kernel
string: the marker was the literal `4.4.0`, and a real sentry reports `4.19.0-gvisor`.
That is not a missing signal. This function treats *a version that is not gVisor's* as
positive evidence of a host kernel, so a genuine sandbox came back `verified=False`,
`effective` downgraded to `shared-kernel`, and the gate refused. Worse, it refused
where the platform actually runs: `hardening_args` always passes `--cap-drop=ALL`,
which is usually what makes `dmesg` — the signal that still worked — unreadable.

`scripts/verify_gvisor.sh` is what found it, by running `runsc` for real. The marker is
now the `-gvisor` **suffix**, because gVisor has moved its reported kernel version once
already and will again. If you add a signal here, test it against that script's output
and not against a string you typed.

## The launch gate: one refusal, four conditions

`require_untrusted_posture` checks D23 (a sandbox), D18 (no credentials in the tenant
process), D24 (egress enforced by the network) and D32 (a driver that can put the
broker where the tenant is not) together, because MULTITENANT_PLAN §6 opens with "half
a security boundary is not a security boundary" and separate warnings are separate
things to miss.

**The fourth one reads `launched_unit`, and there are three answers.** `pair` (both
container drivers) passes: a credentialed claimer container beside a credential-free
sandbox running `rya template-host`, sharing `/run/rya`. `claimer` (`local`) passes
too, in D32's *weak* form — the boundary is a process boundary, and `local` is refused
by the isolation condition instead, which is the honest place for "a process boundary
is not a sandbox". `sandbox` is **refused**: a claimer with no DSN claims nothing, so
the deployment starts, looks healthy and serves nothing.

That third state is not dead code even though no shipped driver declares it. It is what
both container drivers were until Phase 6, and it is the arrangement a new driver is
most likely to write by accident. Do not "fix" a refusal by giving `sandbox_env` a
DSN — that is the boundary, not an oversight. Write the pair: `claimer_env` for the
credentialed half, `host_argv` for the other, and declare `UNIT_PAIR`.

**It is called from `start_worker`, not from the CLI.** That was a Phase 4 fix and it
matters: Phase 3 wired the isolation check into `rya supervisor` only, so a
hand-started `rya worker` walked straight past it and `RYA_UNTRUSTED_TENANTS=1` on the
`local` driver started happily. `start_worker` is what *every* route to a running
worker goes through - the CLI, the supervisor's launched process, and a test - so a
check there cannot be routed around.

The split on cost: `start_worker` checks the **declaration** on every worker
(`verify=False`, because probing per scale-up costs a sandbox start per replica);
`rya supervisor` and `rya posture --verify` **probe** (`verify=True`), once.

## Gotchas

- **`plan()` is pure; `apply()` has the effects.** Keep it that way. Every
  interesting scheduling question is then a test with no processes in it, and
  `rya supervisor --plan` shows an operator the *real* decision rather than a
  dry-run approximation of it.
- **A driver handle is not a worker registration.** A handle exists the instant the
  driver launches something; a registration exists only once that process reached
  the database. `observe()` reconciles both and treats a key as served if *either*
  view has it - trusting the registry alone starts a duplicate for every worker
  that is mid-boot.
- **The `local` driver's inventory is process-local.** A subprocess parent cannot
  enumerate orphaned children, so a supervisor restart re-derives the fleet from the
  worker registry, not from the driver. Do not "fix" this with a host scan; the
  reconciliation in `observe()` is the design.
- **The pool is keyed by bundle hash and *checked* against it.** The template
  recomputes the hash of the tree it imports. An earlier cut echoed back the
  requested hash, which made the check confirm only that the caller remembered its
  own question (§9 risk 9).
- **A forked child must open its own store**, and from the *state* root, not the
  bundle root. A psycopg connection cannot be shared across a fork, and a
  `FileStore` built on the unpacked bundle directory is a private empty database
  whose idle ticks look exactly like "no work to do".
- **`WorkerSpec.key` must spell the key exactly as `WorkerKey.concurrency_key`
  does.** The supervisor compares what it launched against what registered itself;
  if those drift, the fleet reads as permanently understaffed and it starts a worker
  every tick.
- **`RYA_ENVIRONMENT` is forced onto a launched worker**, not inherited. Phase 2
  paid for that lesson: the api reads the environment pointer to pin a run, so an
  api on `dev` with a worker on `prod` leaves every turn unclaimed.
- **Pre-warming is opt-in and empty by default.** Warming every promoted agent would
  defeat the scale-to-zero this package just made two-way, and §6 names idle cost as
  *the* constraint. At tenant scope it means warm *interpreters* inside a sandbox the
  tenant already needs, which is the same latency win at a fraction of the idle cost.
- **The supervisor's scope must match its claimers'.** A supervisor planning
  `ws:agent:vid` keys in front of tenant-scoped claimers sees every key unserved and
  starts a worker every tick, forever. Both read `resolve_scope`, and
  `worker_argv` spells `--scope` onto the launched process so the two cannot drift.
- **`plan()` still runs when the lease is held elsewhere.** That is deliberate: a
  standby logging a correct plan it did not apply is what points an operator at the
  lease instead of at the policy. Do not short-circuit `tick` on `hold_lease()`.
- **A driver declares what it *launches*, not just what it isolates.**
  `launched_unit` is `claimer` (holds credentials) or `sandbox` (holds none), and
  `sandbox_env`/`worker_env` want opposite environments. Getting this wrong is not a
  degraded mode: a claimer with no DSN opens a FileStore in its own container and
  reports idle ticks that look exactly like "no work to do".

## Adding a driver

1. Subclass `ExecutionDriver` - or `ContainerDriver` if it runs containers, which
   gives you `sandbox_env`, `hardening_args`, `labels` and the probe for free. Set
   `name`, `isolation`, `cold_start_target_ms`.
2. Implement `start`/`stop`/`list`. Build the command line with the inherited
   `worker_argv`/`worker_env` - do not hand-roll them, or a flag added for one
   substrate silently applies to only that one. (That is not hypothetical: `--agent`
   was missing from `worker_argv` until Phase 4, so a supervisor scheduling for one
   agent launched a worker serving whichever the mounted manifest named.)
3. Accept `env` in `__init__` even if you ignore it, so `resolve_driver` can thread a
   declared environment through (D8) without inspecting signatures.
4. Override `verify_isolation` if you claim `sandboxed` or better. Claiming it without
   a probe means the launch gate refuses your driver, which is the correct outcome.
5. Register it in `DRIVERS` and remove its entry from `PLANNED_DRIVERS`.
6. Add it to the parametrized list in `test_supervisor.py`. If the plan changes, the
   policy layer has learned about your substrate and that is the bug.

## The two environments a driver builds

Not one function with a flag - two, and the difference is where D18's exit criterion
is actually met:

- `worker_env` starts from `os.environ`, because a worker is **platform** code: it is
  the claimer, and it needs the DSN and the keys precisely so its forks do not.
- `sandbox_env` (container drivers) starts from `{}`. The credentials are not filtered
  out, they are never added - so there is no list to forget to update. It also *sets*
  `RYA_BROKER=1` and `RYA_EGRESS=proxy` rather than accepting them, because a
  container this driver launched with either off would be a sandbox with an open
  network and a database credential.
