# `rya.console` - the built-in web console

A single-file SPA served by `rya serve` at `/`. An agent-backend infrastructure
dashboard: the primitives a coding agent provisions, and the operator surface.

## Files

- `index.html` - the entire SPA (inline CSS + JS, no build step). Reads live
  state from `GET /console` and the specific endpoints; polls every ~6s.
- `lucide.min.js` - self-hosted icon library (no third-party CDN; CSP-friendly).

## Views

Build: Overview, Infrastructure, Manifest, Tools (with kill switches), Memory,
Knowledge, Models, Channels, Connections.
Deploy: Environments, Versions, Workers, Quota & usage (see below).
Operate: Runs & traces (filters + trace view), Conversations, Approvals, Evals,
Action Guard, Jobs & cron, Queue & turns (job table + durable turn-stream
inspector), Secrets, Team & access (members/invites/keys/password).
Auth modal handles signup/login/API-key.

## The deployment hierarchy (PLATFORM_DESIGN §11 item 12)

`workspace → agent → environment → version → runs`, one drill-down:

- **Environments** (`loadEnvironments`) - one row per pointer. `GET
  /agents/_/environments` returns the raw pointer record only, so each row is
  enriched with `GET /agents/_/environments/{env}` (`describe_environment`) for
  the bundle hash and `pinnedRuns`; gate state comes from `GET /gate`.
- **Environment detail** (`openEnvironment`) - current version + hash, who
  promoted it and when (§12 risk 7), the gate and a live `GET /gate/check`
  verdict, **retained versions** (`pinnedRuns`: older versions held open because
  runs are pinned to them - §9's drain step), promote/rollback history, and the
  runs on the current version.
- **Version detail** (`openVersion`) - identity (hash/sdk/entrypoint/lockfile/
  provenance `metadata`), state, attestations (the gate's evidence), runs, and
  the workers serving it.
- **Workers** (`loadWorkers`) - `GET /workers`. An empty list is scale-to-zero
  (§6), the designed idle state; it must read as "idle", never as an outage.
- **Quota & usage** (`loadQuotas`) - `GET /quotas` (limits + consumption +
  admission violations) and `GET /usage` (durable meter totals).

`GET /versions/{id}/runs` exists for these pages: `pinned-runs` deliberately
lists only NON-terminal runs (it answers "what blocks a retire"), and
`/console`'s run list is capped at 30 and carries no `versionId`.

## Design + gotchas

- Follows the standing design system (see repo `docs/`): warm off-white, muted
  semantic accents, hairline borders, pill chips, tabular numerals, no emojis, no
  em dashes. Keep it calm and typography-led.
- The HTML is cached at `rya serve` startup (`api/app.py` `_CONSOLE_HTML`) -
  restart the server to see edits.
- Refresh-safe rendering: the 6s poll must not clobber focused inputs or
  enriched tables (e.g. tools kill-switch columns, runs filter). Split volatile
  sub-renders (see `renderRunsTable`, `renderTools`).
- Session persists in `localStorage` (`rya_session`); the workspace API key in
  `rya_token`.
- Deploy views load on entry (like guard/evals/queue), not from the 6s poll;
  only the sidebar counts refresh, throttled to 30s (`refreshDeployCts`) because
  deployment topology moves on a promote, not per second.
- Every deploy panel must degrade calmly: a fresh install has no versions, no
  environments and no workers, and all three are ordinary states under D12/§6.
  New nav groups must be added to `views`, and to `NAVBTNS` if a new `<nav>`.
