# Rya Operator Console — Functional Audit

**Date:** 2026-08-07 · **Last updated:** 2026-08-07 (remediation in progress)
**Scope:** `web/console` (React operator console), its contract with `src/rya/api/app.py`, and the build/serve/release path.
**Method:** Six parallel audits — four over the view tree, one reconciling every console HTTP call against every server route, one running the real build/test/serve/security checks. Every finding below was verified against source on both sides; the highest-severity ones were re-verified independently.

---

## 0. Remediation status

Every **P0 release blocker is now closed**, and the P1 that was masking the rest with it.
Each fixed finding carries a `> **FIXED 2026-08-07**` note in place saying what changed and
why — the original diagnosis is left intact above it, unedited, so the reasoning stays
auditable and any wrong call in it stays visible.

| # | Finding | Status |
|---|---|---|
| 3.1 | No CI exists | ✅ Fixed — `.github/workflows/{ci,release}.yml` |
| 3.2 | Dockerfile never builds the console; no `.dockerignore` | ✅ Fixed — `node:20-slim` builder stage, allowlist `.dockerignore` |
| 3.3 | Saving the guard policy destroys unmodelled keys | ✅ Fixed — `toPolicy` round-trips; `url:` migrated |
| 3.4 | No ErrorBoundary anywhere | ✅ Fixed — `components/ErrorBoundary.tsx`, two boundaries |
| 3.5 | `?api=` sends the workspace API key to any origin | ✅ Fixed — compiled out of production builds |
| 4.1 | The error envelope split | ✅ Fixed — **one envelope** for the whole product |
| 4.2 | Every 401 is misreported as a stale token | ✅ Fixed — 401s are classified, and say why |
| 4.3 | The user-identity flow is entirely unwired | ✅ Fixed — console wired **and** an MT server gap closed |
| 4.4 | Approvals: wrong list, and the operator approves blind | ✅ Fixed — arguments rendered; inbox labelled, not hidden |
| 4.5 | Governance reads two dead data sources | ✅ Fixed — reads what enforces; the audit hash now covers it |
| 5.1 | Runs/filters/search see only the newest 30 runs | ✅ Fixed — server-side paging; pills read `stats` (§5a) |
| 5.2 | Conversations capped at 50; transcript never re-fetches | ✅ Fixed — all four defects (§5a) |
| 5.3 | Detail state survives an agent switch | ✅ Fixed — the view subtree is keyed on the agent (§5a) |
| 5.4 | Queue tiles render "0" during load and outage | ✅ Fixed — unknown is `—`, and says why (§5a) |
| 5.5 | Queue never refreshes while its badge polls | ✅ Fixed — one poll, two readers (§5a) |
| 5.6 | Deploy/Versions/Workers/Quotas discard `reload` | ✅ Fixed — one refresh signal, broadcast (§5b) |
| 5.7 | The launch gate has four conditions; the console renders three | ✅ Fixed — the gate names its own conditions (§5b) |
| 5.8 | `usePoll` has no in-flight guard, abort, backoff or visibility check | ✅ Fixed — the loop schedules itself (§5b) |
| 5.9 | A persistently failing poll is masked forever | ✅ Fixed — stale data carries its age (§5b) |
| 5.10 | Sidebar counts render real zero and unknown identically | ✅ Fixed — three states, three renderings (§5b) |
| 5.11 | Sign-out leaves the previous tenant's data on screen | ✅ Fixed — a session is a component lifetime (§5c) |
| 5.12 | The auth gate opens even when the runtime requires no auth | ✅ Fixed — the runtime decides, not the browser (§5c) |
| 5.13 | "Retained" multiplies by environment count | ✅ Fixed — one agent-scoped set, derived once (§5c) |
| 5.14 | The agent `<select>` is bound to the server echo | ✅ Fixed — the control shows the choice, and names the gap (§5c) |
| 5.15 | Kill switch is destructive, unconfirmed, reason hardcoded | ✅ Fixed — confirmed, four tiers, the operator's reason (§5c) |
| 5.16 | Re-running the same knowledge query does nothing | ✅ Fixed — the query is a value, the submit is an event (§5d) |
| 5.17 | A connection with no status renders as "revoked" | ✅ Fixed — three states; an unknown word is quoted, not translated (§5d) |
| 5.18 | CORS omits DELETE | ✅ Fixed — and a drift test now walks the router (§5d) |
| 5.19 | `index.html` has no `Cache-Control` | ✅ Fixed — immutable assets, a revalidated index (§5d) |
| 5.20 | CSP `connect-src` carries bare `ws: wss:` | ✅ Fixed — `connect-src 'self'` (§5d) |

**Test counts moved with the fixes:** console **164 → 377** (30 files), Python **899 → 992
passing, 1 skipped** (the skip needs a real `ANTHROPIC_API_KEY`; the 58 `@needs_pg` skips are
gone — a local Postgres now backs that suite, see §4.3), plus **+10** for the paged-listing
contract (`tests/test_api.py`), **+4** for the launch gate (§5b), and 2 more withheld-method
assertions in `test_broker.py`; TypeScript SDK **56 passing**. Bundle 336 kB → **356.5 kB**
(99 → **106.1 kB** gzip); `tsc --noEmit` clean, `vite build` 0 warnings, 0 `act(...)`
warnings, `scripts/smoke_console.sh` **15/15** against a live server on the rebuilt bundle.

**Still open and worth reading:** §4.6–§4.10, §5.21–§5.27, and the stub inventory in §6. Three
notes on how the fixes above changed them:

- **The approvals story is now closed end to end.** §4.3 restored *who* is approving, §4.4
  restored *what* they are approving, and §4.2 made the refusal readable when a deployment
  declines. The gate that had no test now has 17.
- **The governance surface now reads what enforces** (§4.5), which also **narrows §4.6**: the
  guard-file half of it is resolved wherever policy is store-backed, i.e. every deployed case.
  What survives there is the manifest, `.env` and branding halves, plus the file fallback in a
  mounted dev tree.
- **The three serving-path findings are closed together** (§5.18–§5.20, see §5d), which is
  also the answer to the question they raise: all three were invisible in the only deployment
  anyone develops against. `scripts/smoke_console.sh` grew from 11 checks to 15 and is the
  thing that will notice next time — it asks a live process what came back rather than
  asserting what the code meant to send.

---

## 1. Verdict

**Can this console be deployed live today? No — but it is much closer than a stub audit would suggest.**

The headline result is that **there is almost no scaffolding here.** Every one of the 23 views is a real component bound to a real endpoint. There are no `TODO` handlers, no `NotYetMigrated` fallback, no placeholder screens. The React port reached genuine nav parity with the deleted legacy console — the old `views` array (`d124ce9^:src/rya/console/index.html:348`) lists exactly the same 23 ids as `web/console/src/lib/nav.ts`.

What blocks going live is three different things, none of them "unfinished UI":

1. **No release automation at all.** There is no CI, and the Docker image never builds the console. Both failures are *silent* by design.
2. **A cluster of contract mismatches** where a view trusts a shape, a field, or a data source the server does not actually provide. These do not crash — they render confidently wrong answers, several of them about governance state.
3. **Three security defects** in the serving and credential path.

The single most dangerous finding is not a crash. It is that **pressing "Save policy" in the Action Guard editor can silently disable the grounding gate, wipe all secrecy redaction patterns, and — on a project using the legacy `url:` rule spelling — delete every allow rule under a `default: deny` policy**, producing a total egress lockout, with no error shown.

> **UPDATE 2026-08-07** — All three blockers above are now closed (§0). (1) is done, and (3) is
> down to the CSP-coverage hardening noted under §3.5. What remains is (2), the contract
> mismatches. §4.4 held that title, then §4.5; both are now fixed, and **§4.7 carries it** —
> not for its dead `signedIn` flow, which is merely broken and obvious, but because
> **removals and key revocations toast success when the server refused them**. Every finding
> in this line is the same error: a surface asserting something the system does not know to be
> true. §4.4's operator approved an action they could not see; §4.5's read "not enforced" off a
> file nothing enforced; §4.7's believes a compromised key is dead. Note that until §4.1
> landed, several of these were partly invisible — their failures arrived as the string
> `HTTP 400`.
>
> §4.2 is closed too, and it sharpens §4.4 rather than resolving it: bank mode's refusal to
> accept an anonymous approval now reaches the operator in the server's own words instead of as
> a demand to re-paste a working key. But being told clearly *why* an approval cannot be
> resolved is not the same as being able to resolve one — that still needs §4.3's `/v1/token`
> wiring — and it does nothing about approving blind, which is §4.4's real danger.

---

## 2. What is genuinely good

Stated plainly, because it constrains what needs doing:

- **Build health is clean.** `tsc --noEmit` → 0 errors. `vitest` → **164/164 pass**, 0 skipped. `vite build` → 0 warnings, 336 kB JS / 99 kB gzip, sourcemaps off, `npm audit` clean. *(2026-08-07: now **282/282** across 22 files; 348.0 kB / 103.1 kB gzip.)*
- **Packaging contract holds end-to-end.** `dist/` is gitignored, untracked, force-included via `artifacts`, and verified present inside a freshly built server wheel. The absent-`dist/` 503 arm behaves exactly as documented — never a 404, never an import-time crash.
- **Addressing discipline is perfect.** Of 42 distinct console call templates, there are **zero 404/405s** and **zero uses of a deprecated unprefixed spelling**. Every agent-scoped call goes through `ag()`.
- **The SSE turn-stream inspector is excellent.** Frame parsing, chunk-straddling, resume-from-cursor, terminal-status set, and reader teardown all match the server byte for byte.
- **`script-src 'self'` with no `'unsafe-inline'`** — the AGENTS.md headline claim is true in the live header.
- **No secrets or hardcoded origins in the bundle.** Scanned for 14 credential/host patterns: zero hits. `lucide-react` tree-shakes to 74 of 1748 icons.
- **15 of 17 test files assert real behaviour**, not "renders without crashing" — focus preservation across a poll, agent-prefixed routing, 401-is-not-an-outage, and XSS-as-text on hostile ids in eight views.

---

## 3. P0 — Release blockers

### 3.1 No CI exists
`.github/` does not exist; zero workflow files are tracked. `scripts/build_console.sh` is correct and executable but **nothing calls it**. The 164 tests have never run outside a developer's laptop, and `uv build --wheel packaging/server` will happily ship a wheel with no console — silently, because a missing bundle is a designed-legal 503 state.

**Fix:** A PR job running `npm ci && npm run typecheck && npm test`, and a release job running `scripts/build_console.sh` *before* the wheel build, with the `zipfile` assertion from `docs/PACKAGING.md:98`.

> **FIXED 2026-08-07** — `.github/workflows/ci.yml` (jobs: `console`, `python` on 3.10 + 3.12, `wheel`, `image`, `deps-unlocked`) and `.github/workflows/release.yml`. The wheel assertions were negative-controlled: a server wheel built with `dist/` absent builds *cleanly* and is caught only by the assertion. `python` installs with `uv sync --locked` — an unlocked resolve of `mcp>=1.2.0` picks up mcp 2.0, which dropped `streamablehttp_client` and fails three tests, so `deps-unlocked` is scheduled-and-advisory to surface that drift on purpose rather than during a PR.

### 3.2 The Dockerfile never builds the console; no `.dockerignore`
`Dockerfile` has no Node stage (verified: no `node`, `npm`, or `console` reference anywhere in it). `src/rya/console/dist/` is gitignored, so a clean clone produces a container serving a 503 at `/`. With no `.dockerignore`, a machine where a developer *has* built bakes a stale, unversioned `dist/` from the working tree into the image — non-reproducible. `docker compose up`, the documented self-host path, is broken either way.

**Fix:** Add a `node:20` builder stage calling `scripts/build_console.sh`, `COPY --from=builder`, and a `.dockerignore` covering `web/console/node_modules`, `.venv`, `.git`, `.env*`.

> **FIXED 2026-08-07** — `node:20-slim` builder stage (Debian, not Alpine: rollup/esbuild resolve platform-specific optional deps) writing to `/build/src/rya/console/dist`, `COPY --from=console`, and a build-time assertion that the bundle reached the *installed* package rather than just the build context. `.dockerignore` is an allowlist (`*` then `!`), because the only reliable way to keep a developer's stale `dist/` out of `COPY src ./src` is to not send it to the daemon at all. Verified end to end: image builds, container serves, `scripts/smoke_console.sh` passes 11/11; with the bundle removed the same image fails the smoke test and names the cause.

### 3.3 Saving the guard policy destroys unmodelled keys
`Guard.tsx:155-174` (`toPolicy`) builds a **fresh** five-key document `{ssrf, default, fail, policy, rules}`. `save_policy` (`guard.py:405-435`) does `dict(policy or {})` — a replace, **not a merge**. Any other top-level key is deleted.

`grounding` and `secrecy` are both real top-level keys (`guard.py:649`, `guard.py:204`), and both shipped examples set `grounding: {enabled: true}`.

**Compounding it:** `examples/crizac/rya.guard.yaml` writes rules with `url:` where the matcher reads `pattern:`. `toDraft` (`Guard.tsx:136`) reads `r.pattern ?? ''` → empty; `toPolicy` (`:161`) then **drops patternless rows**. Saving that policy deletes all three allow rules and leaves `default: deny` with an empty allowlist — total egress lockout, from a button labelled "Save policy", with no error.

