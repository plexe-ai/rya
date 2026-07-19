# `rya.runtime` - the execution engine

## Files

- `engine.py` - `Engine` and `load_agent()`. Owns the run lifecycle.

## What the engine does

- `load_agent(manifest, root)` - imports the entrypoint, returns the `Agent` it
  defined (raises stable `E_*` on missing/broken entrypoint).
- `make_event` / `_new_run` - construct events and run records.
- `run_event` / `run_job` / `run_cron` - entry points that build a run and call
  `_execute`. Accept `on_trace` / `on_token` / `on_ui` callbacks for live streaming.
- `_execute` - constructs a `RuntimeContext`, invokes the handler (async, with an
  optional timeout), and maps the outcome to a run status:
  - normal return -> `completed`
  - `PausedForApproval` -> `waiting_approval` (run persisted mid-flight)
  - `ApprovalRejected` -> `rejected`; timeout -> `failed` (E_TIMEOUT); other -> `failed`
  - on finish, best-effort export via `observability.export`.
- `approve` / `reject` - resolve an approval and **resume by replaying** the
  handler against the now-updated journal (memoized steps do not re-run). Accepts
  `on_trace`/`on_token`/`on_ui` so the continuation can stream (see `turns.py`).
- Jobs: `run_job` retries with exponential backoff to `maxAttempts` then fails;
  `work_once` claims + runs all due jobs (atomic `claim_due_job`, safe across
  workers); `due_jobs` / `dead_letter` / `retry_job` round out the queue.

## Gotchas

- `_run_coro` runs handler coroutines whether or not there's already an event
  loop (CLI = none; API/MCP = inside one) - it uses a worker thread in the
  latter case and propagates `PausedForApproval`.
- Resume restores the run's `identity` so per-user RLS holds across the pause.
- The engine is store-agnostic: it only calls the duck-typed store surface.
