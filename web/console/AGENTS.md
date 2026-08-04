# `web/console` - the React operator console

Vite + React + TypeScript. Source lives here; the compiled bundle lands in
`src/rya/console/dist/` and ships inside the wheel. Served at **`/v2`** by
`rya serve` while the legacy single-file SPA keeps **`/`**.

## Why this exists

The legacy console (`src/rya/console/index.html`) is 120KB of inline JS building
HTML with 62 `innerHTML` assignments. It works, and it is disciplined about
`esc()`, but it hand-rolls reconciliation: the 6s poll would clobber focused
inputs, so `renderRuns` sniffed `document.activeElement` and skipped re-rendering
whenever the search box had focus. That workaround is the bug class this migration
removes - see `src/views/Runs.test.tsx`, which pins the behaviour down.

Decision record and the survey of how other OSS Python platforms ship a frontend:
[issue #3](https://github.com/plexe-ai/rya/issues/3).

## Commands

```bash
npm install
npm run dev         # :5273, proxies API paths to a local `rya serve` on :8000
npm run build       # typecheck + bundle -> ../../src/rya/console/dist
npm test            # vitest
npm run typecheck
```

From the repo root, `scripts/build_console.sh` runs test + build and is what CI
should call before `uv build --wheel packaging/server`.

## Packaging contract

**Node is a release-time dependency, never an install-time one.** `pip install
rya-server` needs only Python, because `dist/` is already in the wheel.

- `dist/` is **gitignored build output**. Do not commit it.
- The root `pyproject.toml` opts it past the VCS-ignore with
  `artifacts = ["src/rya/console/dist/**"]` (the Airflow pattern).
  `packaging/server` gets it free via its whole-tree force-include.
- Absent `dist/` is an ordinary state: the wheel still builds and `/v2` serves a
  503 naming the build command. Never let a missing bundle become a 404 or a
  crash at import time.
- Sourcemaps are **off** for the production build - the map is ~1MB, four times
  the bundle. `npm run dev` has full sourcemaps.

## Layout

```
src/
  main.tsx          mount
  App.tsx           shell: poll, routing (URL hash), auth gate, toasts, nav counts
  styles.css        the design system, lifted VERBATIM from the legacy console
  lib/
    api.ts          the one fetch path; 401 -> auth modal; token vs session creds
    types.ts        hand-written shapes for GET /console and friends
    format.ts       ago/stamp/usd/num/statusClass - pure, tested
    runs.ts         filterRuns/runCounts - pure, tested
    usePoll.ts      usePoll (interval, keeps last good value) + useLoad (on entry)
    nav.ts          the nav as DATA - one entry per view
  components/       ui.tsx (Table/Tile/Empty/...), Sidebar, TopBar, AuthModal
  views/            Overview, Runs, Approvals, simple.tsx (pure-state views)
```

## Conventions

- **`styles.css` is copied from the legacy console on purpose.** Same tokens, same
  class names, so the two consoles look identical and visual risk stays near zero.
  Restyling is a separate change from migrating - do not mix them.
- **No `innerHTML`, no `esc()`, ever.** React escapes text children; that is the
  point. `dangerouslySetInnerHTML` should not appear in this tree.
- **Give `<Table>` a real `rowKey`.** Keyed rows are what let React preserve DOM,
  focus and selection across a poll. An index key silently reintroduces the bug
  this migration exists to fix.
- **Icons are `lucide-react` components**, not `data-lucide` attributes. There is
  no `icons()` call to remember after each render.
- **Views load from the shell's poll** unless they need their own fetch; those use
  `useLoad` (guard/evals/queue/deploy move on a promote, not per second).
- Every panel must degrade calmly on a fresh install: no runs, no versions, no
  workers, no environments are all ordinary states, never outages.

## Migration status

Ported: Overview, Runs & traces, Approvals, Manifest, Models, Channels, Secrets,
Memory, Jobs & cron.

Everything else in `lib/nav.ts` renders `NotYetMigrated`, which links back to the
same view in the legacy console at `/`. To port one: add the component, add a
branch in `App.tsx`, and delete nothing from `/` until parity is reached.

Un-ported: Infrastructure, Tools (kill switches), Knowledge, Connections,
Environments, Versions, Workers, Quota & usage, Conversations, Evals, Queue &
turns, Governance, Action Guard, Team & access.
