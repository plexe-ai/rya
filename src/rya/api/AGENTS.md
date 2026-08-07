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
- Deployments (D11/D12/§9): `POST /agents/:id/versions` (upload a packed bundle —
  this is `rya publish`), `GET /agents/:id/versions`, `GET /versions/:id[/runs]`,
  `POST /versions/:id/retire`, `GET /agents/:id/environments[/:env[/history]]`,
  `POST .../environments/:env/{promote,rollback}`, `GET/PUT /gate`,
  `GET /gate/check`, `GET /workers`.
- Tools/kill switches: `GET /tools`, `PUT /tools/:id/permission`.
- Guard/evals: `GET/PUT /guard`, `GET /evals`, `POST /evals/run`.
- Console + inspection: `GET /console` (aggregate), `GET /` (SPA), knowledge, sessions, connections, secrets, channels, models, healthz, `/v1/info`.
- Accounts/teams (multi-tenant): `/v1/signup`, `/v1/login`, `/v1/me`,
  `/v1/workspaces[...]` (create, members/invites, keys), `/v1/password`.

## Gotchas

- **One agent per app.** `build_app` calls `load_manifest` + `load_agent` once, so
  the process serves exactly one `rya.agent.yaml`. `:id` in every path above is
  decorative — handlers resolve `manifest.name` — which is why `POST
  /agents/:id/versions` refuses a bundle declaring a different name instead of
  filing a version nothing here would list or run. Serving N agents means N apps.
- **The entrypoint is imported once, at startup.** So is the agent module. An edit
  to the mounted tree does not reach a running process, and after a `publish` the
  api can be running *older* code than the promoted bundle. That matters on the
  one path where the api still executes handlers: resuming an approval replays the
  journal, and mismatched code is caught as `E_JOURNAL_DRIFT` rather than silently
  replayed. Restart on the promoted bundle to clear it.
- `POST /agents/:id/versions` must never import the uploaded bundle (D13) — it
  verifies bytes, records, promotes. That is why it files no readiness attestation
  and says so in its response (`"attested": false`).
- The console is the built React bundle in `rya/console/dist`, served at `/` with
  its assets mounted at `/assets`; `/v2` (its address during the migration)
  308-redirects there. Edits go to `web/console/src` and need a rebuild
  (`scripts/build_console.sh`), not just a server restart. An absent bundle is an
  ordinary state and `GET /` returns a 503 naming the build command - never a 404,
  and never an import-time failure.
- The asset mount is `/assets`, deliberately not `/`. A `StaticFiles` mount at "/"
  matches every path and Starlette matches in registration order, so it would
  shadow every route declared after it.
- Streaming endpoints run the engine on a worker thread and marshal callback
  frames onto the event loop; SSE tails end only on a terminal-status `run` or
  `error` (a `waiting_approval` run frame is a pause marker, not the end).
- `rya serve` runs a background turn-reclaim sweeper (`RYA_TURN_SWEEP_SECONDS`).
