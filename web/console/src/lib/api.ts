// The single fetch path for the console, ported from the legacy SPA's `api()`.
//
// Two behaviours are load-bearing and must not drift:
//  1. A 401 is classified, not assumed. When the stored credential IS the problem
//     the Connect dialog opens and the caller sees `UnauthorizedError`; when it is
//     not (bank mode's `E_APPROVER_IDENTITY_REQUIRED`, say) the caller sees a plain
//     `ApiError` and the dialog stays shut. Either way this is NOT "the runtime is
//     down" — an un-authenticated console is a distinct state from an outage.
//     See `unauthorizedError`.
//  2. Every failure carries a `message`, a stable `code` and a `hint`, and all
//     three reach the operator. The api emits ONE envelope for this —
//     `{ok: false, error: {code, message, hint, exit_code}}`, the same one the CLI
//     and MCP use — so there is exactly one thing to read. See `readError`.
//
// This comment used to say "FastAPI reports errors as `{detail: {message}}`", and
// that sentence was the bug: it is true of `HTTPException` and of nothing else the
// server raises, so the whole `RyaError` vocabulary — quota, governance,
// versioning, agent addressing — was parsed to the literal string `HTTP 400`. The
// envelope was unified server-side rather than met with a cleverer parser here.

// Type-only, so this erases at compile time and cannot make a cycle with `types.ts`.
import type { RuntimeInfo } from './types'

/**
 * Loopback only. `localhost`, IPv4 loopback, and IPv6 `::1` (which `URL` reports
 * bracketed). Nothing else is a plausible `rya serve` on a developer's machine.
 */
const LOOPBACK = new Set(['localhost', '127.0.0.1', '[::1]', '::1'])

/**
 * `?api=…` — the dev override, validated.
 *
 * Exported for the tests, and called ONLY under `import.meta.env.DEV`.
 *
 * Read unconditionally, this was a credential exfiltration primitive: `API` is
 * prefixed onto every request in this file, plus `Team.tsx` and the Queue's SSE
 * reader, and each of those attaches `Authorization: Bearer …` — the workspace API
 * key (`rya_sk_…`), which can approve actions, rewrite the Action Guard and flip
 * kill switches, or the account session token, which is worse. So
 * `https://console.example.com/?api=https://evil.tld` was a one-click handover, and
 * the only thing standing in front of it was a `connect-src 'self'` that the server
 * stamps on exactly two responses — absent entirely from any deployment fronting
 * `dist/` with nginx, S3 or CloudFront.
 *
 * The destination is checked rather than the input merely sanitised, because the
 * question is not "does this look like a URL" but "does this credential leave the
 * origin". Same-origin (any relative path) and loopback both answer no.
 */
export function devApiBase(search: string, origin: string): string {
  const raw = (new URLSearchParams(search).get('api') || '').trim().replace(/\/$/, '')
  if (!raw) return ''
  let target: URL
  try {
    // Resolved against the page origin, so a relative path, a protocol-relative
    // `//evil.tld` and an absolute URL all reduce to one comparison.
    target = new URL(raw, origin)
  } catch {
    return ''
  }
  if (target.origin === origin) return raw
  if ((target.protocol === 'http:' || target.protocol === 'https:') && LOOPBACK.has(target.hostname)) {
    return raw
  }
  console.warn(`[rya] ignoring ?api=${raw} — the API base may not leave this origin.`)
  return ''
}

/**
 * Same-origin by default: the console is served by `rya serve` from the same origin
 * as the API, so the base is the empty string and every path is relative.
 *
 * The `?api=` override exists for `npm run dev` and is compiled out of production
 * builds — `import.meta.env.DEV` is replaced with the literal `false`, the ternary
 * folds, and `devApiBase` is left unreferenced for the bundler to drop. A gate that
 * merely *rejects* bad values at runtime would still ship the parsing; this ships
 * nothing. Note the dev server proxies `/console`, `/agents`, `/v1` and the rest to
 * `127.0.0.1:8000` already (`vite.config.ts`), so the override is a fallback for a
 * backend on a different port, not the normal path.
 */
export const API = import.meta.env.DEV ? devApiBase(location.search, location.origin) : ''

const TOKEN_KEY = 'rya_token'
const SESSION_KEY = 'rya_session'
const EMAIL_KEY = 'rya_email'
const USER_TOKEN_KEY = 'rya_user_token'

