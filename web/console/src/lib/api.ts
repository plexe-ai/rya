// The single fetch path for the console, ported from the legacy SPA's `api()`.
//
// Two behaviours are load-bearing and must not drift:
//  1. A 401 means "the token is missing or stale" -> the Connect dialog opens and
//     the caller sees `UnauthorizedError`. Callers distinguish this from a real
//     outage: an un-authenticated console is NOT "the runtime is down".
//  2. FastAPI reports errors as `{detail: {message}}`; surface that message rather
//     than a bare status code.

/** Same-origin by default (served by `rya serve`); `?api=…` overrides for dev. */
export const API = (new URLSearchParams(location.search).get('api') || '').replace(/\/$/, '')

const TOKEN_KEY = 'rya_token'
const SESSION_KEY = 'rya_session'
const EMAIL_KEY = 'rya_email'

export const getToken = () => localStorage.getItem(TOKEN_KEY) || ''
export const setToken = (t: string) => localStorage.setItem(TOKEN_KEY, t)
export const getSession = () => localStorage.getItem(SESSION_KEY)
export const getEmail = () => localStorage.getItem(EMAIL_KEY) || ''

export function saveSession(token: string, email?: string) {
  localStorage.setItem(SESSION_KEY, token)
  if (email) localStorage.setItem(EMAIL_KEY, email)
}

export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(SESSION_KEY)
  localStorage.removeItem(EMAIL_KEY)
}

/** Thrown on 401. The shell opens the auth modal; views render a "connect" empty state. */
export class UnauthorizedError extends Error {
  constructor() {
    super('unauthorized')
    this.name = 'UnauthorizedError'
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
  candidates?: string[]
  constructor(message: string, status: number, code?: string, candidates?: string[]) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.candidates = candidates
  }
}

/**
 * Subscribers notified on any 401, so the shell can raise the auth modal from
 * wherever the failing call happened to be. A plain callback set beats threading
 * an onUnauthorized prop through every view.
 */
const unauthorizedHandlers = new Set<() => void>()
/** Returns an unsubscribe function usable directly as a `useEffect` destructor. */
export function onUnauthorized(fn: () => void): () => void {
  unauthorizedHandlers.add(fn)
  return () => {
    unauthorizedHandlers.delete(fn)
  }
}

export async function api<T = unknown>(path: string, opts: RequestInit = {}): Promise<T> {
  const headers = new Headers(opts.headers)
  const t = getToken()
  if (t) headers.set('Authorization', `Bearer ${t}`)

  const r = await fetch(API + path, { ...opts, headers })

  if (r.status === 401) {
    unauthorizedHandlers.forEach((fn) => fn())
    throw new UnauthorizedError()
  }
  if (!r.ok) {
    let message = `HTTP ${r.status}`
    let code: string | undefined
    let candidates: string[] | undefined
    try {
      const body = await r.json()
      message = body?.detail?.message || body?.detail || message
      code = body?.detail?.code ?? body?.code
      candidates = body?.detail?.candidates
    } catch {
      /* a non-JSON error body is still an error; keep the status line */
    }
    throw new ApiError(message, r.status, code, candidates)
  }
  return (r.status === 204 ? null : await r.json()) as T
}

/** POST without a bearer token — signup/login, which mint one. */
export async function authPost<T = unknown>(path: string, body: unknown): Promise<T> {
  const r = await fetch(API + path, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
  const d = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(d?.detail?.message || `HTTP ${r.status}`)
  return d as T
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
  const d = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(d?.detail?.message || `HTTP ${r.status}`)
  return d as T
}
