# `rya.console` - the built-in web console

A single-file SPA served by `rya serve` at `/`. An agent-backend infrastructure
dashboard: the primitives a coding agent provisions, and the operator surface.

## Files

- `index.html` - the entire SPA (inline CSS + JS, no build step). Reads live
  state from `GET /console` and the specific endpoints; polls every ~6s.
- `lucide.min.js` - self-hosted icon library (no third-party CDN; CSP-friendly).

## Views

Overview, Infrastructure, Manifest, Tools (with kill switches), Memory,
Knowledge, Models, Channels, Connections, Runs & traces (filters + trace view),
Conversations, Approvals, Evals, Action Guard, Jobs & cron, Queue & turns (job
table + durable turn-stream inspector), Secrets, Team & access
(members/invites/keys/password). Auth modal handles signup/login/API-key.

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