export const getToken = () => localStorage.getItem(TOKEN_KEY) || ''
export const setToken = (t: string) => localStorage.setItem(TOKEN_KEY, t)
export const getSession = () => localStorage.getItem(SESSION_KEY)
export const getEmail = () => localStorage.getItem(EMAIL_KEY) || ''

/**
 * The short-lived user JWT, sent as `X-Rya-User-Token`.
 *
 * THREE credentials live in this browser and they are not interchangeable:
 *  - `rya_token` — the workspace API key (`rya_sk_…`) or operator token. Says which
 *    WORKSPACE is calling. Long-lived.
 *  - `rya_session` — the account session from email sign-in. Says which ACCOUNT is
 *    signed in. Only ever sent to `/v1/*`.
 *  - `rya_user_token` — a 12-hour JWT minted from the session by `POST /v1/token`.
 *    Says which USER a data-plane request is for. Sent alongside the API key, never
 *    instead of it.
 *
 * The third one did not exist here, which was §4.3: the console authenticated as a
 * workspace and never as a person, so approvals recorded `resolvedBy: null`, per-user
 * RLS never engaged, and bank mode
 * (`RYA_REQUIRE_APPROVER_IDENTITY=1`) made approvals unresolvable from the console.
 */
export const getUserToken = () => localStorage.getItem(USER_TOKEN_KEY) || ''

export function saveSession(token: string, email?: string) {
  localStorage.setItem(SESSION_KEY, token)
  if (email) localStorage.setItem(EMAIL_KEY, email)
}

export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(SESSION_KEY)
  localStorage.removeItem(EMAIL_KEY)
  localStorage.removeItem(USER_TOKEN_KEY)
}

/**
 * `GET /v1/info`, fetched at most once per page load.
 *
 * Two callers need it and they used to be one caller: the auth modal fetched it to
 * decide which tabs to offer, and nobody asked it the more basic question — *does
 * this runtime want a credential at all?* — which is why `authRequired` was returned
 * and discarded and a default `rya serve` opened on a locked door (§5.12).
 *
 * Shared here rather than fetched twice because the two answers must agree: a shell
 * that decided "auth is required" from one response and a dialog that decided "this
 * is single-tenant" from another could, on a runtime reconfigured between the two,
 * offer a set of tabs for a mode the gate was not in. One request, one answer.
 *
 * **Never rejects, and never assumes the runtime is open.** A discovery endpoint that
 * cannot be reached tells you nothing, and the safe reading of nothing is `{}`: every
 * consumer treats an absent `authRequired` as "ask for a credential", so an unreachable
 * or non-Rya server degrades to exactly the behaviour the console had before this
 * existed. Only an explicit `authRequired: false` opens the door.
 *
 * Unauthenticated on purpose — `cloud_info` takes no `Depends(get_plane)`, which is
 * what makes it usable as the thing that decides whether to authenticate.
 */
let infoOnce: Promise<RuntimeInfo> | null = null

export function runtimeInfo(): Promise<RuntimeInfo> {
  infoOnce ??= fetch(`${API}/v1/info`)
    .then((r) => (r.ok ? (r.json() as Promise<RuntimeInfo>) : {}))
    .catch(() => ({}) as RuntimeInfo)
  return infoOnce
}

/**
 * Drop the cached `/v1/info`.
 *
 * For tests, and for the one runtime event that can invalidate it: signing out ends
 * a session, and the next one may be pointed at a different runtime entirely.
 */
export function resetRuntimeInfo(): void {
  infoOnce = null
}

/**
 * Exchange the account session for a user JWT, and remember it.
 *
 * Returns the token, or `''` when there is no session or the runtime declines. Both
 * are ordinary states rather than errors: `POST /v1/token` calls `_require_mt()` and
 * answers 400 on a single-tenant runtime, where identity comes from the operator's own
 * bearer JWT instead — and an operator who pasted an API key without signing in has no
 * session to exchange. **Neither may block the console**, so this resolves rather than
 * throws; a failure means requests go out unattributed, exactly as they did before.
 *
 * A failure that MATTERS is still visible: bank mode answers
 * `E_APPROVER_IDENTITY_REQUIRED` with a hint naming this exact exchange, and since
 * §4.2 that message reaches the operator instead of being reported as a stale token.
 */