**Fix:** Spread the loaded document in `toPolicy` so unmodelled keys round-trip; normalise `url:` → `pattern:` in `toDraft` with a migration banner; block save (don't silently drop) on patternless rows. Add a test that `grounding`/`secrecy` survive a save.

> **FIXED 2026-08-07** — The root cause was the *type*, not the function: `GuardPolicyDoc` was a closed five-key interface, which is a compile-time promise that no other key exists, so `tsc` green-lit a literal that deleted `grounding` and `secrecy`. It now carries `[key: string]: unknown`, `Draft` holds the loaded doc as `base`, and `toPolicy` spreads it — closing the whole class of key, not the two known ones. `save_policy` was left alone deliberately: replace-not-merge is correct for a versioned write that computes an added/removed/changed diff, and a server-side merge would make deleting a key impossible through the API. Ownership of the merge is now assigned rather than left in the gap between the two sides.
>
> `toDraft` reads `r.pattern ?? r.url ?? ''` and counts legacy rules; a banner states the part that matters — those rules match **every** URL today, so saving *tightens* egress. A pending migration seeds the baseline to a sentinel so Save is live, otherwise the banner would point at a disabled button. Patternless rows are emitted (so the dirty check sees them) and **block** the save with "N rules need a pattern" rather than vanishing.
>
> Tests: 4 in `Guard.test.tsx`, including one that `grounding`, `secrecy` and an arbitrary `futureKey` survive a save, and one running the real `examples/crizac` fixture. The pre-existing `drops a rule with no pattern…` test encoded the old behaviour (the §9 "fixtures encode bugs" pattern) and was rewritten. All three new tests were verified to **fail** against the unfixed file.

### 3.4 No ErrorBoundary anywhere — one render throw white-screens the console
`grep -rn "ErrorBoundary|componentDidCatch|getDerivedStateFromError|onUncaughtError" src/` returns **nothing**. React 19 unmounts the entire tree on an uncaught render error: no sidebar, no nav, no recovery, and a reload reproduces it because the hash still points at the broken view.

This is exactly the failure mode in the screenshot at the repo root (`Cannot read properties of null (reading 'name')`) — except that screenshot is the *legacy* console, which caught it and showed a toast. The React version is strictly worse.

Made sharper by `hasAgent()` (`types.ts:230`) narrowing on `!== null`, so an `undefined` agent passes the guard and every `loaded.agent.name` dereference throws.

**Fix:** A `ViewErrorBoundary` keyed on `view` (so navigating away resets it) plus a root boundary in `main.tsx`. Narrow `hasAgent()` on the fields consumers actually dereference.

> **FIXED 2026-08-07** — `components/ErrorBoundary.tsx`: one class (class-only because `getDerivedStateFromError`/`componentDidCatch` still have no hook equivalent in React 19) plus `ViewErrorBoundary` inside the shell and `RootErrorBoundary` in `main.tsx`.
>
> Two departures from the suggested fix, both deliberate. **`resetKeys`, not `key`** — a `key` on the boundary would remount healthy children on every navigation, and the shell already gives each view its own component identity; `resetKeys` clears only the *error*, which also made adding `agent` to the reset set free. And the **component stack is shown**, collapsed: `sourcemap: false` means the JS stack is minified noise, while the component stack names the view. `RootErrorBoundary` sets `location.hash = 'overview'` *before* reloading — the hash is the route, so a bare reload lands back on whatever threw. Neither boundary touches the stored token.
>
> `hasAgent` now probes `typeof r.agent?.name === 'string'`. The old `!== null` passed `undefined`, a missing key, **and** `agent: {}` — all three threw during render.
>
> Tests: 10 in `ErrorBoundary.test.tsx` (containment, retry, reset-on-key-change *and* the negative case — an unrelated re-render must not clear it, or it loops) and 2 in `App.boundary.test.tsx`, which mocks `views/Guard` to throw the exact message from the repo-root screenshot and asserts the sidebar survives, that navigating away clears it, that going *back* re-enters the broken view rather than latching clear, and that deep-linking to `#guard` recovers. Both integration tests were verified to fail against the un-boundaried `App.tsx` — the throw escapes `fireEvent.click` and the tree dies.
>
> **Remaining gap:** React boundaries catch render, lifecycle and constructor throws only — *not* event handlers or async callbacks. Those still need the `try/catch` → toast pattern the views already use. This closes the white-screen class, not every error path.

### 3.5 `?api=` sends the workspace API key to any origin
`api.ts:11`: `export const API = (new URLSearchParams(location.search).get('api') || '')` — the API base is read from the query string **unconditionally, in the production bundle**, and `api()` attaches `Authorization: Bearer <rya_sk_…>` to every request against it.

`https://console.example.com/?api=https://evil.tld` exfiltrates a credential that can approve actions, edit the Action Guard, and flip kill switches.

The only thing containing this is `connect-src 'self'` — and **`_CONSOLE_HEADERS` is applied at only three places** (`app.py:680, 707, 709`): the `/assets` mount and the `/` route. Any deployment fronting `dist/` with nginx/S3/CloudFront has **no CSP at all** and the hole is live.

**Fix:** Gate on `import.meta.env.DEV`, or reject anything matching `^[a-z]+:` or `^//`. The Vite proxy already covers the dev use case.

> **FIXED 2026-08-07** — `export const API = import.meta.env.DEV ? devApiBase(location.search, location.origin) : ''`. Two layers.
>
> **Compiled out, not merely rejected.** Vite replaces `import.meta.env.DEV` with the literal `false`, the ternary folds, and `devApiBase` is left unreferenced for the bundler to drop. Verified against a real build rather than assumed: the production bundle contains zero occurrences of the warn string, the function name, the loopback hostnames, and **`URLSearchParams` at all** — there is no longer any code path in the deployed artefact that reads the query string. A runtime-only guard would still have shipped the parsing.
>
> **Destination-checked, not pattern-matched.** In dev, `devApiBase` resolves the input with `new URL(raw, origin)` and allows it only if the result is same-origin or a loopback host over http/https. The suggested regex (`^[a-z]+:` / `^//`) misses `https://127.0.0.1@evil.tld`, where userinfo makes it *read* as loopback while `hostname` is `evil.tld`; that case is in the tests.
>
> Fixing it at `API` covers all six call sites, including `Queue.tsx:222`'s SSE reader. Note the blast radius was slightly wider than described: `sessionPost` and `Team.tsx` send the **account session token**, not the workspace key.
>
> Tests: 4 added to `api.test.ts`, including the exploit itself (asserting the bearer went to *us*), the dodging spellings, what must keep working, and inertness in a production build. Verified to fail against the unfixed file (`expected 'https://evil.tld' to be ''`) while the three pre-existing dev-override tests still pass.
>
> **Still open (now a general hardening item, not this bug):** `_CONSOLE_HEADERS` is applied at only three places, so an nginx/S3/CloudFront deployment of `dist/` has no CSP. That no longer contains anything here, since `?api=` does not exist in the production bundle. The portable fix is a `<meta http-equiv="Content-Security-Policy">` that travels with the bundle — left out because Vite serves the same `index.html` in dev, where `connect-src 'self'` would break the loopback override. It needs its own decision about dev exemptions. §5.20's bare `ws: wss:` **is now closed** (§5d), which tightens the directive this depends on wherever the CSP *is* applied — and sharpens what is still missing here, because a `dist/` behind nginx gets no `connect-src` at all, tight or not.

---

## 4. P1 — High severity

### 4.1 The error envelope split: every `RyaError` reaches the operator as "HTTP 400"
The server emits **two incompatible error shapes**. `HTTPException(detail={...})` serialises as `{"detail": {...}}`. But `_rya_error_handler` (`app.py:612-613`) returns `exc.to_dict()["error"]` — `{code, message, hint, exit_code}` at the **top level with no `detail` wrapper** (`errors.py:167-176`).

`api.ts:94` reads only `body?.detail?.message || body?.detail || message`. For the top-level shape that resolves to the literal fallback `HTTP ${status}`. The `code` survives via `?? body?.code` on line 95; **the message and hint do not.**

So the entire quota, governance, versioning and agent-addressing failure vocabulary — `E_AGENT_AMBIGUOUS`, `E_AGENT_NOT_FOUND`, `E_QUOTA_EXCEEDED`, `E_TOOL_PERMISSION_DENIED`, `E_PROMOTION_BLOCKED`, `E_JOURNAL_DRIFT` — surfaces as a bare status line. `Plane.sole_agent` even names the candidate agents in its hint; the operator never sees it.

This silently degrades every other finding in this report. **Two-line fix**, and it should be first.

> **FIXED 2026-08-07** — Not with a two-line parser fix. **The envelope was unified at the source**, because a cleverer client parser leaves the underlying defect — two error conventions in one service — in place for the next client.
>
> **The audit undercounted the problem.** Probing a live `build_app()` found **four** reachable shapes, not two: `{"detail": {…}}`, `{"detail": "Not Found"}` (Starlette's own 404/405), the bare error object, and `{"detail": [{loc, msg}]}` (FastAPI validation — which the console rendered as the literal string **`[object Object]`**, confirmed by running the expression). And **`hint` was dropped for *every* shape, not just the bare one** — `ApiError` had no field to hold it, and `grep hint web/console/src` returned zero non-test hits. The field the spec calls first-class DevEx had no client-side representation at all.
>
> **Root cause: the HTTP envelope had never been written down.** `docs/devex.md` specified the CLI's, `DEEP_DIVE.md` the same, `mcp.md` the MCP one — HTTP was never specified, which is exactly how two conventions grew beside each other unnoticed. The `api.ts` header comment even asserted the wrong one ("FastAPI reports errors as `{detail: {message}}`"), which is true of `HTTPException` and of nothing else the server raises.
>
> **Chosen shape** (`{ok: false, error: {code, message, hint, exit_code}}`) is the product-wide one — byte-identical to `RyaError.to_dict()`, to `rya … --json`, to an MCP reply and to a broker reply. One envelope to learn, one to document. Evidence weighed: the test suite had a de facto contract of 17 assertions on `["detail"]["code"]` against 2 on the top-level shape, which argued for the FastAPI-native option; product coherence won, at a cost of 18 mechanical test updates.
>
> **Server:** three handlers in `api/app.py` are now the only place a shape is decided — `_rya_error_handler`, `_http_error_handler` (dict *and* string arms) and `_validation_error_handler`. **Route code is unchanged**: all 68 `HTTPException` call sites still raise what they always raised. Two things fell out: errors that never had a code now have one (a bare 404 is `E_NOT_FOUND`, `exit_code: 4`), via a status→code map mirroring `clients/typescript/src/errors.ts: inferCode` so server and SDK cannot disagree; and response headers survive the rewrap (Starlette's 405 carries `Allow`), which has its own test.
>
> **Client:** four copies of the parse — `api()`, `authPost()`, `sessionPost()` and `Team.tsx`'s `sessionFetch` — collapsed to one `readError`. The latter three were weaker still (no string arm), so signup, login and every account operation reported `HTTP 400` for refusals that had a good message. `ApiError` gained `hint`, composed into `.message` so it reaches the operator without touching 23 views. A small non-contract fallback remains for bodies that aren't ours — an nginx 502 or CloudFront error page will never carry this envelope.
>
> **The SDK needed no change.** `clients/typescript` already had a *passing* test for exactly this envelope (`errors.test.mjs:43`); its `unwrap()` was built shape-tolerant. The SDK was ahead of the server.
>
> Tests: `tests/test_api.py::_assert_envelope` is the executable contract and asserts `set(body) == {"ok", "error"}`, so a route regressing to `detail` fails loudly; 6 new Python tests, 21 existing assertions converted across 5 files, 11 error fixtures corrected across 9 console test files (leaving alone the `detail` keys that are legitimate payload fields — governance violations, posture probes, eval and gate checks), and an `api.test.ts` case asserting the **retired** shapes no longer resolve to something plausible. Cross-checked by capturing real error bodies from a live `build_app()` for all four raise paths and feeding them through the actual console parser.
>
> `docs/devex.md` now has a **One error envelope** section — the shape, the handler table, the status→code map, and a history note, since the missing spec *was* the root cause.

### 4.2 Every 401 is misreported as a stale token
`api.ts:84-87` fires the unauthorized handlers and throws without reading the body. The server returns 401 for several distinct conditions, including `E_APPROVER_IDENTITY_REQUIRED` and an expired user JWT. Non-credential 401s raise the Connect dialog over the operator's work and demand they re-paste a key that was never the problem.

> **FIXED 2026-08-07** — 401s are now **classified** rather than assumed, and a credential 401 says *why*.
>
> **The precise defect:** the 401 branch sat *above* `if (!r.ok) throw await errorFrom(r)`, so a 401 never reached the envelope parser §4.1 had just built — and `UnauthorizedError` took **no constructor arguments**, so there was nowhere to put a message even if one had been read. One fact ("the status is 401") produced two conclusions — *your credential is stale* and *re-pasting it is the fix* — and only the first is entailed by a 401 at all.
>
> **The audit undercounted the conditions.** It names two; `app.py` has **nine** raise sites, five reachable from the console. Read on both sides: `_check_token:253`, `authorize:321`, `_identity_from:341` (`"JWT required."`), `_identity_from:348` → `verify_jwt` (**six** different messages under one code — malformed, expired, bad signature, no `sub`, missing `[auth]` extra, no verifier), and `_actor_from:371`. That last one is called at exactly two sites, `approve:1925` and `reject:1955`.
>
> **The fix: `unauthorizedError(body)` in `lib/api.ts`, one decision, two sites.** `E_UNAUTHORIZED` → `UnauthorizedError` (dialog opens, as before) now carrying `reason`, `code`, `hint`, `status`. Anything else → an ordinary `ApiError`, and the dialog **stays shut**. It is exported because the Queue's SSE reader (`Queue.tsx:226`) runs its own `fetch` and threw a hand-rolled `new Error('unauthorized')`; a second copy of this decision is exactly how §4.1's four-way parse split came about.
>
> **The default cuts on whether a code is PRESENT, not on whether we recognise it.** No code — a bodiless 401, a gateway's HTML, an SSO proxy — is a credential failure, which is the pre-existing behaviour and the only safe reading of a bare status. An *unrecognised* code is not: a code means Rya answered and chose to say something specific, so the honest move is to show it. Erring the other way would re-create this bug for the next 401 code anyone adds.
>
> **`UnauthorizedError.message` deliberately stays the literal `'unauthorized'`.** Five views — `Evals`, `Guard`, `Quotas`, `Deploy`, `Queue` — switch their empty state on `error === 'unauthorized'`, because `useLoad` hands them a string and not the error object. That sentinel is the *identity* of the condition; the server's prose rides in `reason`. Putting prose in `message` would have silently turned all five into `"Evals unavailable — Missing or invalid operator token."` A non-credential 401 is an `ApiError`, so those same views fall through to their real-message arm **with no per-view change**.
>
> **What the operator now sees.** The Connect dialog renders the reason (`AuthModal` gained a `reason` prop, `.anote` in `styles.css`). This matters most where the dialog's static copy is *wrong*: with `RYA_JWT_SECRET` set, `get_plane:392` skips `_check_token` entirely, so "This runtime requires an operator token" and the `rya_sk_… or operator token` placeholder both name a credential that cannot work, while the server is plainly saying `"JWT required."` It also separates `"JWT is expired."` (sign in again) from `"JWT signature verification failed."` (worth investigating) — same code, so `code` alone does not distinguish them and `reason` is the only discriminator. `HTTP 401` is **never** shown as a reason: a status line is not an explanation, which is why `ErrorEnvelope` gained `fromServer`.
>
> **One more misclassification in the same family, found while fixing this.** `App.tsx`'s `onPollError` suppressed the "Lost connection to the runtime" toast by comparing `e.name === 'UnauthorizedError'`, and `usePoll`'s `unauthorized` flag tested the same class. Both mean *"is this an outage"*, whose answer is no for **every** 401 — so once a 401 could also arrive as an `ApiError` they would have reported a perfectly healthy server as unreachable. Both now use an exported `isUnauthenticated`.
>
> **Not fixed, and deliberately out of scope:** the dialog can still re-open in a loop on a runtime that 401s every request (close → poll → 401 → open, since `usePoll` is gated on `!authOpen`). The loop is now *navigable* — it says what credential it wants — but it is still a loop. Fixing it means deciding when a dismissal should stick, which is a UX decision, not a bug fix.
>
> Tests: **+15** (console 210 → 224). `api.test.ts` 32 → 43 covering the sentinel, `reason`/`code`/`hint`, the bodiless-401 fallback, the present-but-unrecognised code, `E_APPROVER_IDENTITY_REQUIRED` and `E_BAD_SIGNATURE`, plus `unauthorizedError()` directly against all six `E_UNAUTHORIZED` messages. New `App.unauthorized.test.tsx` (3) drives the real approve button in bank mode and asserts the toast names the remedy while **no dialog opens**. Verified by reverting the 401 branch in place: 5 of the `api.test.ts` cases and 2 of the 3 App cases fail against the old code.
>
> Also corrected: the stale `Team.tsx:68` comment still claiming `{detail:{message}}` unwrapping, contradicted by §4.1's change three lines below it.

### 4.3 The user-identity flow is entirely unwired
Nothing in the console calls `POST /v1/token`, and `X-Rya-User-Token` is never sent. Consequences:
- Every approval is recorded with `resolvedBy: null` even when the operator is signed in.
- Under `RYA_REQUIRE_APPROVER_IDENTITY=1` — documented bank mode — **every approve/reject 401s** and, per 4.2, the console blames the token. Approvals become unresolvable from the console.
- Per-user Postgres RLS never engages; every governance write is attributed to a workspace key rather than a person.

> **FIXED 2026-08-07** — Wired in the console **and** a server gap the audit did not see.
>
> **All three bullets confirmed**, and a fourth consequence found. `X-Rya-User-Token` has **three** consumers, not two: `get_plane:420` (per-user RLS), `_actor_from:393` (approval attribution) and `_identity_from` → the run's `Identity`, which `SDK context._authorize_connection` needs to resolve a per-user connection.
>
> **The RLS bullet understates it.** The policy is `owner IS NULL OR owner = current_setting('app.user_id', true)` (`tenancy.py:240`), and `store_postgres.py:278` sets that GUC to the **empty string** when no user token arrives. So the console did not merely fail to isolate — it could not SEE any row with a non-null owner. `save_run`, `create_session` and `create_connection` all set `owner` from the caller's `user_id`, so runs, conversations and connections belonging to an identified user were silently absent from the console's lists. Combined with §4.10 (emptiness reads as idleness), a user's runs rendered as *no runs*.
>
> **The server gap: `_identity_from` opened with `if mt or not jwt_configured(): return None`.** Every `identity=` argument in `app.py` comes from that function, so under multi-tenancy a run NEVER carried an `Identity` however the request was headed. The header was read by the other two consumers, which is exactly why this stayed hidden — two of three worked. The third means every `require_user` tool raised `E_NO_IDENTITY` in MT **for every client**, with a hint (`context.py:540`) telling the caller to forward a header the api was reading and discarding. Wiring the console alone would not have fixed it.
>
> **Server:** `_verified_user` is now the single place `X-Rya-User-Token` is verified, used by all three consumers. `_identity_from` gained an MT arm; an explicit user token wins in **either** mode, which preserves single-tenant's old precedence (a bearer JWT plus an explicit header used the header). A present-but-invalid token is a 401, never a silent `None` — collapsing "no identity offered" into "the identity does not verify" would let an expired token quietly downgrade a request to anonymous *and* silently change which rows it can see. Three routes gained the header in their signature: `post_event`, `create_turn_ep` and the SSE trigger.
>
> **Console:** a third credential, `rya_user_token`, minted by `mintUserToken()` from the session and sent by `api()` alongside the workspace key — never instead of it. Minted after signup and login, and on mount when a session outlived its 12-hour JWT (the returning-browser case, without which attribution would not survive a reload). `clearAuth` drops it, or a shared machine would send the previous operator's identity with the next one's key. The Queue's SSE reader sends it too, since that route decides RLS visibility. A failed mint **resolves rather than throws**: `/v1/token` calls `_require_mt()` and 400s on single-tenant, and an operator who pasted an API key has no session to exchange — neither may block the console.
>
> **Expiry:** the JWT lasts 12h and the session outlives it, so a 401 with a user token in play triggers ONE re-mint and retry. It deliberately does not try to prove the user token was at fault — an expired JWT and a bad workspace key are both `E_UNAUTHORIZED`, separable only by prose, and string-matching a human-facing message is not a contract. A wrong guess costs one request on an already-failing path.
>
> **Not fixed:** an operator who enters via the API-key tab has no session, so bank mode stays unresolvable for them. That needs a product decision about whether key-only entry is a supported way into a deployment that requires named approvers — `AuthModal` currently presents it as complete.
>
> **Also corrected here:** `Governance.tsx`'s "Per-user identity" tip claimed "verified JWT bound to every run". The flag behind it is just `jwt_configured()` — the mechanism being configured, not every run carrying an identity. Reworded. The dead *data sources* in that view remain §4.5.
>
> Tests: **+13**. Python +4 in `test_identity_bridge.py` — the run carries the identity in MT (response *and* durable record *and* `rya_runs.owner`), a token-less run stays anonymous and shared, and a bad or expired token is refused rather than downgraded. Console +9 in `api.test.ts` (52 total) covering the mint, the no-session and runtime-declines paths, the header riding alongside the key, its absence when there is none, the re-mint-and-retry, the single-retry bound, and `clearAuth`. Verified by reverting each side in turn: the MT test fails against the old `_identity_from`, and 3 console tests fail without the header.
>
> **Infrastructure note:** these tests are `@needs_pg` and had never run here. A local Postgres 18 now backs them, and **the full suite runs with 0 skips for the first time (951 passed)** — which is how the last stale §4.1 assertion was found (`test_identity_bridge.py:130` still asserted `["detail"]["code"]`, hidden behind the skip). CI installs the `postgres` extra but provisions no Postgres service, so it still skips 57 tests; giving §3.1's workflow a Postgres service is the single highest-value change left in the CI story.

### 4.4 Approvals: wrong list, and the operator approves blind
Two defects in the product's human-in-the-loop gate:

- **Cross-agent.** `snapshot.py:255` uses the unfiltered `store.list_approvals("pending")`. `_approvals_of` (`app.py:1778`) exists precisely to narrow by agent and is wired only to the agent-scoped route. *Nuance:* the server deliberately treats workspace-wide approvals as an inbox (`app.py:1801-1804` says so). The server isn't wrong — the **console** is, because it renders that workspace inbox under a page titled with one agent's name.
- **Blind.** `snapshot.py:316` ships `body` and the full `action`. `Approvals.tsx:52-63` renders **neither** — it cherry-picks `action.input.to` and hardcodes a `Mail` icon. A pending `payments.refund` with `{amount: 500000}` renders as a title, a tool name, and a mail icon.

There is **no `Approvals.test.tsx`** — the only view that POSTs an irreversible governance action is untested.

> **FIXED 2026-08-07** — Both halves, plus the missing test file. Both diagnoses were accurate as written.
>
> **Blind — the operator now sees the action.** `body` renders, and `action.input` renders as formatted JSON, capped at 4,000 characters with an explicit "truncated" marker rather than a silent cut. The audit's own example is now the headline test: a `payments.refund` of `500000` must appear on screen, because approving without it is a signature on an unread document. Two absences are also stated rather than left blank — an action with no arguments says *"This action takes no arguments."*, and an unserialisable payload says so, since "we could not show you this" and "there was nothing to show" are different facts and only one is a reason not to approve.
>
> **The hardcoded `Mail` icon is gone**, replaced by one neutral gate icon rather than a guess. A per-tool icon map was the tempting alternative and is worse: it would be a second thing to keep true, and the failure mode of the old code was not "uninformative", it was **wrong** — every approval in the product asserted it was an email.
>
> **Cross-agent — the list stays workspace-wide, and now says so.** Narrowing it to the selected agent was the obvious fix and would have been a mistake: an approval is the only irreversible human gate in the product, and hiding one because a different agent happens to be selected is how a run waits forever. Instead `build_console` attributes each row (`agent`), the header states the list is workspace-wide, foreign rows are marked amber, and a banner counts them — *"1 of these is for another agent. Approving one resumes **that** agent's run."* The audit's nuance was the right call: the server was not wrong, so the server's behaviour did not change.
>
> **Cost of the attribution:** `list_approvals` has no agent axis in storage, so it resolves through the run, as `_approvals_of` does. `runs` is already loaded for the selected agent, so the common case — every approval belongs to it — costs **zero** extra reads, and only a foreign approval pays for one.
>
> Tests: **+19**. New `Approvals.test.tsx` (17) — the arguments, the body, the tool name, both absence states, truncation, the unserialisable payload, foreign-agent marking and its pluralisation, the no-`agent` case, both resolve routes, id encoding (`apr/../evil` → `apr%2F..%2Fevil`), per-row rather than per-list disabling, and the bank-mode failure path re-enabling the buttons while surfacing the server's message. Python +2 in `test_console.py` pinning that the snapshot ships body/tool/input and that a second agent's approval is attributed to it. Verified by reverting each side: **7 of the 17** console tests fail against the old view, and the attribution test fails without the `agent` field.
>
> **Not changed:** `build_snapshot` (the CLI's `rya status --json`, `snapshot.py:121`) still ships only `{id, title, runId}` for pending approvals. That is a different surface with its own contract, and widening it was not this finding.

### 4.5 Governance reads two dead data sources
- **Kill switches.** `snapshot.py:154` reads `store.load_memory("_runtime_config")`. Verified: this is the **only remaining reader** in the tree outside legacy fallbacks, and **nothing writes it**. The live path is `policy_set(POLICY_KILLSWITCHES)` (`app.py:2090`). Disable a tool in Tools; the Governance kill-switch table and its History stay empty forever.
- **Egress.** `snapshot.py:127-129` reads `rya.guard.yaml` from disk, while the Guard editor writes to the store. Two views disagree about whether egress is enforced, and the auditable policy hash never moves when the allowlist changes. On a published bundle there is no file at all.

The repair surface already exists: `store.policy_get` / `policy_history` (`store.py:914,942`), and `GET /tools/log` (`app.py:2096`) already returns exactly the attributed history — it is simply never called.

> **FIXED 2026-08-07** — `snapshot._governance` now reads the sources that *enforce*. Both halves reproduced first, against a live app; the line numbers above have drifted (`snapshot.py:154` → `:177`, `app.py:2090` → `:2294`).
>
> **Kill switches.** Confirmed exactly as written: `PUT /tools/email.send/permission` returned 200, `GET /tools` reported `effectivePermission: "disabled"`, `GET /tools/log` had the attributed entry — and `/console` said `switches: {"active": [], "history": []}`. The screen an operator opens to confirm a kill is the one screen that said it had not landed.
>
> The reader was not copied, it was **shared**. `read_killswitches(store)` in `sdk/context.py` is now the single definition, and `ExecutionContext._killswitches` and `app._killswitches` both delegate to it. There were three copies; two agreed and the third — this one — did not. A reader that can drift from the enforcer is a reader that will.
>
> **Egress.** `guard.effective_policy(store, agent, guard_file=…)` is the read-only counterpart to `app._guard_source`, with the same precedence: store row first, project file second. `_guard_source` stays separate rather than calling it, because it answers the harder *write* question — which file a write may touch at all.
>
> The condition the audit did not state is why this survived: in single-agent `rya dev` the editor and this view both read the file, so the dev loop is honest and only a **deployed** one lies. Once the guard is store-backed, `PUT /guard` wrote 3 rules / `default: allow` while `/console` reported `5` / `deny`. On a bundle shipping no file, a live `default: deny` policy with rules and grounding on rendered as **"Egress guard: off · Grounding gate: off · 0 rules · not configured"** — the *inverse* error. Unpredictable direction is the real defect: neither reading could be believed either way.
>
> **`policy.hash` was the sharpest of these** and the audit undersold it. It is described in the source as "the version an auditor can pin a run to", and it moved for **neither** a store-backed allowlist change **nor** a kill switch — an auditable pin to a document nobody was enforcing. It now hashes the guard's **etag** (a content hash of the normalised live policy, whatever source it came from — so it is stable across reformatting and correct across sources) and **effective** tool permissions. Existing hashes therefore change value once; that is the fix, not a side effect.
>
> **A third defect, found while verifying and not in the audit:** the Policy tiles were manifest-only, so **"Denied: 0" rendered while a tool was killed** — the same lie as the empty table, one row up. Counts are now effective, with `toolsOverridden` beside them so a reader can tell a declaration from an operator's 03:00 override.
>
> **Two shape mismatches meant this was not a rename.** (a) The kill-switch table's `v` column read a per-override `version` **that has never existed** — `policy_set` versions the whole switches map. It rendered `vundefined` in production and only ever showed `v4` in tests because the fixture invented the field, the same pattern as §4.9's fabricated `handlers` (§9's "fixtures encode bugs"). The document version now sits in the section header and the column shows the override's `reason`, which is a question the row can answer. (b) History had to be **derived**: `policy_history` returns document snapshots, and the log never records *which* tool an operator touched, so `_switch_history` diffs each record's before/after into per-tool transitions. A record that changed another tool yields no row.
>
> **Taken while in there:** every policy record carries `actor`, `store.py:981` cites §12 risk 7 — *"who reviewed this allowlist change is a feature"* — and no screen showed it. The History table now has a **Who** column.
>
> **A source that cannot be read now says so** (`switches.error`, `policy.egressError`), because "no overrides" and "we could not ask" are the same picture and opposite facts — §4.4's lesson, restated one layer up. An unreadable guard additionally denies *all* traffic, so the tile's truthful "0 rules" is dangerously incomplete alone; it now carries the amber warning.
>
> **Narrows §4.6.** That finding's guard-file half is resolved wherever policy is store-backed, which is every deployed case: the key is `policy_key(agent)`, so agent B's `egressRules`, `egressDefault`, `groundingGate` and `policy.hash` are now B's. It survives only in the file fallback — a mounted dev tree with no store row. §4.6's manifest, `.env` and branding halves are untouched.
>
> Tests: **+20**. `test_console.py` +9, all driven through the *enforcement* path — set the switch through the real endpoint, read the guard through the real resolver — because a dashboard that agrees with a fixture but not the runtime is precisely this bug. `test_identity_bridge.py` +1 puts the same assertion on the **multi-tenant** path, which is the one that matters here: reader and writer only diverge once policy is store-backed, so the file store agreeing was never the interesting case, and `rya_policy_log` is a different table with a different projection. It is also the only path that produces an `actor` to check. `Governance.test.tsx` 7 → **17**, and its fabricated fixture corrected. Verified by reverting each side: **10 of 10** Python tests fail against the old `snapshot.py`, and **10 of 17** console tests fail against the old view.
>
> **Not changed:** the tiles' `pinnedArgTools` stays manifest-only — pins are not overridable by a kill switch, so there is nothing effective to compute.

### 4.6 `/console` attributes one project's files to whichever agent is selected
`app.py:775` passes a single process-level `root` into `build_console`, while `selected.manifest` is agent-scoped. So `_governance` (guard file), `.env` secrets, and branding all read from one fixed directory regardless of `?agent=`.

On a mounted project serving several agents, selecting agent B shows **agent A's manifest YAML**, **agent A's secret names**, and computes agent B's `policy.hash`, `egressRules`, `egressDefault` and `groundingGate` from **agent A's guard file**.

### 4.7 Team & access: the primary flow is dead
`Team.tsx:126` lazy-initialises `signedIn` from localStorage at mount. The only other calls are `setSignedIn(false)` at `:151` and `:166` — **`setSignedIn(true)` does not exist in the file.** An operator clicks Sign in, authenticates successfully, and the page still says "Sign in to manage the team". Members, invites, keys and password change are unreachable until a full browser reload.

Also in this view: **removals and revocations report success when the server refused them.** `remove_member` returns 200 with `{"removed": false}` and `revoke_key` returns `{"ok": false}`; the console reads neither and toasts success unconditionally. The operator believes access is revoked when it is not.

### 4.8 `/app` serves the whole `web/` tree unauthenticated
`app.py:650-653` mounts `root / "web"` at `/app` with no auth, no dotfile exclusion, no extension filter. In this repo `web/` **is** the console source. Confirmed live: `/app/console/src/lib/api.ts`, `/app/console/package.json`, `/app/console/vite.config.ts`, and `/app/console/node_modules/react/package.json` all return 200.

**Fix:** Require an explicit opt-in (`RYA_PRODUCT_UI_DIR`) rather than inferring from a directory name that collides with the console's own source.

### 4.9 Workers: the Handlers column can never render
`worker.py:202-209` `advertise()` returns a **dict** (`{event, jobs, crons, tools}`). `Deploy.tsx:148` types it `string[]` and calls `.length` — `{}.length` is `undefined`, falsy, so every worker row shows `—`. The one column answering "can this process serve the tool that is timing out" is permanently blank. `Deploy.test.tsx` feeds a fabricated `handlers: ['event']`, which is why it survived.

### 4.10 A crash loop reads as "scaled to zero"
`Deploy.tsx:411` queries `/workers?version_id=…` with **no `status=`**, and the server defaults to `status="alive"` (`app.py:2577`). A version whose only workers have crashed shows an empty table under "No process is serving this version — idle keys scale to zero." `WorkersView` gets this right at `:1160` with a 12-line comment explaining why; this call was written without it.

---

## 5. P2 — Medium severity

| # | Finding | Location |
|---|---|---|
| 5.1 | **Runs/filters/search operate on only the newest 30 runs** while the Overview tile counts all of them. No pagination, no "showing 30 of N". Searching a run id older than 30 returns "No runs match". | `Runs.tsx:54`, `snapshot.py:318` · ✅ **§5a** |
| 5.2 | **Conversations capped at 50** with no indication; open transcript never re-fetches; second row click shows the first row's messages with no spinner; no close button. | `Conversations.tsx:74,148,150` · ✅ **§5a** |
| 5.3 | **Detail state survives an agent switch.** Views are unkeyed in `App.tsx`, so trace/thread/turn/env/version/eval-result panels keep the previous agent's data under the new agent's header. In Guard this is worse: **Save writes agent A's draft into agent B's policy.** | `Runs.tsx:52`, `Conversations.tsx:75`, `Queue.tsx:522`, `Deploy.tsx:825`, `Evals.tsx:84`, `Guard.tsx:186` · ✅ **§5a** |
| 5.4 | **Queue tiles render "0" during load and during an outage** — "Dead-letter 0" while the endpoint is failing is the most dangerous wrong answer this view can give. | `Queue.tsx:546` · ✅ **§5a** |
| 5.5 | **Queue never refreshes** (loads once on entry) while the sidebar badge polls the same endpoint every 6s, visibly disagreeing a few pixels away. | `Queue.tsx:510` · ✅ **§5a** |
| 5.6 | **Deploy/Versions/Workers/Quotas discard `reload`** — the visible Refresh button changes nothing on these pages. | `Deploy.tsx:829,1028,1159`, `Quotas.tsx:312` · ✅ **§5b** |
| 5.7 | **The launch gate has four conditions; the console renders three.** `topology` (D32) is omitted and `unmet` is fetched and discarded, so the tile reads INCOMPLETE while all visible rows say "in force". | `Quotas.tsx:204-208`, `drivers.py:1477-1483` · ✅ **§5b** |
| 5.8 | **`usePoll` has no in-flight guard, no abort, no backoff, no visibility check.** `/console` opens a fresh psycopg connection per request in MT mode; a slow backend gets 10 concurrent requests/min/tab forever, and a dead runtime is hammered at full rate. | `usePoll.ts:76-81` · ✅ **§5b** |
| 5.9 | **A persistently failing poll is masked forever** once one poll succeeded — stale data with a 2.6s toast as the only notice, no age indicator, no retry. | `App.tsx:104-112,317-323` · ✅ **§5b** |
| 5.10 | **Sidebar counts render real zero and unknown identically** (both blank) — "0 workers" and "the fetch failed" look the same, which is exactly the outage-vs-idle confusion `app.py:2582` says must never happen. | `Sidebar.tsx:133-135` · ✅ **§5b** |
| 5.11 | **Sign-out leaves the previous tenant's data on screen** and its agent in storage. | `App.tsx:281-284` · ✅ **§5c** |
| 5.12 | **The auth gate opens even when the runtime requires no auth.** `/v1/info.authRequired` is returned and ignored; default `rya serve` blocks on a dialog demanding a credential the server doesn't want. No close button, `Escape` only. | `App.tsx:73`, `AuthModal.tsx:164` · ✅ **§5c** |
| 5.13 | **"Retained" multiplies by environment count** — `pinnedRuns` is agent-wide, summed across environments. 3 envs + 1 retained version reads "3". | `Deploy.tsx:909` · ✅ **§5c** |
| 5.14 | **The agent `<select>` is bound to the server echo, not the pending selection** — it snaps back for a full round trip, and stays wrong if that request fails. | `App.tsx:294` · ✅ **§5c** |
| 5.15 | **Kill switch is destructive, unconfirmed, and its audit reason is hardcoded** to `'console kill switch'`. Only two of four permission tiers are reachable — "put this behind approval" is impossible despite the legend explaining it. | `Tools.tsx:138-140,231` · ✅ **§5c** |
| 5.16 | **Re-running the same knowledge query does nothing** — `setSubmitted(same)` bails out of the update, so the deps never change. No request, no spinner. | `Knowledge.tsx:101` · ✅ **§5d** |
| 5.17 | **A connection with no status renders as "revoked"** (red), sending operators to chase a problem that doesn't exist. | `Connections.tsx:115-129` · ✅ **§5d** |
| 5.18 | **CORS omits DELETE.** Verified by preflight: `DELETE → 400`. Breaks exactly two operations cross-origin — revoking a member and deleting an API key. Invisible same-origin and in dev. | `app.py:624` · ✅ **§5d** |
| 5.19 | **`index.html` has no `Cache-Control`** while hashed assets get revalidated. Backwards: a CDN default TTL pins a stale index to a deleted asset hash → white screen after every deploy. | `app.py:709` · ✅ **§5d** |
| 5.20 | **CSP `connect-src` carries bare `ws: wss:`** — permitting a WebSocket to *any host*, a general exfiltration channel — for a console that opens no WebSocket at all. It buys nothing and weakens the directive containing 3.5. | `app.py:167` · ✅ **§5d** |
| 5.21 | **"Send test event" posts a hardcoded fake payload** to the live agent with no confirmation and no prod check — real spend, real tool calls, a fabricated customer email in the trace. | `App.tsx:264-279` |
| 5.22 | **Jobs & cron silently drops non-cron triggers.** Two shipped examples declare only a `manual` trigger and are told "No jobs or schedules." | `simple.tsx:147` |
| 5.23 | **Costs and tokens rendered as raw floats** (`0.523481`, `4321000`) directly beneath a tile showing `$0.5235`. `usd()`/`num()` are imported and used only in the tiles. | `Quotas.tsx:400,408` |
| 5.24 | **`/posture` missing from the dev proxy** — the panel silently vanishes under `npm run dev`, so regressions in it can't be caught pre-release. | `vite.config.ts:35` |
| 5.25 | **"At limit" counts org-scope violations no row on the page can explain.** | `Quotas.tsx:383` |
| 5.26 | **The Members table structurally cannot show the workspace owner** — a one-owner workspace reads "No members yet". | `Team.tsx:411`, `tenancy.py:448` |
| 5.27 | **The one-time API key has no copy button** — `CopyId` is imported and used for the non-secret key *id* three lines below. Mis-select and the key is unrecoverable. | `Team.tsx:484-496` |

---

### 5a. Remediation notes — §5.1 to §5.5 (2026-08-07)

The table above is left exactly as written. These five share one root cause, which is
worth stating before the individual fixes, because four of the five were symptoms of it:

> **`GET /console` is a dashboard AGGREGATE, and four views were using it as their
> database.** Its `runs` (30) and `sessions` (50) keys are previews, its counts are
> workspace-wide, and it returns a brand-new object every 6 seconds. Read as a
> dataset it produces exactly the failures listed: a filter that reports on a page
> (§5.1), a list with an unreachable tail (§5.2), and a view whose own detail state
> outlives the agent the aggregate was describing (§5.3). The fix is a boundary, not
> five patches: **the aggregate feeds overviews, badges and previews; a detail view
> owns the data it is detailed about.** `snapshot.py` now says so at both keys, and
> `web/console/AGENTS.md` records it as a convention so the next view inherits it.

**§5.1 — the runs table pages the server; the pills read the totals.**
`GET /agents/{id}/runs` grew `limit`/`offset`/`status`/`q`/`summary`, additively:
unparameterised it returns byte-for-byte what it always did (full run documents,
traces included), because `rya runs list` and the TypeScript SDK read it that way.
`count` is the size of the **filtered set**, not the page — the one number a client
cannot compute for itself, and its absence is why the old view had to guess. Filtering
lives in `store.run_matches`, one written definition that `FileStore` applies in Python
and `PostgresStore` pushes into SQL (with LIKE metacharacters escaped, so a search for
`run_9` is not a wildcard), so the rows and the counts beside them cannot disagree by
backend. `limit` is clamped 1..500 (`_page_limit`): these routes project every row, so
an unclamped `?limit=1000000&summary=1` would be a way to make one request materialise
a workspace. The view fetches `offset=0&limit=pages*50` and refetches the window whole
rather than accumulating pages — a newest-first list re-sorts under a merge, and no
merge key fixes a boundary that moved. It stays as live as the poll it replaced by
depending on a signature over `stats.runs` + `stats.byStatus`, so it re-reads when the
numbers move and stays quiet otherwise; a status change counts, not just a new run.
`filterRuns` and `runCounts` are **deleted** — counting a truncated array was the bug,
not an implementation of a good idea — and `pillCounts`/`runsSignature` replace them.
Two dead ends the paging design creates are closed rather than left: a `Retry` in the
error state (the shell's Refresh reloads `/console`, and if the numbers come back
unchanged nothing here would refetch), and the 500 ceiling detected from the **echoed**
`limit` rather than a client-side copy of the server's constant, so `Load more` never
becomes a live-looking button that does nothing.

**§5.2 — all four defects, and the cast that caused one of them.** Paging as above,
against `GET /agents/{id}/sessions` (already summaries server-side, so no projection
flag). The open transcript re-fetches when *that conversation's* row shows newer
activity (`lastMessageAt`/`messageCount`), which is precise and free while it is idle.
The second-row bug is fixed by **derivation, not sequencing**: the rendered transcript
is `thread.id === openId ? thread.detail : null`, compared against the id that was
*requested*, so a transcript belonging to a row nobody selected cannot reach the DOM at
all — no amount of careful `setThread` ordering can promise that. There is a close
button. And `type StateWithSessions = ConsoleState & { sessions?: … }` is gone: that
cast existed only to read an undeclared field off the aggregate, which is the root
cause wearing a type annotation.

**§5.3 — one key, in one place.** The view subtree in `App.tsx` is keyed on
`loaded.agent.name`, so a switch remounts it and every open trace, thread, turn,
environment, version, eval-result and Guard draft goes with it. Keyed on the server
**echo** rather than the pending selection, deliberately: the selection changes on click
while the fetch is in flight, so keying on it would blank the panels a beat early and
remount once more when the shell adopts a sole agent's name on first load. The echo
changes exactly when the content does. `App.agentSwitch.test.tsx` pins both halves —
that it resets on a switch, and that it does **not** reset on a poll, because the shell
hands every view a new `state` object every 6s and a key that noticed would delete a
panel out from under an operator every six seconds. Verified as a negative control: with
the key removed, agent A's open panel survives the switch to agent B and the test fails.
Views therefore need no per-view reset effects, which is the version of this fix that
would have been reintroduced by the next view to forget one.

**§5.4 — an unknown count is not a zero.** `num(undefined)` is `'0'`, so a count
threaded from an unsettled or failed fetch rendered as a drained queue. The shell's
queue state is now `QueueCounts | null` and is never coerced to `{}`; the tiles render
`—` with `reading…` / `not available`, and a failed refresh with real numbers on screen
keeps them and says they are stale rather than silently freezing. Amber never fires on a
guess. A real zero still reads `0` — the fix must not trade a false zero for a false
unknown, and there is a test for that direction too. The sidebar count is omitted rather
than sent as `0` for the same reason, which is the shell's half of §5.10.

**§5.5 — one poll, two readers.** `/queue/stats` is polled once, in `App.tsx`, and
handed to the Queue view, because the sidebar badge was already polling it every 6s on
every page: the view's own on-entry fetch was not saving that request, it was making a
second one and then going stale beside it. The jobs *table* polls itself every 6s, which
is **fewer** requests on entry than before. This reverses a decision the file documented
("re-fetching every six seconds would fight the open stream"), so the revision is
documented in its place: the tiles now cost no request at all, one small GET is not
contention for an SSE reader, and a queue table that freezes while you inspect a turn is
how a job reaching the dead-letter queue gets missed. A failed tick no longer blanks
rows an operator is reading.

**Tests.** Console **233 → 265** (21 files): `Runs.test.tsx` 7 → 17 against a fake server
that *implements* the contract over a 412-run corpus whose failures are all older than
page 1, `Conversations.test.tsx` 7 → 18 with a decoy `sessions` key on the aggregate so
"reads its own fetch" is asserted rather than assumed, `Queue.test.tsx` +9, and a new
`App.agentSwitch.test.tsx`. Python **+10** in `tests/test_api.py` — the paged contract,
including the two audit symptoms as executable tests (`test_a_search_reaches_past_the_console_preview`,
`test_conversation_fifty_one_is_reachable`) — plus `list_runs_page`/`list_sessions_page`
added to the broker's **withheld** methods, since a window onto an enumeration is still an
enumeration. Every new assertion was verified to fail against the unfixed code.

**The contract is written down.** `docs/devex.md` gained a **One paged-listing shape**
section beside its error-envelope one, for the reason §4.1 established: the envelope
diverged because it had never been specified, and a `limit`/`offset`/`count` convention
on three routes with nothing written down is the same setup.

**Deliberately not done here:** the aggregate still ships its `runs` and `sessions`
previews, now with no console consumer (Overview reads `stats`). Shrinking or dropping
them is a `/console` payload question, not a §5 fix, and `tests/test_sessions.py` asserts
the key. Flagged rather than folded in. The TypeScript SDK's `listRuns()` also does not
expose the new parameters — §10's point that `clients/typescript/` re-implements these
contracts stands.

---

### 5b. Remediation notes — §5.6 to §5.10 (2026-08-07)

Four of these five live in the two hooks every view fetches through, and they share a
theme worth stating first, because it is the same one §5a found one layer up:

> **A control or an indicator that describes "the console" was quietly wired to one
> endpoint.** Refresh meant *refetch `/console`* (§5.6). `live` meant *the last
> `/console` attempt succeeded* (§5.9). Both are true of the aggregate and were
> presented as true of the page. The fix in each case is to make the thing global for
> real — one refresh signal every loader subscribes to, one staleness state that carries
> an age — rather than to widen the wiring by hand, which is the version that stays
> correct only until the next view is added.

**§5.6 — one refresh, broadcast.** `lib/refresh.ts` holds a counter in context and
`usePoll`/`useLoad` subscribe to it themselves, so every loader in the mounted tree
honours the Refresh button **by construction** and a new view inherits the behaviour by
using the hooks it was already using. Nothing in `views/` mentions the file. The
alternative — lifting `reload` out of nine views as a prop — was rejected as the shape
of the bug rather than a fix for it: the finding is not that four views were wired
wrong, it is that wiring was per-view at all, and the tenth view forgets. Deploy,
Versions, Workers, Quotas, Evals, Guard, Knowledge and (since §5.1/§5.2) Runs and
Conversations all changed behaviour with zero lines changed in any of them. Two details
that are not incidental: the shell **publishes** the signal, so it passes its own poll
`opts.tick` rather than also calling `refresh()` — two code paths would have meant two
`/console` requests per press of a button whose neighbouring finding is about request
volume — and the signal is bumped for exactly three things (the button, a successful
sign-in, Send test event), never on a timer, because a signal that fired every six
seconds would turn every `useLoad` on the page into a second poller, which is the
mistake §5.5 had just finished undoing. `useLoad` gained a sequence guard and an abort
in the same change: a reload used to happen only on an agent or id change, so two
overlapping loads were nearly unreachable, and a button an operator can press twice
makes them ordinary.

**§5.8 — the loop schedules itself.** Not a stack of guards bolted onto a
`setInterval`; the interval is gone. The next tick is scheduled only once the previous
one has SETTLED, which makes overlap structurally impossible rather than merely
defended against — the old code gave a struggling multi-tenant database ten fresh
psycopg connections a minute per open tab, forever, and discarded every earlier answer
on arrival. On top of that: exponential backoff to a 60s ceiling, no request at all
while `document.hidden` (with an immediate fetch on `visibilitychange`, so the saving
costs nothing in freshness), and an `AbortSignal` on every attempt so a superseded or
unmounted request is *cancelled* rather than ignored, which is what actually releases
the connection at the far end. The fetcher signature became
`(signal?: AbortSignal) => Promise<T>`, which required **no** call-site changes because
a shorter parameter list is assignable. Two decisions came out of writing the tests
rather than the code: a scheduled tick **skips** when a request is in flight while an
operator's Refresh **supersedes** it (dropping the click would make the button look
broken in exactly the slow-backend case it was pressed for), and the failure count and
the backoff counter are **separate numbers** — sharing one meant a failed Retry reset
the count to 1 and dismissed the staleness banner the operator had just pressed Retry
on, the console answering "it's fine now" to a click that had proved the opposite.

**§5.9 — stale data carries its age.** Keeping the last good value through a failure is
right and stays; the omission was that `live` is a boolean, so data from four seconds
ago and data from forty minutes ago were the same state under the same 6px dot, and the
only notice was a 2.6-second toast on the leading edge. An operator who looked away at
the wrong moment — or who opened the tab after the runtime died — saw a complete
dashboard with every number frozen. `usePoll` now exposes `lastSuccessAt`, the pill
reads "offline · 4m old", and after **two** consecutive failures `StaleBanner` states
the age in words, in the runtime's own words, with a Retry. Two rather than one
deliberately: a banner on every blip is one operators learn to scroll past, and the pill
goes offline on the first failure regardless, so nothing is hidden meanwhile. The banner
sits above the view and **outside** the error boundary, because staleness belongs to the
shell's poll and is exactly as true of a view that has crashed. One `useNow` clock feeds
both readouts, so they cannot quote different ages for the same instant.

**§5.10 — three states, three renderings.** `{c.value || ''}` drew a real zero and a
count whose request failed as the same blank, which is the outage-versus-idle confusion
`app.py`'s workers route has a comment forbidding, reproduced one panel to the left —
and the worse half of the pair, because scale-to-zero is the *designed* state for an
idle key, so an operator has every reason to read the blank as "fine" and stop looking.
A number renders as a number, a failed read renders `—` with a tooltip, and a count
nobody has attempted yet renders no badge at all. The third state is what stops a
healthy console flashing `— — —` across the nav on first paint, and it is why the
shell's deploy counts became `DeployCounts | null` rather than `{}`: an empty object
cannot say "not attempted", so three pending requests and three failed ones looked
identical. Amber never applies to `—`; highlighting a number we do not have is the same
lie in a louder colour, and there is a test for that direction.

**§5.7 — the console renders the gate; it no longer restates it.** The launch gate has
four conditions and `Quotas.tsx` kept its own three-entry copy of the set, so when D32
(broker topology) was added to `PostureReport` nothing failed: the table went on showing
three rows that all read "in force" beneath a tile reading INCOMPLETE, because `ok` and
`unmet` are computed server-side and *did* count the fourth. `PostureReport.conditions`
is now the single ordered definition that `unmet`, `ok`, `describe()`, `rya posture` and
the console all read, so the summary and the rows are three renderings of one evaluation
rather than two lists kept equal by hand. It is additive on the wire: the flat
`isolation`/`broker`/`egress`/`topology` keys are untouched and `unmet`'s prose is
unchanged byte for byte, because that string is quoted verbatim into
`E_ISOLATION_INSUFFICIENT` — which is why each condition carries both a title-cased
`label` for a heading and a lowercase `prose` fragment for the refusal, and why a test
pins the second spelling. The `CONDITIONS` literal is deleted and the rows come from
`posture.conditions`, so a condition added after a bundle ships still renders under the
server's own label; the test that proves the drift class is closed sends a **fifth**
condition the console has never heard of and asserts it appears. `rya posture` had the
identical defect — a hardcoded list of three, written before D32 — and is fixed the same
way, by iterating the gate rather than listing it. The other half of the finding was
that `unmet` was fetched and thrown away, so nothing said *why* the tile read INCOMPLETE:
it is now rendered in the platform's own words, and only when `untrusted && !ok`, because
an unmet condition on a trusted deployment is the designed state and listing them there
would make every self-host look broken — the same reason the badge already follows
`untrusted` rather than `ok`.

**Tests.** Console **283 → 315** (26 files): a new `lib/usePoll.test.tsx` (15) driving
the hooks directly, because the behaviour under test is timing and cancellation and
asserting it through a view would pin it to whichever view was chosen as the vehicle; a
new `components/Sidebar.test.tsx` (6); and two new shell files, `App.refresh.test.tsx`
(3) and `App.stale.test.tsx` (6), which exercise the real shell through a real view —
a green hook and an unwired provider is precisely the shape of §5.6. `Quotas.test.tsx`
10 → 13. Python **+4** (`tests/test_sandbox.py`, `tests/test_api.py`). Every new
assertion was verified as a negative control against the unfixed code: reverting
`Sidebar.tsx` fails 4 of 6, reverting `usePoll.ts` fails 13 of 15, reverting `App.tsx`
fails 6 of 9, reverting `Quotas.tsx` fails 3 of 13 and `drivers.py` fails 4 Python
tests. The assertions that pass either way are the regression guards, and are meant to:
"a blip raises no banner", "nothing re-reads until Refresh is pressed", "no badge for a
count nobody attempted", "a trusted deployment stays calm".

**Deliberately not done here:** a failed Refresh on a `useLoad` view still swaps a good
table for that view's error state, because `useLoad` reports the error and the nine
views decide what to render. It is defensible — the operator asked for fresh data and it
failed — but it is now reachable by a button where before it took an agent switch, and
making those nine degrade the way `usePoll` does is a per-view change, not a hook one.
`useNow` ticks once a second while offline; that is a re-render of the shell, not of the
views, and it stops the moment a poll succeeds.

---

### 5c. Remediation notes — §5.11 to §5.15 (2026-08-07)

Two clusters, not one theme. Three of these five are the shell's account of *itself* —
who is signed in, whether that was ever required, and which agent is selected — and they
share a cause:

> **The console answered a question about the runtime by looking at itself.** "Does this
> need a credential?" was answered by reading localStorage (§5.12). "Which agent is
> selected?" was answered by reading the server's echo (§5.14). "What does signing out
> clear?" was answered by listing the keys someone remembered to list (§5.11). Each time
> the authoritative source existed and was a few characters away.

The other two are ordinary defects in one view each, and both are about **scope**: an
agent-wide number added up per environment (§5.13), and an operator's intent replaced by
a constant (§5.15).

**§5.11 — a session is a component lifetime.** `signOut()` was `clearAuth()` plus
`openAuth()`: it removed four credentials and nothing the credentials had fetched. The
previous tenant's runs, secrets, traces and queue depths stayed mounted behind the
dialog — one Escape from being read, and the dialog now has a visible Close as well
(§5.12) — while `rya_agent` survived to be sent as the *next* tenant's first request.
`App` is now a five-line session owner rendering `<Console key={epoch}/>`; signing out
bumps the epoch and React discards the entire subtree, which is every piece of shell
state plus every view's local detail state, **including caches nobody has written yet**.
Clearing them by hand was rejected for the reason the bug exists: it is a list, and the
finding is that someone forgot an item on it. Storage is cleared *before* the remount,
because the new instance reads `getToken()`/`readAgent()` during its first render — one
of the two ways the new code can plausibly go wrong, and pinned by a test.

**§5.12 — the runtime decides.** `useState(!getToken())` is the whole bug in one
expression: it answers "does this runtime want a credential?" with "does this browser
have one?". Those come apart on the most common way to run Rya — a plain `rya serve`,
where `auth_enabled()` is false and every request would have been answered — and the
first thing that deployment showed a new operator was a full-page modal demanding a
token the server neither wants nor validates, exitable only by a keystroke nothing
mentioned. `/v1/info.authRequired` has always answered it; the modal even fetched
`/v1/info` for its tab set and dropped that field. `runtimeInfo()` now caches the probe
once per page load and both the gate and the modal read it, so they cannot disagree and
the boot costs one RTT rather than two. **It resolves rather than rejects, and only an
explicit `false` opens the door**: an unreachable discovery endpoint is not evidence that
a door is open, so `{}` degrades to exactly the old behaviour. A browser holding a token
skips the probe entirely, so the common path costs nothing. Nothing fetches before the
gate has decided (`canFetch`), which also removes the doomed `/console` a gated console
used to fire on every load. The dialog gained a visible Close and honest copy for the
`authRequired: false` case, since the workspace button can now open it on a runtime that
wants nothing.

**§5.14 — the control shows the choice, and names the gap.** The `<select>` was bound to
`loaded.agent.name`, so choosing an agent moved the control and the next render put it
back: the operator watched their own click undone, for a full round trip. If that request
failed it stayed undone, while the selection was *real* — in localStorage, and prefixed
by `ag()` onto every agent-scoped request in the console. Binding it to the choice fixes
the snap-back and creates a second way to lie, because the control then names an agent
whose data is not on the page, so `Sidebar` takes **both** `selected` and `showing` and
states the disagreement for as long as it lasts: `loading billing-agent…` while the
runtime is healthy, amber `still showing support-agent` once the read has failed. Note
this is the *opposite* of §5.3's view key, which wants the echo precisely so a panel is
not blanked a beat early — a key wants the echo, a control wants the click, and having
both names available is what lets each have the one it needs.

**§5.13 — one agent-scoped set, derived once.** `pinnedRuns` looks like an environment
fact and is not one: `describe_environment` walks `store.version_list(agent=agent)` and
`pinned_runs` scans `store.list_runs()` with no environment filter, so every environment
answers with the same agent-wide census of versions holding a live run, less only its own
current pointer. Summing `Object.keys(...).length` across rows counted that census once
per environment — three environments draining one old version reported "3", and the
number grew when someone added a staging pointer, which changes nothing about what can
be retired. `retainedVersionsAcrossAgent()` derives the set once, under a name that says
the scope out loud, as the **union of every row's keys minus every live pointer**. The
subtraction is forced, not cosmetic: prod's map omits prod's current version but dev's
map still lists it, so a plain union readmits a version that is promoted right now and
files it under the tile's own label, "older versions still pinned". **The per-row
"Retention" column was removed** — it printed the identical agent-wide number in every
row, and a column is read as a property of its row, which no header wording survives.
The count lives in the tile that already summarises the table, and the specifics stay one
click away in the environment panel's "Retained versions" table, which lists each held
version by id, bundle, state and run count.

**§5.15 — confirmed, four tiers, the operator's reason.** One unguarded click on a table
a 6s poll keeps repainting wrote **privileged, versioned, append-only** policy state, and
the `reason` was the constant `'console kill switch'`. That is worth being precise about:
the server stores it verbatim beside a real actor and a real timestamp, and
`GET /tools/log` exists to answer "who changed which kill switch, when, and what it was
before" — so a hardcoded string does not add nothing to that record, it fills the only
field capable of answering *why* with a description of the button that was pressed,
permanently, under someone's name. Six months later the log reads identically for the
tool killed during an outage and the one killed by a mis-click. Both switches now open
`components/ConfirmDialog.tsx` (a shared primitive, since "Send test event" (§5.21),
revoking a member and deleting an API key are all one click too), which owns
`AuthModal`'s a11y contract exactly — `role="dialog" aria-modal="true"`, own-heading
label, focus moved inside, Tab trapped, Escape closes — and **neither Escape nor Cancel
performs the action**. Three deliberate deltas from `AuthModal`, all commented: the Tab
ring filters `[disabled]` (the confirm button starts disabled, so treating it as the last
stop leaks the trap), focus lands on the first field else Cancel and **never** on the
destructive control, and Cancel stays live while the request is in flight so a hung write
cannot trap the operator. The tier picker offers all four tiers off the same constant the
legend renders — `approval_required`, the tier that suspends a run rather than failing it
and the one an operator actually wants mid-incident, was documented at length and
unreachable. The reason is required and trimmed; `Restore` is confirmed too, because
re-enabling a tool somebody deliberately killed is not a neutral undo, but asks for no
reason because the server drops the record rather than annotating it. A failed write
keeps the dialog and the typed reason, since discarding a sentence the operator just
composed teaches them to type `.` next time. Row labels are now `Disable…` / `Restore…`
— the ellipsis is the conventional signal that a control opens a dialog.

**Tests: 315 → 358** in isolation, **368 across 30 files** once merged with the §4 work in
flight (was 26 files). New files: `App.gate.test.tsx` (8),
`App.session.test.tsx` (6), `App.agentSelect.test.tsx` (6),
`components/ConfirmDialog.test.tsx` (13). Extended: `views/Tools.test.tsx` 12 → 19,
`views/Deploy.test.tsx` 14 → 17. Every new assertion was verified as a **negative
control** against the unfixed code, file by file, because these five findings live in
five different files and a single revert would not have isolated them:

| Reverted | New tests failing |
|---|---|
| `App.tsx` | 10 of 20 (§5.11 + §5.12 gate + §5.14 snap-back) |
| `components/AuthModal.tsx` | 4 of 14 (the shared probe, the Close button, the copy, and the §5.11 dismiss walk-up) |
| `components/Sidebar.tsx` | 2 of 6 (the mismatch readout only — App passing the choice is what fixes the snap-back) |
| `views/Tools.tsx` | 10 of 19 |
| `views/Deploy.tsx` | 3 of 4 new |

The assertions that pass either way are regression guards on the **fix**, and each says
so in place: "an unreachable `/v1/info` still opens the dialog" (fails if `{}` is ever
defaulted to open), "a token in hand costs no probe" (fails if the probe is made
unconditional), "sign-out still clears every credential" (fails if the remount is
reordered before the storage writes), "a fully drained agent reads 0" (fails if live
pointers are added to the retained set instead of subtracted), and "no mismatch warning
on a first load" (fails if the choice/echo comparison forgets that they differ on every
boot).

`tsc --noEmit` clean, `vite build` 0 warnings, **356.5 kB / 106.1 kB gzip** merged, 0
`act(...)` warnings, `smoke_console.sh` 11/11 on the rebuilt bundle. **No `src/rya/` file
was touched by this batch** — §5.13's fix is client-side arithmetic against a payload the
server was already right about — and the Python suite is unmoved by it (926 passed / 62
skipped in the merged tree, all of the delta from the §4 work). Verified over the wire that
§5.12's premise holds both ways: a default `rya serve` answers `authRequired: false`, and the
same binary with `RYA_TOKEN` set answers `true`.

**Deliberately not done here:**

- **The per-environment retention number is gone, not replaced.** Nothing in the payload
  is environment-scoped, so there is nothing honest to put in that column. A
  `pinnedRuns` filtered by the runs' own environment would be a server change
  (`deployments.pinned_runs` takes no environment) and is a better fix than anything
  available client-side — worth its own finding.
- **Closing the auth dialog on a runtime that *does* require a credential still loops**:
  the poll fires, 401s, and `onUnauthorized` reopens it. That is the pre-existing
  behaviour of Escape and is arguably correct (you cannot get in), but it makes the new
  Close button useless in that one case. Left alone because redesigning the 401 path is
  not what §5.12 asked for.
- **Only the tool kill switch is confirmed.** `ConfirmDialog` exists and is general, but
  wiring it into "Send test event" is §5.21's fix and is deliberately not smuggled in
  here.

---

### 5d. Remediation notes — §5.16 to §5.20 (2026-08-07)

Two clusters again. The first two are client-side and share a cause that is more specific
than "a rendering bug":

> **The wrong data type was answering the question, and TypeScript could not object to
> either one.** A React state value *deduplicates* — which is correct for a value and
> fatal for an event, so a query box whose submitted text was also its "go" signal stopped
> working on the second press (§5.16). A two-arm ternary is *total* over `boolean` and not
> over `string | null | undefined`, so a status column with two arms had to put a missing
> value somewhere, and put it under a red accusation (§5.17). Both compile. Both are the
> shape of the state being wrong rather than the logic over it.

The other three are the serving path, and the audit filed them as "the one-liners". They
are one-liners, and that undersold why they were all still here:

> **Each header list describes a console that does not exist, and every one of them is
> invisible in the only deployment anyone develops against.** Same-origin `rya serve` and
> Vite's proxy both hide a missing CORS verb. No CDN in the repo hides a missing cache
> policy. A channel the console never opens hides an over-broad `connect-src`. Three
> defects that a correct-looking local run cannot exhibit, which is why the fix that
> matters most in this batch is not any of the three edits — it is the four checks added
> to `smoke_console.sh`, which asks a live process what came back.

**§5.16 — the query is a value, the submit is an event.** `submitted` was doing both jobs.
`setSubmitted(query.trim())` with the same text is a no-op, so `useLoad`'s deps never moved
and the second press produced **no request, no spinner and nothing on screen at all** — in
the two situations an operator does exactly that: retrying a failed search, and re-running a
query after ingesting a document. A monotonic `searchTick` now carries "it happened again",
beside `submitted`, which carries what the request is made of; that is the shape
`lib/refresh.ts` already uses and `useLoad` already folds into the same dependency list.
Deliberately **one** path — `reload()` as an escape hatch for the repeat case would have
been a second way to start a search for the next change to forget about.

The same cause had a **second instance in the same file, which the finding does not
describe**, and it fails in both directions. The toast effect was keyed on
`[error, submitted]`. A retry that failed again with the identical message on the identical
query changed neither dep, so **the second failure was swallowed in silence** — the console
reading as "the button did nothing", which is §5.16 arriving from the other end. And
`submitted` changes at *submit* time while `useLoad` clears `error` only on *success*
(`reload()` opens with `setLoading(true)` and leaves the standing failure alone until the
next attempt settles), so **submitting a new query after a failure re-announced the old
failure instantly**, for a request that had not been made yet. The only honest trigger is
the settle, so the effect is keyed on `loading` returning to false and guarded in-flight.
The naive fix — adding `searchTick` to the old deps — passes the repeated-failure test and
is caught by the stale-toast one; both are in the suite for that reason.

**§5.17 — an unknown word is quoted, not translated.** `c.status === 'active' ? ok :
'revoked'` made two claims the runtime never made. An absent status fell into the else arm,
so a connection whose secret is still set and which nobody has revoked was reported as
revoked, in red — sending an operator to re-issue a credential that is fine. And *any*
value other than the literal `'active'` was relabelled `revoked`, so the console
simultaneously invented a word and discarded the one it was sent. What makes this a
particularly clear miss is that **the console already had the right idiom and this column
implemented its inverse**: `statusClass` returns `''` for a word it has not been taught and
`StatusBadge` prints the word it was handed, so an open vocabulary degrades to
neutral-and-verbatim. Three states now, reading as a sibling of the Secret column three
lines above it: a word we understand gets its tone from a local `STATUS_TONE`; a word we do
not is printed verbatim and left neutral; an absent one becomes amber `unknown`. Amber
rather than neutral is load-bearing — `get_connection` resolves `WHERE status = 'active'` in
both stores, so a statusless connection **will not be injected into a tool call**, and a
grey badge would understate that. Both unknown states carry a `title` naming that
consequence, because an unknown here says *why* (§5.4, §5.10); the two we understand
deliberately do not, and a test keeps that noise from creeping back. On reachability the
comments are honest rather than dramatic: both stores do write `status`, so this is a
defensive rendering, warranted because `_public_connection` passes through whatever it
finds, the broker proxies `list_connections` for duck-typed third-party stores, and the
field is modelled optional here for the same reason every field past `{id, provider,
scopes}` is.

**§5.18 — the verb is the symptom; the drift is the finding.** `allow_methods` was
`["GET", "POST", "PUT"]` against a router with two `@api.delete` routes, so revoking an API
key and removing a member both died at the **preflight** — a 400 before the request was
ever made — for every cross-origin caller. Adding `DELETE` fixes today. What fixes the
class is that the list is hand-written and derived from nothing, so it stopped matching the
router the moment the first `@api.delete` landed and would drift out again just as quietly.
Deriving it from `api.routes` was considered and rejected on two grounds: at that call site
most routes are not registered yet, and moving the registration would reorder the
middleware, because `add_middleware` prepends and the last one registered is outermost.
Verified rather than assumed — `app.user_middleware` is `[BaseHTTPMiddleware,
CORSMiddleware]`, outermost first — so CORS currently sits *inside* `_security_headers`,
and moving it would change an unrelated answer (see the new finding below). So the list
stays explicit and reviewable, and `test_cors_allowlist_covers_every_method_the_router_
exposes` walks the finished router and preflights every verb it finds, on the wire. `PATCH`
is deliberately still refused: no route serves it, and this is an allow-list.

**§5.19 — immutable assets, a revalidated index.** One shared `_CONSOLE_HEADERS` dict served
the index and the asset mount, and *that* was the root cause: one object cannot express
opposite policies, which is how it managed to be wrong in both directions at once. Measured
before the fix: `/` carried **no `Cache-Control`, no `ETag` and no `Last-Modified`**, so an
intermediary was free to invent a heuristic TTL and pin the one file that names the current
asset hashes — after a deploy the pinned index asks for hashes that no longer exist, the
bundle 404s, and the operator gets a blank page with nothing in any log. Meanwhile
`/assets/*`, whose names are content hashes and therefore cannot go stale, carried a
validator and no `Cache-Control`, so every page load spent a conditional request; the `304`
was confirmed by replaying both `If-Modified-Since` and `If-None-Match`. The dict is now
three that share every security header and differ only in cache policy: `immutable` for a
year on the assets, `no-cache` on the index, `no-store` on the 503 "bundle not built"
explainer — a cached copy of which outliving the build that fixes it is the pinned index
with the sign flipped, a correct deployment reporting itself broken. `immutable` is honest
only because `/assets` is mounted on exactly Vite's hashed output; unhashed files land in
`dist/` root, which this routing does not serve, and the comment says so.

**§5.20 — `connect-src 'self'`.** Bare `ws:`/`wss:` are *scheme* sources, not host sources:
they permit a socket to any host on the internet, which is a general exfiltration channel,
sitting in the one directive whose job is containing §3.5. Nothing wanted them. Verified:
the console contains no `WebSocket` and no `EventSource` — the Queue's turn-stream inspector
is a plain `fetch` + `getReader()` over SSE, an ordinary `connect-src 'self'` request. The
app's real `@api.websocket("/ws")` route has no console client, and `_CONSOLE_HEADERS` is
stamped only on `/` and `/assets/*`, so the project UI at `/app` is untouched either way.
If a console socket is ever written it still needs no change here: CSP3 matches a
same-origin `ws://` against `'self'`.

**Found while fixing §5.18, not fixed:** a cross-origin **preflight to `/mcp` on a
token-protected runtime is answered `401` with no CORS headers on it at all**, because
`_security_headers` is outermost and its MCP token guard short-circuits ahead of CORS. A
preflight carries no credentials by design, so an allow-listed browser origin can never
reach `/mcp` at all. Small, real, and adjacent — it is exactly what moving the CORS
registration would change, which is why that refactor was kept out of a missing-verb fix.
Worth its own finding.

**Tests: 368 → 377 console** (30 files, unchanged) and **926 → 931 Python**. Extended:
`views/Knowledge.test.tsx` 13 → 18, `views/Connections.test.tsx` 12 → 16,
`tests/test_console.py` +5. `scripts/smoke_console.sh` **11 → 15 checks**. Negative
controls were run per finding, because these five live in five files and one revert would
not have isolated them:

| Reverted (tests kept) | Failing |
|---|---|
| §5.16 (a) — `searchTick` out of the deps | 4 of 18 (the three request-count tests, plus the repeated-failure toast, which has nothing to fail twice) |
| §5.16 (b) — toast deps back to `[error, submitted]` | 2 of 18 — the swallowed repeat *and* the stale toast, which is the half the finding does not describe |
| §5.16 (b) — the *naive* fix, `searchTick` added to the old deps | 1 of 18: the repeat passes, the stale toast catches it. The trap is real and pinned |
| §5.17 — the two-arm ternary restored | 3 of 16 (absent, explicit `null`, unrecognised) |
| §5.18 — `allow_methods` back to GET/POST/PUT | 2 Python: the preflight test, and the drift test with `the router serves ['DELETE'] but CORS rejects the preflight for them` |
| §5.19 — one shared header dict restored | 2 Python, plus **3 of 15** smoke checks (the index and both assets) |
| §5.20 — `ws: wss:` restored | 1 Python, plus 1 of 15 smoke checks |

Assertions that pass either way say so in place: `active → stbadge ok` and
`revoked → stbadge fail` (guards the local status map against losing an entry, not a
demonstration), "a status it understands carries no tooltip" (guards against the
explanatory `title` spreading into noise), and "PATCH stays 400" (guards a future
speculative widening of the allow-list).

`tsc --noEmit` clean, **0 `act(...)` warnings**, `vite build` 0 warnings at **356.8 kB /
106.2 kB gzip**, `smoke_console.sh` **15/15** against a live server on the rebuilt bundle.
All three server fixes were re-confirmed on the wire after merging: the DELETE preflight
answers `200` with `access-control-allow-methods: GET, POST, PUT, DELETE` while PATCH still
answers `400`; `/` carries `no-cache` and the assets `public, max-age=31536000, immutable`,
including on the `304`; `connect-src` is exactly `'self'`.

**Deliberately not done here:**

- **The index still has no validator.** `no-cache` means a revalidation is a full re-fetch
  of about a kilobyte rather than a `304`, because `HTMLResponse` sets no `ETag`. An ETag
  over `html` plus `If-None-Match` handling would make the policy pay for itself, and the
  comment records that `no-cache` (unlike `no-store`) is the choice that lets it be added
  later with no walk-back. Left out because a conditional-request path is new behaviour on
  a request that costs a kilobyte, and the finding was that the response had no policy at
  all.
- **The `/mcp` preflight 401 above.** Diagnosed, verified, not touched.
- **No portable `<meta>` CSP** for an nginx/S3/CloudFront deployment of `dist/` — still the
  open item under §3.5, still blocked on a decision about the Vite dev exemption. §5.20
  tightens the directive wherever the CSP *is* applied and does nothing for the case where
  it is absent entirely.
- **Connections is still read-only.** There is no revoke path in the console and no server
  route for one (§7), so the view can now report a broken credential accurately and still
  offers nothing to do about it.

---

## 6. Stub / dummy inventory

These are the places rendering constants dressed as measured data. This is the direct answer to "any stud implementation or dummy":

| What | Where | Reality |
|---|---|---|
| `agent.status` = `"running"` | `TopBar.tsx:52`, `snapshot.py:279` | Hardcoded server-side. Green pill regardless of workers or execution history — next to a genuinely-computed live/offline dot. |
| Secrets "Source" = `.env / Secrets Manager` | `simple.tsx:91` | Literal. Server derives secrets by splitting `.env` lines and sends no source. |
| Memory "Scope" = `per-agent` | `simple.tsx:130` | Literal, and **false** — it's one workspace-level scope shared by every agent. A false isolation claim. |
| Memory "Recall" | `simple.tsx:133` | `c.name === 'facts' ? 'semantic' : '—'` — a hardcoded name check. |
| Infrastructure "secrets" row | `Infrastructure.tsx:161` | Client-side literal. `build_infra` has no `secrets` field — nothing was checked. Sits between two rows that *are* real. |
| Control/data plane, edge endpoints | `Infrastructure.tsx:189-206`, `app.py:104-107` | Server-side string literals. Claims "in-process worker" on deployments with a separate execution plane — the normal case since D21. The file's own comment at `:130` says this claim was deliberately dropped, then re-renders it one card down. |
| Overview card tags | `Overview.tsx:99,232` | `'polyglot workers'`, `'durable streams'`, `'traces on'` — constants in a grid where every other card shows a real count. |
| Overview "live state · GET /console" panel | `Overview.tsx:256` | Not the `/console` response — a hand-concatenated facsimile built by string interpolation, not `JSON.stringify`-escaped, so any id with a quote emits invalid JSON. |
| Models "Provider" column | `simple.tsx:31` | Shows `m.type` (`external`/`custom`), not a provider. Every row reads "Provider: external". |

---

## 7. Unwired / dead

- **`GET /guard/log`, `POST /guard/test`, `GET /tools/log`** — three working endpoints, **zero references** in the console. The only policy *writer* in the product has no audit trail UI, and rule changes can only be tested by committing them to the live egress policy first.
- **`Overview.deployCounts`** — the prop is declared and branched on but **never passed**; `App.tsx` already computes the values for the sidebar. The Deployments card permanently shows placeholder text.
- **`Connections.onToast`** — threaded in, never used. There is no revoke path at all (no server route either), so the emergency action for the credentials view is impossible from the console.
- **`Governance.onToast`** — destructured as `_onToast` and discarded. The governance surface is read-only, including the promotion gate it describes.
- **`usePoll.unauthorized`** — computed, tracked, returned, consumed by nobody; views duplicate it by string-comparing `error === 'unauthorized'`.
- **`NavItem.amberCount`** — set on two entries, never read. Amber comes from a separate `useMemo` in `App.tsx`. The nav-as-data contract is broken.
- **`nav.ts` re-exports `Bot`/`Activity`** for views that don't import them.
- **`Deploy.tsx:702-705`** — an empty `<div className="filters">`, the container for a rollback control that was never added.
- **Isolation-probe REFUTED state** — unreachable; `/posture` is never called with `?verify=true`.
- **`Tools.tsx:217` `override v{n}`** — unreachable; `version` lives on the policy record and `GET /tools` never merges it.
- **Discarded fields:** `EnvPointer.actor`/`updatedAt`, `VersionRun.pinned`, `GuardSaveResponse.version`, `PostureResponse.unmet`, `QuotaViolation.{scope,limit,label,current,max}`, `Worker.{agent,scope,mode}`.
  > **2026-08-07** — Two more belonged on this list and are now fixed with §4.1: **`error.hint`** was discarded for every error shape (`ApiError` had no field for it), and **`ApiError.candidates`** was parsed from a key no server route has ever emitted — dead at both ends. `hint` is now carried and rendered; `candidates` is kept because extra keys inside `error` ride through `_http_error_handler` untouched, so a raise site adding a structured list would work without another envelope change. The rest of the list stands.
- **Three dead CSS classes** — `pat`, `meth`, `note` (`Guard.tsx:474,487,499`) have no rule in `styles.css`. Harmless: layout comes from `.grule`'s grid and `.grule .gin{width:100%}`. Full cross-check of all 138 CSS classes against all 118 static `className` tokens found no *missing* style — these three are the only orphans, and in the unused direction.

---

## 8. Missing functionality

The console exercises **42 of ~92 distinct server capabilities (~46%)**. Most of the unused half is legitimately CLI/SDK/worker-only, but four are real product gaps:

- **No realtime.** `WS /ws` is fully implemented and never opened. The "live" pill reflects only whether the last 6s HTTP poll succeeded — it is not a connection.
- **No file management.** A complete upload/presign/confirm/list/get API with zero console surface.
- **No promotion-gate editor.** `PUT /agents/{id}/gate` is unused; the egress guard gets a full editor, the gate gets none.
- **No audit trails** for the two things the console itself writes (guard policy, kill switches).
- **Unreachable data:** run #31+ and session #51+ cannot be reached from the console at all, despite `GET /agents/{id}/runs` and `/sessions` existing with full lists.
- **`GET /lifecycle` never called** — a D31-disabled workspace shows every ceiling "within limit" and posture green while every run is refused.

**On promote/rollback/retire:** the absence of these controls is **deliberate and documented** (`Deploy.tsx:485-489`) — promotion is actor-attributed and belongs to `rya promote`. Not counted as a stub. Worth revisiting though: the gate dry-run is *already* fetched and rendered, and `Team.tsx` proves a real session identity is available. A gated promote requiring the account session and refusing when `check.allowed` is false would satisfy the stated objection rather than route around it.

---

## 9. Test gaps

164 tests pass and most are genuinely good *(now 325 — see §0)*. The gaps are specific:

- ~~**No test for `lib/api.ts`**~~ — **FIXED 2026-08-07**, `src/lib/api.test.ts`, now **52 tests**: bearer attachment (and its absence), the 401 fan-out and unsubscribe, `UnauthorizedError` *not* being an `ApiError`, 204 → `null`, non-JSON error bodies, the `authPost`/`sessionPost` credential split, and the `?api=` base. Updated with §4.1 and §3.5: the envelope block now pins **one** shape (there are no longer "both envelope shapes"), asserts the `hint` survives and composes, asserts the *retired* shapes do **not** resolve to something plausible, and covers non-Rya intermediaries. The `?api=` block adds the exfiltration case, the dodging spellings, and inertness in a production build. Extended again with §4.2: the 401 block now covers the *classification* — which codes raise the Connect dialog, which do not, that `message` stays the sentinel five views depend on, and that `HTTP 401` is never offered as a reason — plus `unauthorizedError()` tested directly. Extended once more with §4.3: the user-identity bridge — minting, the two non-error refusal paths, the header riding alongside the workspace key, the re-mint-on-expiry retry and its single-retry bound.
- ~~**No `Approvals.test.tsx`**~~ — **FIXED 2026-08-07** with §4.4: 17 tests over the only view that POSTs an irreversible governance action. Most of them assert what is *rendered*, because on this screen the rendering IS the feature — the sharpest are negative (the amount must not be missing, the agent must not be misattributed, the icon must not claim the action is something it is not).
- ~~**No ErrorBoundary test**~~ — **FIXED 2026-08-07** with §3.4: `components/ErrorBoundary.test.tsx` (10) and `App.boundary.test.tsx` (2), the latter mocking a view to throw and asserting the shell survives.
- ~~**No test drives an auth failure through the shell**~~ — **FIXED 2026-08-07** with §4.2: `App.unauthorized.test.tsx` (3) renders the real `App` against a 401-ing server and asserts what the *operator* ends up looking at — the dialog with the server's reason inside it, or, in bank mode, a toast naming the remedy and no dialog at all. The unit tests could not have caught the bank-mode case: the defect was that a modal appeared, which is only observable at the shell.
- **No test for `AuthModal.tsx`** — signup, login, and API-key minting.
- ~~**No `Approvals.test.tsx`**~~ — a duplicate of the bullet above, left behind when §4.4 landed. Removed rather than re-answered.
- **`Governance.test.tsx` asserted a field the server never sent** — **FIXED 2026-08-07** with §4.5, now **17 tests**. The old fixture invented a per-override `version` so that `expect(getByText('v4'))` passed against a column rendering `vundefined` in production; this is the "fixtures encode bugs" pattern below, caught in the act. The new tests cover the two `.anote` failure bands (unreadable policy store, unreadable guard), the four distinct egress-source states, the derived per-tool history including **Who**, and the override count — nine of the seventeen are about telling *unknown* apart from *nothing*.
- **Six views (`manifest`, `memory`, `models`, `channels`, `secrets`, `jobs`) have no assertions.** Their only coverage is a smoke loop that waits on the *shell*; replacing `SecretsView` with `() => null` still passes.
- **No e2e anywhere.** The CSP is asserted by string-matching the header, not by observing a browser doesn't block anything. The two failure classes this console is most exposed to — a CSP break and a stale-index/asset-hash mismatch — are invisible to the entire suite.
  > **PARTLY FIXED 2026-08-07** — `scripts/smoke_console.sh` runs against a real server (the `image` CI job runs it against the built container): it fetches the index the server actually serves, follows every hashed `/assets/*` it references and checks status *and* content-type on each, reads the CSP off the wire, and distinguishes the unbuilt-bundle 503 by name. That closes the asset-mismatch class outright and the serving half of the CSP class. Still no browser, so a policy that blocks a runtime `connect-src` would pass — Playwright remains the honest answer for that.
- **Fixtures encode bugs rather than catch them:** `handlers: ['event']` (server sends a dict), a `members` array containing the owner (server never sends it), a `posture` with no `topology`, a `probe` on an unverified request, and a per-override kill-switch `version` (§4.5). Each must be corrected *before* the corresponding fix can be pinned.
  > **PARTLY FIXED 2026-08-07** — Three instances of this pattern were corrected as their findings landed. The 11 error-response fixtures across 9 test files emitted the retired `{detail: {…}}` shape and now emit the real envelope (§4.1); the `drops a rule with no pattern…` test in `Guard.test.tsx` asserted the data loss as *intended* behaviour and was rewritten (§3.3); and `Governance.test.tsx`'s fabricated per-override `version` made a column that rendered `vundefined` in production look tested (§4.5). The four remaining stand. Care was taken to leave alone the `detail` keys that are legitimate payload fields — governance violations, posture probes, eval and gate checks all carry their own `detail`.
- ~~Four `act(...)` warnings in `Knowledge.test.tsx`~~ — **FIXED 2026-08-07**. All four were the synchronous tests: `useLoad` fires on mount and, with an empty query, resolves to `null` one microtask after the test body has ended. `findBy*` awaits that settle inside `act`. Also raised Testing Library's `asyncUtilTimeout` to 4s (vitest `testTimeout` to 15s) — every `fetch` here is stubbed, so a `waitFor` needing more than a second is waiting on the CPU, and a 2-core CI runner is exactly where that bites.

**Also found while wiring CI, outside the console:** `tests/test_documents.py` imported `pypdf`, which was declared in no extra anywhere — the Python suite failed on any clean checkout. Added to the `dev` extra in both root and `packaging/server` pyprojects (`test_sdk_surface.py` pins them equal) and `uv.lock` regenerated. Suite is now 895 passed / 0 failed on 3.12 and 892 / 0 on 3.10. *(2026-08-07: **899** on 3.12 after §4.1's envelope-contract tests.)*

---

## 10. Coverage — what this audit did and did not cover

**Fully covered — `web/console`, the operator console (100%):**
all 16 view files, 5 components, 8 `lib/` files, `App.tsx`, `main.tsx`, `styles.css` (full class cross-check), `index.html`, `vite.config.ts`, `tsconfig.json`, all 17 test files, the build, the wheel packaging, the serving path in `src/rya/api/app.py`, and a full call-vs-route reconciliation against all ~114 server route decorators.

**Not covered — three other frontend surfaces exist in this repo:**

| Surface | Size | What it is |
|---|---|---|
| `clients/typescript/` | 2,441 lines, 9 files | The TypeScript SDK (`client.ts`, `http.ts`, `sse.ts`, `turns.ts`, `queue.ts`, `errors.ts`, `types.ts`). Any browser app built on Rya goes through this. Note it has its own SSE and error-parsing implementations — the error-envelope bug in §4.1 and the stream contract in §2 are both worth re-checking here. |
| `examples/loan-renewal/web/index.html` | 974 lines | A complete example end-user web app. |
| `site/index.html` | 436 lines | The marketing/landing page. |

None of these are the operator console, so none were in the original scope — but if "the entire frontend" is the target, `clients/typescript/` is the one that matters most, because it is shipped to users and it re-implements two of the contracts this audit found defects in.

---

## 11. Suggested sequence

**First — cheap, high leverage.** Fix the error envelope (§4.1, two lines). Nearly every other failure is currently invisible or misattributed because of it. Then add the ErrorBoundary (§3.4).

**Then — stop the data loss.** Guard save merge + `url:` normalisation (§3.3). Point Governance at the live policy store (§4.5). Fix `Team.signedIn` (§4.7) and the false success toasts.

**Then — release path.** CI workflow, Docker builder stage, `.dockerignore` (§3.1, §3.2). One line for CORS DELETE, two words out of `connect-src`, `import.meta.env.DEV` on `?api=`, `Cache-Control` on two responses.

**Then — honesty of the data.** Delete the nine stub columns/cards in §6 or make them measured. Fix the truncation lies (§5.1, §5.2) and the loading/error tile states (§5.4, §5.10). *(All four are now done — see §5a and §5b. What survives from this paragraph is the §6 inventory.)*

**Then — the rest of P2**, and the test gaps in §9 alongside the fixes they pin.

The console itself is in good shape. Most of what blocks it is around it, and the estimate of roughly a day for every blocking item in §3 is realistic.

---

### 11a. Revised sequence — 2026-08-07

Now closed from the sequence above: §4.1, §3.4, §3.3, §3.1, §3.2, and the
`import.meta.env.DEV` item (§3.5). The order held up — fixing the envelope first was right, because it was making the other
findings unreadable rather than merely inconvenient. One correction: **§4.1 was not a
two-line fix.** Treating it as one would have papered over two error conventions in a single
service. It cost three server handlers, one client parser, 18 test conversions and a
documentation section, and it was worth all of it.

Also closed, out of sequence and in parallel: **§5.1–§5.5** (see §5a). Worth recording why
they were not deferred to "the rest of P2": four of the five were one root cause — views
reading the `/console` aggregate as a dataset — and fixing that once, with a written
boundary, was cheaper than fixing five symptoms later and much cheaper than fixing them
after §6 had added more consumers of the same previews. It cost a server contract
(`limit`/`offset`/`count` on two listings, one shared filter definition, one shared run row)
rather than view-local patches, which is the same trade §4.1 turned out to be.

And **§5.6–§5.10** (see §5b), for the same reason one layer down: four of those five live in
`usePoll`/`useLoad`, which every view fetches through, so they were going to be touched by
any subsequent P2 work regardless — and §5.6 in particular had just been made worse by §5.1
and §5.2, which moved two more views onto their own fetches and therefore out of the Refresh
button's reach. Fixing the hooks before adding views to them is the cheap ordering; the
alternative was nine `reload` props and a tenth view that forgets. §5.7 rode along because it
is the same lesson as §5.1's shared filter, stated about a different set: **when the client
keeps its own copy of something the server defines, drift is not an error, it is a quiet
disagreement.**

And **§5.11–§5.15** (see §5c). These did *not* share one root cause, and the ordering reason
is different: three of them are the shell's account of itself — the auth gate, the session
boundary and the agent selection — and all three sit in `App.tsx`, which had just been
restructured twice (§5.3's view key, §5b's refresh signal and staleness state). Touching that
file a third time to add a session boundary was much cheaper than touching it once more later,
and the §5.11 fix in particular is a five-line wrapper that only reads as obvious once
`App.tsx` is already the only place shell state lives. §5.13 and §5.15 rode along as ordinary
per-view work, and §5.15 produced the shared `ConfirmDialog` that §5.21 needs. Two things
worth flagging for the remaining P2 work: **§5.12 removes a request rather than adding one**
— a gated console used to fire a doomed `/console` on every load — and §5.11's remount is the
first structure here that discards view state deliberately, so anything added below `Console`
inherits correct sign-out behaviour for free and should not re-implement it.

And **§5.16–§5.20** (see §5d), which were taken next for a reason worth stating because it
is not "they were the next numbers". Three of the five are the serving path, and the audit
had them queued as item 4 below under "the one-liners" — which was true of the edits and
wrong about the work. Getting them right meant establishing what the wire actually returns,
and once a live server was answering, all three could be verified at once for barely more
than the cost of verifying one; splitting them across two batches would have meant standing
that up twice. **They also paid for the check that catches the next one.**
`smoke_console.sh` went from 11 checks to 15, and it is the only thing in this repo that
looks at a real response — every one of the three had survived a full local test suite, a
clean `tsc`, and a working console in a browser, because none of them can be exhibited by a
same-origin `rya serve` with no CDN in front of it. §5.16 and §5.17 rode along as ordinary
per-view work. Two notes for what remains: §5.17 is the **fourth** appearance of
unknown-rendered-as-a-definite-answer (§5.4, §5.10, §4.10, and now a status rather than a
count), which is the strongest signal in this document about what to look for next; and
§5.18's `/mcp` preflight discovery is a **new** finding, recorded in §5d rather than fixed,
because it is precisely the behaviour that the rejected refactor would have changed.

**Next, in order:**

1. **§4.7 Team & access** — the false success is the dangerous half: `remove_member` returns
   `{"removed": false}` and `revoke_key` returns `{"ok": false}`, and the console toasts
   success without reading either, so an operator believes a compromised key is dead. The
   dead `signedIn` flow (`setSignedIn(true)` does not exist in the file) rides along.
1b. **§4.6 the rest of it** — narrowed by §4.5 (see its note), so what is left is the manifest
   YAML, `.env` secret names and branding all reading one process-level `root` regardless of
   `?agent=`. Worth doing next to §4.7 because both are about showing one tenant another's.
2. **A Postgres service in CI** — cheap, and §4.3 showed what the 57-test skip was hiding.
   Also: the API-key-only entry path leaves bank mode unresolvable (see §4.3's FIXED note).
3. **§4.9 / §4.10** — the remaining outage-vs-idle confusions, both in Workers. Cheap, and
   they are the class of bug this console exists to prevent. (§5.4 and §5.10 are done: the
   Queue tiles and the sidebar badge now distinguish a real zero from a count they never
   got. §4.10 is the same mistake in the Workers *view*, where a crash loop still reads as
   scaled to zero — the pattern to copy is `NavCount` and the Queue's `depth()`.)
4. **§5.21–§5.27** — the remaining P2s, and §5.21 is the one with a live consequence:
   "Send test event" posts a fabricated payload to the running agent with no confirmation
   and no prod check, spending real money and writing a fake customer into the trace.
   `ConfirmDialog` (§5c) is already there for it. §5.23 and §5.27 are minutes each.
5. **§6 stub inventory** — delete or measure. Nine constants dressed as data, one of them
   (memory "Scope: per-agent") a false isolation claim.
6. **CSP coverage** — the `<meta>` CSP question left open under §3.5, which needs a decision
   about dev exemptions before it can land. §5.20 narrowed the policy; it did not widen
   where the policy is applied.
7. **The `/mcp` preflight `401`** recorded at the end of §5d — a new finding, not in the
   numbered set, and the reason the CORS registration was left where it is.
