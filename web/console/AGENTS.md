# `web/console` - the React operator console

Vite + React + TypeScript. Source lives here; the compiled bundle lands in
`src/rya/console/dist/` and ships inside the wheel. Served at **`/`** by `rya serve`,
with assets mounted at `/assets`; **`/v2`**, its address during the migration,
308-redirects to `/`.

## Why this exists

The console it replaced (`src/rya/console/index.html`, now deleted) was 120KB of
inline JS building HTML with 62 `innerHTML` assignments. It worked, and it was
disciplined about `esc()`, but it hand-rolled reconciliation: the 6s poll would clobber
focused inputs, so `renderRuns` sniffed `document.activeElement` and skipped
re-rendering whenever the search box had focus. That workaround is the bug class this
migration removed - see `src/views/Runs.test.tsx`, which pins the behaviour down.

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
- Absent `dist/` is an ordinary state: the wheel still builds and `GET /` serves a
  503 naming the build command. Never let a missing bundle become a 404 or a crash at
  import time - and it matters more now than it did at `/v2`, because there is no
  second console to fall back to.
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
    agent.ts        ag() path prefixing + the remembered selection
  components/       ui.tsx (Table/Tile/Empty/...), Sidebar, TopBar, AuthModal,
                    AgentChooser (the `agent: null` state)
  views/            one file per view; simple.tsx holds the pure-state ones, and
                    Deploy.tsx holds Environments/Versions/Workers because they share
                    one drill-down graph
```

## Conventions

- **`styles.css` was lifted verbatim from the legacy console on purpose.** Same tokens,
  same class names, so the port carried no visual risk. It is now the only stylesheet;
  restyling was always meant to be a separate change from migrating, and still is.
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
- **`GET /console` can return `agent: null`, and that is not an error.** The route
  says so in words, and it happens in two ordinary situations: a workspace with
  nothing published, and a workspace serving several agents with none selected (the
  server only auto-selects when there is exactly one). The two responses are two
  *types* — `ConsoleState` (agent guaranteed) and `ConsoleRoster` — because the
  agent-less payload carries none of `tools`/`stats`/`runs`, so a single optional-field
  type would compile and then throw. The shell narrows with `hasAgent()` and renders
  `AgentChooser`; **views always receive `ConsoleState`** and never need to guard.
- **`agent.handlers` is nullable too.** It is `null` whenever the control plane holds
  no loaded agent module, which since D21 is every published bundle — the api never
  imports one, so it cannot know what the code registers. Three states, not two:
  present, absent, *not introspected*. Rendering "no handler" for the third accuses a
  working agent of having none.
- **Every agent-scoped call goes through `ag(path)`** — `/agents/${selected}${path}` —
  exactly as the legacy console does. The unprefixed spellings still answer via the
  deprecated Rule 6 fallback, which resolves the `_` alias and therefore **400s with
  `E_AGENT_AMBIGUOUS` the moment a workspace serves a second agent**. A hard-coded
  `/events` is a bug with a delayed fuse, not a shortcut.
- **A selection change must `refresh()` explicitly.** `usePoll` keeps its fetcher in a
  ref so that changing it never restarts the interval; that is deliberate, and it means
  switching agents would otherwise sit unapplied for up to a poll period while the page
  showed one agent's runs under another's name.
- A remembered agent that the workspace does not serve is **recoverable, not an
  outage** — it is what happens when the same browser is pointed at a different
  tenant. `ApiError.code === 'E_AGENT_NOT_FOUND'` on `/console` clears the selection
  and retries once. That is why `ApiError` carries the code and not just the message:
  branching on human-facing prose is not a contract.

## Migration status — complete

All 23 views are React components. The legacy single-file SPA
(`src/rya/console/index.html`) and its vendored `lucide.min.js` are **deleted**, which
was the stated condition for removing them: "delete nothing from `/` until parity is
reached."

Three things that removal bought outright rather than merely tidied:

- **The CSP dropped `script-src 'unsafe-inline'`.** A console whose whole application
  is one inline `<script>` cannot have that; a bundle of external modules can.
- **~530KB of committed vendored JavaScript left the repo and the wheel.** Icons are
  `lucide-react` imports, tree-shaken into the bundle.
- **`NotYetMigrated` is gone**, and with it the fallback arm that made an unwired view
  look like a deliberate state. `App.tsx`'s view chain now ends in a `never`-typed
  branch, so adding a nav entry without a component is a **build error**;
  `App.test.tsx` mounts every `ALL_VIEWS` id as the runtime half of the same check.

### Ported in the final batch

Infrastructure, Tools (kill switches), Knowledge, Connections, Environments, Versions,
Workers, Quota & usage, Conversations, Evals, Queue & turns (with a resumable
turn-stream inspector), Governance, Action Guard, Team & access.

Two decisions from that batch worth knowing before you change them:

- **The port fixed latent bugs rather than reproducing them.** The legacy console
  called `/tools`, `/tools/{id}/permission`, `/knowledge/search`, `/guard`, `/evals`,
  `/evals/run`, `/gate` and `/gate/check` **unprefixed**. Those spellings resolve the
  `_` alias and 400 `E_AGENT_AMBIGUOUS` on a two-agent workspace, so every one of them
  is agent-prefixed here. Legacy Team & access was worse: it pathed on
  `viewer.workspace`, the display *name*, where the route keys on the id — so it 403'd
  for any workspace that had been named. Use `viewer.workspaceId`.
- **No promote / rollback / retire controls, deliberately.** The legacy console had
  none either. Promotion is a gated, audited, actor-attributed action and it belongs to
  `rya promote` / `rya deploy --env` / `rya versions retire`, where the actor is a real
  identity rather than whoever had the tab open. These views report the pointer; they
  do not move it.

### Known follow-ups

- `lib/types.ts` is narrower than what `snapshot.py` actually sends: `ConsoleState`
  omits `sessions`, `infra` is `unknown`, and `Tool` / `Knowledge.documents` /
  `connections` are missing fields the server returns. Views narrow locally with
  documented casts; the shapes belong in the shared type.
- `lib/api.ts` could use `apiStream()` (the turn-stream reader is a local `fetch`, so
  its 401 does not raise the auth modal), plus session-authenticated `GET`/`DELETE`
  and a `clearSession()` that drops only `rya_session` — `clearAuth()` also drops the
  workspace key, which is wrong on a stale-session 401.
- `examples/crizac/rya.guard.yaml` writes its rules with `url:` where the matcher
  reads `pattern:`, so all three compile to `startswith("")` and allow everything
  under a `default: deny` policy.