export async function mintUserToken(): Promise<string> {
  if (!getSession()) return ''
  try {
    const d = await sessionPost<{ userToken?: string }>('/v1/token', {})
    const t = d.userToken || ''
    if (t) localStorage.setItem(USER_TOKEN_KEY, t)
    return t
  } catch (e) {
    // Warn rather than toast: on a single-tenant runtime this is expected, and the
    // console works fine without it.
    console.warn('[rya] could not mint a user token — requests will be unattributed:', e)
    return ''
  }
}

/**
 * Thrown on a 401 that the *stored credential* explains. The shell opens the auth
 * modal; views render a "connect" empty state.
 *
 * `message` is the fixed sentinel `'unauthorized'` and must stay that way: five
 * views switch their empty state on `error === 'unauthorized'` — `Evals.tsx`,
 * `Guard.tsx`, `Quotas.tsx`, `Deploy.tsx` and `Queue.tsx` — because `useLoad` hands
 * them a string, not this object. The sentinel is the *identity* of the condition;
 * the server's account of it rides in `reason`/`hint`, which is what the Connect
 * dialog renders.
 *
 * A 401 the credential does NOT explain is an `ApiError`, so those same views fall
 * through to their real-message arm without any per-view change.
 */
export class UnauthorizedError extends Error {
  readonly status = 401
  code?: string
  /**
   * The server's own words, when it gave any. Undefined for a bodiless 401 or a
   * gateway's HTML — `HTTP 401` is a status line, not an explanation, and must not
   * be presented to the operator as the reason they are being asked to reconnect.
   */
  reason?: string
  hint?: string
  constructor(reason?: string, code?: string, hint?: string) {
    super('unauthorized')
    this.name = 'UnauthorizedError'
    this.reason = reason
    this.code = code
    this.hint = hint
  }
}

/**
 * A non-401 API failure, carrying the Rya error CODE and not just its prose.
 *
 * Callers have to be able to tell a recoverable condition from an outage, and the
 * only stable discriminator is the code: `E_AGENT_NOT_FOUND` on `/console` means a
 * remembered agent selection has gone stale (drop it and retry), whereas anything
 * else means the runtime is unreachable or broken. String-matching a message
 * written for humans is not a contract.
 */
