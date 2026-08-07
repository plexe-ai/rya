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
  App.tsx           session boundary + shell: poll, routing (URL hash), auth gate,
                    toasts, nav counts
  styles.css        the design system, lifted VERBATIM from the legacy console
  lib/
    api.ts          the one fetch path; 401 -> auth modal; token vs session creds;
                    runtimeInfo() - the cached /v1/info the auth gate reads
    types.ts        hand-written shapes for GET /console and friends
    format.ts       ago/stamp/usd/num/statusClass - pure, tested
    runs.ts         pillCounts/runsSignature + RUN_FILTERS - pure, tested
    usePoll.ts      usePoll (self-scheduling, backoff, abort) + useLoad (on entry)
                    + useNow (the staleness clock)
    refresh.ts      the shell's one refresh signal, in context
    nav.ts          the nav as DATA - one entry per view, plus NavCount
    agent.ts        ag() path prefixing + the remembered selection
  components/       ui.tsx (Table/Tile/Empty/...), Sidebar, TopBar, AuthModal,
                    StaleBanner, ConfirmDialog (destructive actions),
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
  `useLoad` ("on entry, plus Refresh" — guard/evals/deploy move on a promote, not per
  second) or `usePoll` when the table is meant to be live (queue). Runs and Conversations own **paged** fetches:
  the aggregate's `runs` and `sessions` keys are 30- and 50-row PREVIEWS, and treating
  a preview as the dataset is what made a search for an older run id answer "No runs
  match" (§5.1) and conversation 51 unreachable (§5.2). Totals come from `stats`
  (`stats.runs`, `stats.byStatus`, `stats.sessions`), which are computed over
  everything; rows come from `?limit=&offset=` with `count` as the honest denominator.
- **One reader per endpoint, handed down — never a second poller.** `/queue/stats` is
  polled once in `App.tsx` because two things read it: the sidebar badge and the Queue
  tiles. When the view fetched it again on its own it went stale beside a live badge a
  few pixels away (§5.5). If two surfaces need the same numbers, one of them owns the
  fetch and passes the result.
- **An unknown count and a zero are different answers.** `num(undefined)` is `'0'`, so
  a count threaded straight from an unsettled or failed fetch renders as a drained
  queue: "Dead-letter 0" during an outage (§5.4). Model not-known as `null`, render it
  as `—`, and say why — a fabricated zero is the one state that cannot be recovered
  from by looking harder. The sidebar badge is the same rule with a **third** state
  (§5.10, `NavCount`): a number, `—` for a read that failed, and no badge at all for a
  count nobody has attempted yet. That third one exists so a healthy console does not
  flash `— — —` across the nav on its first paint; `{value || ''}` collapsed all three
  into a blank, which is how "0 workers" and "the workers endpoint is down" came to
  look identical one panel away from a route whose own comment forbids exactly that.
- **There is ONE refresh, and it is broadcast.** `lib/refresh.ts` holds a counter in
  context; `usePoll` and `useLoad` subscribe to it themselves, so every loader in the
  mounted tree honours the Refresh button by construction and a new view inherits the
  behaviour by using the hooks it was already using. Do not add a `reload` prop, and do
  not call `refresh()` alongside `refreshAll()` — the shell publishes the signal (so it
  passes its own poll `opts.tick`, since `useContext` cannot see a provider the caller
  renders), and everything else consumes it. Before this, Refresh refetched `/console`
  and nothing else, so on the nine views that own a fetch the most prominent control in
  the console did nothing at all (§5.6). It is bumped for three things only — the
  button, a successful sign-in, and Send test event — never on a timer.
- **The poll is self-limiting, and it is not a `setInterval`.** `usePoll` schedules the
  next tick only once the previous one has SETTLED, backs off exponentially to 60s on
  consecutive failures, skips the request entirely while the tab is hidden (fetching
  immediately on return), and hands every attempt an `AbortSignal`. `/console` opens a
  fresh psycopg connection per request in multi-tenant mode, and the interval version
  gave a struggling database ten new connections a minute per open tab, forever, with
  every earlier answer discarded on arrival (§5.8). A fetcher may ignore the signal, but
  one that forwards it into `api()` gets real cancellation. Note that `failures` and the
  backoff counter are deliberately **not** the same number: an operator's Retry re-times
  the loop without un-knowing a failure.
- **Stale data has an age, and says so.** Keeping the last good value through a failure
  is right; leaving the operator to guess how old it is, is not. `usePoll` exposes
  `lastSuccessAt`, the `live` pill reads "offline · 4m old", and after two consecutive
  failures `StaleBanner` states the age in words with a Retry — permanent while it
  lasts, because the previous notice was a 2.6-second toast on the leading edge and
  nothing afterwards (§5.9). Two failures rather than one on purpose: a banner that
  fires on every blip is one operators learn to scroll past.
- **Never mirror a set the server owns.** `Quotas.tsx` kept its own three-entry copy of
  the launch gate's conditions, so when D32 (topology) was added to `PostureReport`
  nothing failed — the table showed three rows reading "in force" beneath a tile reading
  INCOMPLETE, because `ok` and `unmet` are computed server-side and *did* count the
  fourth (§5.7). The gate now names its own conditions (`PostureReport.conditions`) and
  the console renders whatever it is sent, including a condition it has never heard of.
  Same reasoning as `store.run_matches` in §5.1: when both sides define a set, the
  failure is not an error, it is a quiet disagreement.
