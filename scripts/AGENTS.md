# `scripts/`

Operational scripts that are not part of either shipped distribution.

- `e2e_platform.py` — the end-to-end proof that an agent authored against the thin
  `rya` SDK runs on the platform. Builds both wheels, installs them into two
  separate virtualenvs, authors an agent in the client one, hands the platform a
  content-hashed bundle, admits it through a promotion gate, and executes it
  across an `api` + `worker` pair with a durable human pause.

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

The open gaps all say the same thing from different angles: **handler code still
executes in the api process on two routes**, so `RYA_API_INLINE_WORKER=0` does not
mean what `api/app.py` claims it means.

| Gap | Where |
| --- | --- |
| `POST /agents/{id}/events` runs the handler inline, with zero workers alive | `api/app.py` `post_event` → `engine.run_event` |
| …and unpinned: `versionId` is null, so it runs the api's working tree, not the promoted bundle | same |
| `POST /approvals/{id}/approve` resumes the run inline and returns a terminal status | `api/app.py` `approve` → `_turns.resolve_on_stream` |
| A SIGKILLed worker still reports `status: alive` | `store.py` writes `lastHeartbeatAt`, nothing reads it |

The durable path (`POST /agents/{id}/turns` → queue → `rya worker`) is correct and
is what the harness drives for the main flow; these gaps are about the *other*
routes reaching the same engine without going through the queue.

## Rules

- The client agent source is inlined in the script, not scaffolded. The bundle
  hash is an assertion target, so the bytes must not drift with the templates.
- Anything the client side does must run with only the SDK wheel installed. If a
  step needs `rya-server`, it belongs on the platform side of the harness.
- The platform side must never read the client's project directory — only the
  bundle archive. That separation is the point of the test.