export class ApiError extends Error {
  code?: string
  status: number
  /**
   * What to do next, from the server. First-class in `RyaError` and in the CLI
   * (`docs/devex.md`) and previously discarded here for every shape — `ApiError`
   * had no field to put it in, so `Plane.sole_agent` naming both candidate agents
   * reached the operator as nothing at all.
   */
  hint?: string
  candidates?: string[]
  constructor(
    message: string,
    status: number,
    code?: string,
    hint?: string,
    candidates?: string[],
  ) {
    // The hint rides in `message` because every view already toasts `e.message`
    // and every empty-state already renders it; composing here is what makes the
    // hint reach the operator without touching 23 views. `.toast` is `max-width:
    // 80vw` and wraps, so there is room. Read `.hint` to render them apart.
    super(hint ? `${message} — ${hint}` : message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.hint = hint
    this.candidates = candidates
  }
}

/** The one error envelope, as the server now guarantees it. */
interface ErrorEnvelope {
  message: string
  code?: string
  hint?: string
  candidates?: string[]
  /**
   * True when `message` came from the server, false when it is the synthesised
   * `HTTP <status>` fallback. Callers that render the server's own words rather
   * than an error string — the Connect dialog — need to tell those apart.
   */
  fromServer: boolean
}

/**
 * Read a failure body.
 *
 * The server emits ONE shape for every failure — `RyaError`, `HTTPException`,
 * Starlette's own 404/405, and FastAPI's validation array all pass through three
 * handlers in `api/app.py` and come out as:
 *
 *     {"ok": false, "error": {"code": "E_*", "message", "hint", "exit_code"}}
 *
 * so this reads one field and is done. It used to be three shapes, and this
 * function used to be one expression that understood only the `HTTPException`
 * one; the other two — which is most of the quota, governance, versioning and
 * agent-addressing vocabulary — rendered as the literal string `HTTP 400`.
 *
 * The fallbacks below are NOT the contract. They exist because not every 4xx/5xx
 * a browser sees comes from Rya at all: an nginx 502, a CloudFront error page or
 * a corporate proxy will answer with HTML or a shape of its own invention, and
 * "HTTP 502" is a better thing to show than a crash in the error handler.
 */
function readError(body: unknown, status: number): ErrorEnvelope {
  const fallback = `HTTP ${status}`
  const synthesised = { message: fallback, fromServer: false }
  if (!body || typeof body !== 'object') return synthesised
  const b = body as Record<string, unknown>
  const err = (typeof b.error === 'object' && b.error !== null ? b.error : null) as Record<
    string,
    unknown
  > | null
  if (err) {
    const message = typeof err.message === 'string' ? err.message : ''
    return {
      message: message || fallback,
      fromServer: !!message,
      code: typeof err.code === 'string' ? err.code : undefined,
      hint: typeof err.hint === 'string' && err.hint ? err.hint : undefined,
      candidates: Array.isArray(err.candidates) ? (err.candidates as string[]) : undefined,
    }
  }
  // Not ours. Take a top-level string message if the intermediary offered one.
  const loose = b.message ?? b.detail
  if (typeof loose === 'string' && loose) return { message: loose, fromServer: true }
  return synthesised
}

/**
 * The operator-facing text for a failure body.
 *
 * For callers that need the string but not an `ApiError` — `Team.tsx` runs its own
 * `fetch` because it authenticates with the account session rather than the
 * workspace key, and it must not grow a fifth copy of this parse.
 */
export function readErrorMessage(body: unknown, status: number): string {
  const e = readError(body, status)
  return e.hint ? `${e.message} — ${e.hint}` : e.message
}

/** Parse a failed `Response` into the error it describes. */
async function errorFrom(r: Response): Promise<ApiError> {
  // `.json()` throws on an HTML error page, which is exactly what a gateway sends.
  const body = await r.json().catch(() => null)
  const e = readError(body, r.status)
  return new ApiError(e.message, r.status, e.code, e.hint, e.candidates)
}

/**
 * The codes for which "your credential is missing or stale" is a true statement,
 * and re-entering one is therefore the remedy.
 *
 * `E_UNAUTHORIZED` is the whole set today. It covers `_check_token` (bad operator
 * token), `authorize` (bad workspace key) and every `verify_jwt` failure — malformed,
 * expired, bad signature, no `sub`. Those differ in what the operator should do
 * next, which is exactly why `reason` is carried through to the dialog instead of
 * being collapsed into the word "unauthorized".
 */
const CREDENTIAL_CODES = new Set(['E_UNAUTHORIZED'])

/**
 * Classify a 401.
 *
 * The console used to skip this: `api()` fired the unauthorized handlers and threw a
 * payload-free `UnauthorizedError` without reading the body, so ONE decision ("the
 * status is 401") produced TWO conclusions — *your credential is stale* and *the fix
 * is to re-paste it*. The server means at least five different things by 401, and
 * for two of them both conclusions are false:
 *
 *  - `E_APPROVER_IDENTITY_REQUIRED` (bank mode, `RYA_REQUIRE_APPROVER_IDENTITY=1`)
 *    is raised by `_actor_from` on `/approvals/{id}/approve` and `/reject` only. The
 *    key is fine; the request needs an `X-Rya-User-Token` as well. Raising the
 *    Connect dialog over the operator's work demanded they re-paste a credential
 *    that was never the problem, and froze the shell's poll while it was open.
 *  - `E_BAD_SIGNATURE` is about the webhook HMAC, not the caller's identity.
 *
 * The default cuts on whether there is a code at all, not on whether we recognise it:
 *
 *  - NO code — a bodiless 401, a gateway's HTML, an SSO proxy — is a credential
 *    failure. This is the pre-existing behaviour and the only safe reading, because
 *    "you are not authenticated" is all the bare status reliably conveys.
 *  - An UNRECOGNISED code is not. A code means Rya answered and chose to say
 *    something specific, so the honest move is to show what it said. Erring the other
 *    way would re-create this very bug for the next 401 code somebody adds: the dialog
 *    would open over a credential that is fine. The cost of this direction is one
 *    extra click — the operator reads an accurate message and presses Connect
 *    themselves — and the shell's `/console` poll raises the dialog on its own for any
 *    genuine credential failure, since every auth gate in `app.py` uses
 *    `E_UNAUTHORIZED`.
 *
 * Exported because the Queue's SSE reader runs its own `fetch` and has to classify
 * identically — a second copy of this decision is how §4.1's four-way parse split
 * happened.
 */
export function unauthorizedError(body: unknown): UnauthorizedError | ApiError {
  const e = readError(body, 401)
  if (e.code && !CREDENTIAL_CODES.has(e.code)) {
    return new ApiError(e.message, 401, e.code, e.hint, e.candidates)
  }
  return new UnauthorizedError(e.fromServer ? e.message : undefined, e.code, e.hint)
}

/**
 * True for any 401, however `unauthorizedError` classified it.
 *
 * "Is this an outage?" and "is this a credential problem?" are different questions,
 * and the answer to the first is no for *every* 401. Code that only means the second
 * should test `instanceof UnauthorizedError`; code that means the first wants this,
 * because splitting 401 across two classes would otherwise let a non-credential one
 * be reported as "Lost connection to the runtime".
 */
export const isUnauthenticated = (e: unknown): boolean =>
  (e instanceof UnauthorizedError || e instanceof ApiError) && e.status === 401

/**
 * Subscribers notified on any 401, so the shell can raise the auth modal from
 * wherever the failing call happened to be. A plain callback set beats threading
 * an onUnauthorized prop through every view.
 */
const unauthorizedHandlers = new Set<(e: UnauthorizedError) => void>()
/**
 * Returns an unsubscribe function usable directly as a `useEffect` destructor.
 *
 * The handler receives the error, so the dialog can say WHY it opened. Handlers that
 * do not care may still take no arguments.
 */
export function onUnauthorized(fn: (e: UnauthorizedError) => void): () => void {
  unauthorizedHandlers.add(fn)
  return () => {
    unauthorizedHandlers.delete(fn)
  }
}

/**
 * `retryAfterRemint` is internal. See the 401 branch: the user JWT lasts 12 hours and
 * the session outlives it, so its expiry is the one 401 the console can resolve on its
 * own, and the retry must not recurse.
 */
export async function api<T = unknown>(
  path: string,
  opts: RequestInit = {},
  retryAfterRemint = true,
): Promise<T> {
  const headers = new Headers(opts.headers)
  const t = getToken()
  if (t) headers.set('Authorization', `Bearer ${t}`)
  // Alongside the workspace key, never instead of it: the key says which workspace,
  // this says which person. The server reads it in three places — per-user RLS
  // (`get_plane`), approval attribution (`_actor_from`) and the run's own Identity
  // (`_identity_from`).
  const u = getUserToken()
  if (u) headers.set('X-Rya-User-Token', u)

  const r = await fetch(API + path, { ...opts, headers })

  if (r.status === 401) {
    // A user JWT expires after 12 hours; the account session behind it lasts longer.
    // So when we sent one and still hold a session, re-mint and retry ONCE before
    // concluding anything — otherwise a console left open overnight would greet the
    // operator with a Connect dialog for a credential that is perfectly good.
    //
    // This does not try to prove the user token was at fault: an expired user JWT and
    // a bad workspace key are both `E_UNAUTHORIZED`, distinguishable only by prose,
    // and string-matching a human-facing message is not a contract. Guessing wrong
    // costs one extra request on a path that is already failing.
    if (retryAfterRemint && u && getSession()) {
      if (await mintUserToken()) return api<T>(path, opts, false)
    }
    const err = unauthorizedError(await r.json().catch(() => null))
    // Only a credential failure raises the dialog. A non-credential 401 is a normal
    // failure from here on: it toasts its message and hint like any other.
    if (err instanceof UnauthorizedError) unauthorizedHandlers.forEach((fn) => fn(err))
    throw err
  }
  if (!r.ok) throw await errorFrom(r)
  return (r.status === 204 ? null : await r.json()) as T
}

/** POST without a bearer token — signup/login, which mint one. */
export async function authPost<T = unknown>(path: string, body: unknown): Promise<T> {
  const r = await fetch(API + path, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
  // Same envelope as everything else. These two used to carry their own weaker
  // copy of the parse (no string arm at all), so signup, login and every account
  // operation reported `HTTP 400` for refusals that had a perfectly good message.
  if (!r.ok) throw await errorFrom(r)
  return (await r.json().catch(() => ({}))) as T
}

/** POST with the *session* token (account-scoped), not the workspace API key. */
export async function sessionPost<T = unknown>(path: string, body: unknown): Promise<T> {
  const session = getSession()
  if (!session) throw new Error('Not signed in.')
  const r = await fetch(API + path, {
    method: 'POST',
    headers: { 'content-type': 'application/json', Authorization: `Bearer ${session}` },
    body: JSON.stringify(body ?? {}),
  })
  if (!r.ok) throw await errorFrom(r)
  return (await r.json().catch(() => ({}))) as T
}
