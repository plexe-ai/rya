# `rya.api` - the control plane

## Files

- `app.py` - `build_app(root)` returns the FastAPI app that IS `rya serve`:
  REST API + webhook + WebSocket + SSE + web console + mounted remote MCP, in one
  process.

## Auth modes

- **Single-tenant** (default): one `RYA_TOKEN` operator token, or JWT if
  configured; everything in workspace `default`.
- **Multi-tenant** (`RYA_MULTITENANT=1` + Postgres): per-request engine scoped to
  the caller's workspace via a per-workspace API key (`rya_sk_...`), on the
  non-superuser `rya_app` role so Postgres RLS enforces isolation. An optional
  per-user JWT (`X-Rya-User-Token`) turns on per-user RLS within a workspace.

## Endpoint groups

- Runs/agents: `POST /agents/:id/events`, `POST /agents/:id/events/stream` (SSE),
  `GET /runs/:id[/trace]`, `POST /runs/ingest` (external-loop trace ingest).
- Durable turns: `POST /agents/:id/turns`, `GET .../turns/:id/stream`,
  `POST .../turns/reclaim`.
- Realtime: `WS /ws` (event/message/replay; token+trace+ui frames).
- Approvals: `GET /approvals`, `POST /approvals/:id/{approve,reject}` (turn-bound
  runs stream their continuation via `turns.resolve_on_stream`).
- Queue: `POST /queue/jobs[/batch]`, `/queue/claim`, `.../{heartbeat,complete,fail,cancel,retry}`, `/queue/stats`.
- Tools/kill switches: `GET /tools`, `PUT /tools/:id/permission`.
- Guard/evals: `GET/PUT /guard`, `GET /evals`, `POST /evals/run`.
- Console + inspection: `GET /console` (aggregate), `GET /` (SPA), knowledge, sessions, connections, secrets, channels, models, healthz, `/v1/info`.
- Accounts/teams (multi-tenant): `/v1/signup`, `/v1/login`, `/v1/me`,
  `/v1/workspaces[...]` (create, members/invites, keys), `/v1/password`.

## Gotchas

- The console HTML is read once at process start (`_CONSOLE_HTML`) - restart
  `rya serve` to pick up console edits.
- Streaming endpoints run the engine on a worker thread and marshal callback
  frames onto the event loop; SSE tails end only on a terminal-status `run` or
  `error` (a `waiting_approval` run frame is a pause marker, not the end).
- `rya serve` runs a background turn-reclaim sweeper (`RYA_TURN_SWEEP_SECONDS`).