- **A session is a component lifetime, not four localStorage keys.** Signing out bumps
  an epoch that `App` keys `Console` on, so React discards the whole subtree —
  the shell's poll data and every view's local detail state, including caches nobody has
  written yet. `clearAuth()` alone left the previous tenant's runs, secrets and traces
  mounted behind a dismissible dialog, and `rya_agent` (not a credential, so never
  cleared) went into the *next* tenant's first request (§5.11). Clear storage BEFORE the
  remount: the new instance reads `getToken()`/`readAgent()` during its first render.
- **Ask the runtime what it wants; do not infer it from this browser.**
  `!getToken()` answered "does the runtime need a credential?" with "does this browser
  have one?", so a default `rya serve` — where `auth_enabled()` is false — opened on a
  modal demanding a token the server neither wants nor checks (§5.12). The gate reads
  `/v1/info.authRequired` through `runtimeInfo()`, cached once per page load and shared
  with `AuthModal` so the two cannot disagree. It **never rejects and never assumes
  open**: an unreachable probe resolves to `{}`, and only an explicit `false` opens the
  door. No loader may fetch before the gate has decided — that is what `canFetch` is.
- **A control shows the operator's choice; a readout shows the server's answer.** The
  agent `<select>` was bound to `loaded.agent.name`, so choosing an agent snapped it
  back for a round trip and left it naming the wrong agent forever if that request
  failed — while `ag()` and localStorage had already moved on (§5.14). `Sidebar` takes
  both `selected` (the choice) and `showing` (the echo) and states the gap out loud
  while it exists, rather than hiding it by reverting the control. Note this is the
  opposite of the view *key* below, and deliberately so: a key wants the echo, a control
  wants the click.
- **A destructive action gets a confirmation, and its audit reason comes from the
  operator.** The tool kill switch wrote versioned, append-only policy state on one
  unguarded click with `reason: 'console kill switch'` hardcoded — filling the only
  field capable of answering *why* with a description of the button (§5.15). Use
  `components/ConfirmDialog.tsx`; it owns the a11y contract (`AuthModal`'s, exactly) and
  the caller owns the fields and gates the confirm button with `confirmDisabled`. Offer
  every tier the server accepts: the legend documented four and the column reached two,
  so `approval_required` was explained at length and unreachable.
- **Read the scope of a field before you aggregate it.** `pinnedRuns` arrives once per
  environment and is agent-wide (`describe_environment` walks every version of the
  agent; `pinned_runs` filters no environment), so summing it across env rows multiplied
  "Retained" by the environment count (§5.13). It is derived once now, in
  `retainedVersionsAcrossAgent`, whose name says the scope; the per-row "Retention"
  column is gone, because a column is read as a property of its row and no header
  wording survives that.
- **A value deduplicates; an event must not.** A React `setState` to the value already
  held is a no-op, so a piece of state that is *both* a datum and a "go" signal stops
  working the second time you press the button — `setSubmitted(query.trim())` meant
  re-running an identical knowledge query produced no request, no spinner and no change
  at all (§5.16). When something needs to happen *again*, put a monotonic counter in the
  dependency list beside the value, the shape `lib/refresh.ts` already uses. Keep one
  path: a repeat and a first attempt are the same action, and an escape hatch for the
  repeat is a second way to start the work for the next change to forget about.
- **Announce a failure on the settle, never on the submit.** `useLoad` clears `error`
  only on success — `reload()` opens with `setLoading(true)` and leaves the standing
  failure in place until the next attempt lands — so an effect keyed on anything that
  changes when a request *starts* re-announces the previous failure for a request that
  does not exist yet. Key it on `loading` going false and guard the in-flight render.
  This is the same finding from both ends (§5.16): the old deps swallowed a repeated
  identical failure *and* toasted a stale one.
- **An unrecognised status is not the opposite status.** `statusClass` returns `''` for
  a word it has not been taught and `StatusBadge` prints the word it was handed, so an
  open vocabulary degrades to neutral-and-verbatim. Connections hand-rolled the inverse
  — a two-arm ternary on `=== 'active'`, which is total over `boolean` and not over
  `string | null | undefined` — so an absent status and a value from a newer server both
  came out as a red "revoked", a claim the runtime never made (§5.17). Assign a tone only
  to words you understand; quote a word you do not; and keep the map local when the
  domain has its own vocabulary, as `Team.tsx` and `Quotas.tsx` already do. A status
  *you* supply rather than the server is the one that needs a tone — and, like any
  unknown here, a `title` saying what it costs.
- **View-local detail state belongs to one agent.** The view subtree is keyed on
  `loaded.agent.name` in `App.tsx`, so a switch remounts it and every open trace,
  thread, turn, environment, version, eval-result and Guard draft goes with it. Keyed
  on the server ECHO, not the pending selection: the poll hands every view a brand-new
  `state` object every 6s, and a key that noticed would blank a panel the operator is
  reading. Views therefore do not need their own agent-change reset effects — see
  `App.agentSwitch.test.tsx`, which pins both halves.
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
