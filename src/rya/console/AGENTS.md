# `rya.console` - the built-in web console

A single-file SPA served by `rya serve` at `/`. An agent-backend infrastructure
dashboard: the primitives a coding agent provisions, and the operator surface.

> **Being replaced.** A Vite + React console now lives in `web/console/` and is
> served at `/v2`; see `web/console/AGENTS.md` and
> [issue #3](https://github.com/plexe-ai/rya/issues/3) for the decision record.
> Both run side by side until parity (the Prefect `ui` / `ui-v2` pattern).
>
> **Port views out of this file rather than adding to it.** If you must change a
> view here, mirror the change in `web/console/` if it has already been ported -
> the ported list is in `web/console/AGENTS.md`.

## Files

- `index.html` - the entire SPA (inline CSS + JS, no build step). Reads live
  state from `GET /console` and the specific endpoints; polls every ~6s.
- `lucide.min.js` - self-hosted icon library (no third-party CDN; CSP-friendly).
- `dist/` - **not from this directory.** Build output of `web/console/`,
  gitignored, force-included into the wheel, mounted at `/v2`. Never edit by hand.

## Views

Build: Overview, Infrastructure, Manifest, Tools (with kill switches), Memory,
Knowledge, Models, Channels, Connections.
Deploy: Environments, Versions, Workers, Quota & usage + tenant posture (see below).
Operate: Runs & traces (filters + trace view), Conversations, Approvals, Evals,
Action Guard, Jobs & cron, Queue & turns (job table + durable turn-stream
inspector), Secrets, Team & access (members/invites/keys/password).
Auth modal handles signup/login/API-key.

## The deployment hierarchy (PLATFORM_DESIGN §11 item 12)

`workspace → agent → environment → version → runs`, one drill-down:

- **Environments** (`loadEnvironments`) - one row per pointer. `ag('/environments')`
  returns the raw pointer record only, so each row is enriched with
  `ag('/environments/{env}')` (`describe_environment`) for the bundle hash and
  `pinnedRuns`; gate state comes from `GET /gate`.

> **Every agent-scoped call goes through `ag(path)`**, which prefixes the
> currently selected agent — `/agents/${AGENT}${path}`. Since D21 a deployment
> serves many agents, so a hard-coded `/agents/_/…` would be a bug the moment a
> second one is published. `AGENT` defaults to `_` (the platform's sole-agent
> alias, which 400s naming the candidates once there are several) and is set by
> the sidebar selector, which `renderAgentPicker` renders only when `/console`
> reports more than one agent.
- **Environment detail** (`openEnvironment`) - current version + hash, who
  promoted it and when (§12 risk 7), the gate and a live `GET /gate/check`
  verdict, **retained versions** (`pinnedRuns`: older versions held open because
  runs are pinned to them - §9's drain step), promote/rollback history, and the
  runs on the current version.
- **Version detail** (`openVersion`) - identity (hash/sdk/entrypoint/lockfile/
  provenance `metadata`), state, attestations (the gate's evidence), runs, and
  the workers serving it.
- **Workers** (`loadWorkers`) - `GET /workers?status=`, deliberately *every* status
  rather than the `alive` default. An empty list is scale-to-zero (§6), the designed
  idle state, so it must read as "idle", never as an outage — which is exactly why a
  crashed worker must not be filtered out: it would empty the list and look like the
  same thing. Since Phase 3 the server derives `status` from heartbeat age, so a
  SIGKILLed process comes back `lost` and gets its own tile.
- **Quota & usage** (`loadQuotas`) - `GET /quotas` (limits + consumption +
  admission violations) and `GET /usage` (durable meter totals). Since Phase 4 it also
  renders `postureSection()` from `GET /posture`: the launch gate's three conditions
  (D18/D23/D24), the driver's declared *and probed* isolation, and whether this process
  holds platform credentials.

  Two details in there are deliberate. The badge follows `untrusted` rather than `ok`,
  because a trusted deployment with none of the three conditions met is **correct** and
  a red cross would train an operator to ignore the mark on the one deployment where it
  means something. And `/posture` reports credential *kinds*, never values - the
  inventory is designed so the response is safe to render, which is also why the route
  needs no token.

`GET /versions/{id}/runs` exists for these pages: `pinned-runs` deliberately
lists only NON-terminal runs (it answers "what blocks a retire"), and
`/console`'s run list is capped at 30 and carries no `versionId`.

## Design + gotchas

- Follows the standing design system (see repo `docs/`): warm off-white, muted
  semantic accents, hairline borders, pill chips, tabular numerals, no emojis, no
  em dashes. Keep it calm and typography-led. `web/console/src/styles.css` is a
  verbatim copy of this file's `<style>` block - change both or neither.
- The HTML is cached at `rya serve` startup (`api/app.py` `_CONSOLE_HTML`) -
  restart the server to see edits.
- `esc()` escapes `& < > " '`. The quotes matter: its output lands in attribute
  position too (`title="${esc(ts)}"` in `ago()`), where `&<>` alone would let a
  value containing a quote break out of the attribute.
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

## The quotas page answers three questions, not one

It grew by accretion and the order is deliberate: **this workspace's** limits, then
**its organization's** budget (D29), then the **launch gate** (D18/D23/D24/D32). Each
comes from a different boundary and an operator debugging a refusal has to know which
one refused — a tenant told "quota exhausted" while its own usage is near zero will
look in the wrong place, which is why `orgSection` says so in words when
`org.exhausted` is true.

`orgSection` renders **nothing** when there is no `org` block, and that is not a
missing-data shortcut: the api omits it until a reconciler has written a verdict,
because "no rollup" and "an all-clear rollup" are different states.
