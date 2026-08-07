import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  ApiError,
  UnauthorizedError,
  api,
  authPost,
  getEmail,
  getSession,
  getToken,
  onUnauthorized,
  saveSession,
  sessionPost,
  setToken,
  unauthorizedError,
  clearAuth,
  getUserToken,
  mintUserToken,
} from './api'

// The single fetch path for the whole console. Every view reaches the server through
// `api()`, so the three behaviours below are load-bearing in a way no view test can
// pin on its own:
//
//   1. the workspace key is attached as a bearer, and ONLY by `api()` — `authPost` is
//      the un-authenticated arm that mints one and `sessionPost` uses the account
//      session instead, and confusing the two is a real credential mix-up;
//   2. a 401 is CLASSIFIED. Only a credential failure fans out to the shell (which
//      raises the Connect dialog) and reaches the caller as `UnauthorizedError`,
//      distinct from an outage; a 401 the credential does not explain is an ordinary
//      `ApiError` and leaves the dialog shut;
//   3. a non-401 failure carries the Rya error CODE, not just prose. Callers branch on
//      `E_AGENT_NOT_FOUND` and read `candidates` off `E_AGENT_AMBIGUOUS`; string-
//      matching a human-facing message is not a contract.

/**
 * A `fetch` stub that records its calls and answers with one canned response.
 *
 * The parameters are declared even though the body ignores them: `vi.fn(() => …)`
 * types `mock.calls` as the empty tuple, so every `calls[0][1]` below would be a
 * type error rather than the init object it plainly is.
 */
function stubFetch(res: Response | (() => Response)) {
  const fn = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
    Promise.resolve(typeof res === 'function' ? res() : res),
  )
  vi.stubGlobal('fetch', fn)
  return fn
}

type FetchStub = ReturnType<typeof stubFetch>

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

/** The URL actually requested on call `i`. */
const sentUrl = (fn: FetchStub, i = 0) => String(fn.mock.calls[i]![0])

/** The `RequestInit` actually passed on call `i`. */
const sentInit = (fn: FetchStub, i = 0): RequestInit => fn.mock.calls[i]![1] ?? {}

/** The headers actually put on the wire for call `i`. */
const sentHeaders = (fn: FetchStub, i = 0) => new Headers(sentInit(fn, i).headers)

describe('api()', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('attaches the stored workspace key as a bearer token', async () => {
    setToken('wk_live_123')
    const fn = stubFetch(json({ ok: true }))

    await api('/console')

    expect(sentUrl(fn)).toBe('/console')
    expect(sentHeaders(fn).get('authorization')).toBe('Bearer wk_live_123')
  })

  it('sends no Authorization header at all when no key is stored', async () => {
    const fn = stubFetch(json({ ok: true }))
    await api('/console')
    // Not an empty `Bearer `: an absent header is what makes a dev server without
    // RYA_TOKEN answer normally instead of rejecting a malformed credential.
    expect(sentHeaders(fn).has('authorization')).toBe(false)
  })

  it('preserves the caller\'s method, body and headers alongside the bearer', async () => {
    setToken('wk_live_123')
    const fn = stubFetch(json({ ok: true }))

    await api('/agents/support/events', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ type: 'ping' }),
    })

    const init = sentInit(fn)
    expect(init.method).toBe('POST')
    expect(init.body).toBe('{"type":"ping"}')
    expect(sentHeaders(fn).get('content-type')).toBe('application/json')
    expect(sentHeaders(fn).get('authorization')).toBe('Bearer wk_live_123')
  })

  it('parses a JSON body on success', async () => {
    stubFetch(json({ agent: { name: 'support-agent' } }))
    await expect(api('/console')).resolves.toEqual({ agent: { name: 'support-agent' } })
  })

  it('resolves 204 to null instead of trying to parse an empty body', async () => {
    // DELETE endpoints answer 204. `r.json()` on an empty body throws, which would
    // surface a delete that actually succeeded as a failure.
    stubFetch(new Response(null, { status: 204 }))
    await expect(api('/secrets/api-key', { method: 'DELETE' })).resolves.toBeNull()
  })

  describe('401', () => {
    it('throws UnauthorizedError and notifies every subscriber', async () => {
      const a = vi.fn()
      const b = vi.fn()
      const offA = onUnauthorized(a)
      const offB = onUnauthorized(b)
      stubFetch(json({ ok: false, error: { message: 'invalid token' } }, 401))

      try {
        await expect(api('/console')).rejects.toBeInstanceOf(UnauthorizedError)
        expect(a).toHaveBeenCalledTimes(1)
        expect(b).toHaveBeenCalledTimes(1)
      } finally {
        offA()
        offB()
      }
    })

    it('is NOT an ApiError, so an outage check cannot swallow it', async () => {
      // Views treat `UnauthorizedError` as "connect", and everything else as "the
      // runtime is down". Making 401 an ApiError subclass would collapse the two.
      stubFetch(json({}, 401))
      const err = await api('/console').catch((e) => e)
      expect(err).toBeInstanceOf(UnauthorizedError)
      expect(err).not.toBeInstanceOf(ApiError)
    })

    it('stops notifying once the subscriber unsubscribes', async () => {
      const fn = vi.fn()
      onUnauthorized(fn)()
      stubFetch(json({}, 401))
      await api('/console').catch(() => {})
      expect(fn).not.toHaveBeenCalled()
    })

    it('keeps `message` as the sentinel five views switch on', async () => {
      // `Evals`, `Guard`, `Quotas`, `Deploy` and `Queue` compare `error === 'unauthorized'`
      // to pick their "connect" empty state, because `useLoad` hands them a string and
      // not this object. Putting the server's prose in `message` would silently turn
      // all five into "… unavailable — Missing or invalid operator token."
      stubFetch(json({ ok: false, error: { code: 'E_UNAUTHORIZED', message: 'Bad key.' } }, 401))
      const err = (await api('/console').catch((e) => e)) as UnauthorizedError
      expect(err.message).toBe('unauthorized')
    })

    it('carries the reason, code and hint so the dialog can say why it opened', async () => {
      // The whole point of §4.2: this body was read and discarded, so "JWT is expired."
      // and "JWT signature verification failed." — both E_UNAUTHORIZED, one routine and
      // one worth investigating — were indistinguishable to the operator.
      stubFetch(
        json(
          {
            ok: false,
            error: { code: 'E_UNAUTHORIZED', message: 'JWT is expired.', hint: 'Sign in again.' },
          },
          401,
        ),
      )
      const err = (await api('/console').catch((e) => e)) as UnauthorizedError
      expect(err).toBeInstanceOf(UnauthorizedError)
      expect(err.reason).toBe('JWT is expired.')
      expect(err.hint).toBe('Sign in again.')
      expect(err.code).toBe('E_UNAUTHORIZED')
      expect(err.status).toBe(401)
    })

    it('does not offer "HTTP 401" as a reason when the body carries none', async () => {
      // A bodiless 401, or a gateway's HTML. `HTTP 401` is a status line, not an
      // explanation, and the dialog must not present it as one.
      stubFetch(new Response('<html>nope</html>', { status: 401 }))
      const err = (await api('/console').catch((e) => e)) as UnauthorizedError
      expect(err).toBeInstanceOf(UnauthorizedError)
      expect(err.reason).toBeUndefined()
    })

    it('shows an unrecognised code rather than blaming the credential', async () => {
      // The default cuts on whether a code is PRESENT, not on whether we know it. A
      // code means the server chose to say something specific, so show what it said.
      // Defaulting the other way would re-create §4.2 for the next 401 code added.
      const fn = vi.fn()
      const off = onUnauthorized(fn)
      stubFetch(json({ ok: false, error: { code: 'E_SOMETHING_NEW', message: 'Nope.' } }, 401))
      try {
        const err = (await api('/console').catch((e) => e)) as ApiError
        expect(err).toBeInstanceOf(ApiError)
        expect(err).not.toBeInstanceOf(UnauthorizedError)
        expect(err.message).toBe('Nope.')
        expect(fn).not.toHaveBeenCalled()
      } finally {
        off()
      }
    })

    it('falls back to a credential failure when there is no code at all', async () => {
      // A bodiless 401 or a gateway's HTML: "you are not authenticated" is the only
      // thing the bare status conveys, so the dialog is the right response.
      const fn = vi.fn()
      const off = onUnauthorized(fn)
      stubFetch(new Response(null, { status: 401 }))
      try {
        const err = (await api('/console').catch((e) => e)) as UnauthorizedError
        expect(err).toBeInstanceOf(UnauthorizedError)
        expect(fn).toHaveBeenCalledTimes(1)
      } finally {
        off()
      }
    })

    describe('a 401 the credential does not explain', () => {
      // `_actor_from` raises this on /approvals/{id}/approve and /reject under
      // RYA_REQUIRE_APPROVER_IDENTITY=1. The key is valid; the request needs an
      // X-Rya-User-Token as well. Opening the Connect dialog demanded the operator
      // re-paste a credential that was never the problem — and froze the shell's poll
      // while it was open, since usePoll is gated on `!authOpen`.
      const bankMode = () =>
        json(
          {
            ok: false,
            error: {
              code: 'E_APPROVER_IDENTITY_REQUIRED',
              message: 'This deployment requires a user identity to resolve approvals.',
              hint: 'POST /v1/token with your session, then send X-Rya-User-Token.',
              exit_code: 5,
            },
          },
          401,
        )

      it('does not raise the Connect dialog', async () => {
        const fn = vi.fn()
        const off = onUnauthorized(fn)
        stubFetch(bankMode())
        try {
          await api('/approvals/a1/approve', { method: 'POST' }).catch(() => {})
          expect(fn).not.toHaveBeenCalled()
        } finally {
          off()
        }
      })

      it('reaches the caller as an ApiError with the message and hint composed', async () => {
        stubFetch(bankMode())
        const err = (await api('/approvals/a1/approve', { method: 'POST' }).catch(
          (e) => e,
        )) as ApiError
        expect(err).toBeInstanceOf(ApiError)
        expect(err).not.toBeInstanceOf(UnauthorizedError)
        expect(err.code).toBe('E_APPROVER_IDENTITY_REQUIRED')
        expect(err.status).toBe(401)
        // Approvals.tsx toasts `e.message`; it used to read "Error — unauthorized".
        expect(err.message).toContain('requires a user identity')
        expect(err.message).toContain('X-Rya-User-Token')
        expect(err.hint).toBe('POST /v1/token with your session, then send X-Rya-User-Token.')
      })

      it('classifies E_BAD_SIGNATURE as not about the caller either', async () => {
        // The webhook HMAC, not this operator's identity.
        stubFetch(
          json(
            { ok: false, error: { code: 'E_BAD_SIGNATURE', message: 'Invalid webhook signature.' } },
            401,
          ),
        )
        const err = (await api('/events').catch((e) => e)) as ApiError
        expect(err).toBeInstanceOf(ApiError)
        expect(err).not.toBeInstanceOf(UnauthorizedError)
      })
    })
  })

  /**
   * ONE envelope, matching `tests/test_api.py::_assert_envelope`:
   *
   *     {"ok": false, "error": {"code": "E_*", "message", "hint", "exit_code"}}
   *
   * The server emits it for every failure — `RyaError`, `HTTPException`,
   * Starlette's own 404/405 and FastAPI's validation array all pass through the
   * three handlers in `api/app.py`. It used to be three different shapes and this
   * parser understood one of them, so most failures reached the operator as the
   * literal string `HTTP 400`.
   */
  describe('the error envelope', () => {
    const envelope = (error: Record<string, unknown>, status: number) =>
      json({ ok: false, error }, status)

    it('surfaces the message and the code', async () => {
      stubFetch(envelope({ code: 'E_VALIDATION', message: 'rule 3 has no pattern' }, 400))
      const err = (await api('/guard/policy', { method: 'PUT' }).catch((e) => e)) as ApiError

      expect(err).toBeInstanceOf(ApiError)
      expect(err.message).toBe('rule 3 has no pattern')
      expect(err.status).toBe(400)
      expect(err.code).toBe('E_VALIDATION')
    })

    /**
     * The hint is the whole DevEx point of the envelope — "tell the caller what to
     * do next" — and it used to be dropped for every shape, because `ApiError` had
     * no field to hold it. `Plane.sole_agent` names both candidate agents in its
     * hint and the operator saw none of it.
     */
    it('carries the hint, and composes it into the text a view will render', async () => {
      stubFetch(envelope({
        code: 'E_AGENT_AMBIGUOUS',
        message: "This deployment serves 2 agents, so '_' cannot say which one you mean.",
        hint: 'Address one explicitly: /agents/{billing|support}/…',
      }, 400))
      const err = (await api('/console').catch((e) => e)) as ApiError

      expect(err.hint).toBe('Address one explicitly: /agents/{billing|support}/…')
      // Every view toasts `e.message`; composing is what gets the hint on screen
      // without touching 23 views.
      expect(err.message).toContain('cannot say which one you mean')
      expect(err.message).toContain('/agents/{billing|support}/…')
    })

    it('leaves the message alone when there is no hint', async () => {
      stubFetch(envelope({ code: 'E_NOT_FOUND', message: 'Not Found', hint: null }, 404))
      const err = (await api('/nope').catch((e) => e)) as ApiError
      expect(err.message).toBe('Not Found')
      expect(err.hint).toBeUndefined()
      expect(err.code).toBe('E_NOT_FOUND')
    })

    it('carries a candidate list when a raise site adds one', async () => {
      // Extra keys ride along inside `error` (see `_http_error_handler`), so a
      // structured candidate list would survive without another envelope change.
      stubFetch(envelope(
        { code: 'E_AGENT_AMBIGUOUS', message: 'name the agent', candidates: ['a', 'b'] },
        400,
      ))
      const err = (await api('/console').catch((e) => e)) as ApiError
      expect(err.candidates).toEqual(['a', 'b'])
    })

    /**
     * The regression that made this worth doing: the OLD shapes must not silently
     * resolve to something plausible. They are now simply not the contract, and a
     * body that does not carry `error` gets the status line — never a wrong message
     * and never `[object Object]`, which is what FastAPI's validation array used to
     * render as.
     */
    it('does not read the retired shapes as if they were the envelope', async () => {
      stubFetch(json({ detail: [{ loc: ['query', 'limit'], msg: 'not an integer' }] }, 422))
      const arr = (await api('/x').catch((e) => e)) as ApiError
      expect(arr.message).toBe('HTTP 422')
      expect(arr.message).not.toContain('[object Object]')
    })

    /**
     * Not every 4xx/5xx a browser sees comes from Rya. An nginx 502, a CloudFront
     * error page or a corporate proxy answers with a shape of its own — the parser
     * must degrade, not crash or invent a code.
     */
    it('degrades gracefully for an intermediary that is not Rya', async () => {
      stubFetch(json({ message: 'upstream connect error' }, 502))
      const gw = (await api('/console').catch((e) => e)) as ApiError
      expect(gw.message).toBe('upstream connect error')
      expect(gw.code).toBeUndefined()

      stubFetch(json({ error: 'AccessDenied' }, 403)) // S3-style: `error` is a STRING
      const s3 = (await api('/console').catch((e) => e)) as ApiError
      expect(s3.message).toBe('HTTP 403')
    })

    it('keeps the status line when the error body is not JSON', async () => {
      // A proxy 502 or an nginx error page is HTML. Parsing must not turn a real
      // failure into an unhandled SyntaxError.
      stubFetch(new Response('<html>502 Bad Gateway</html>', { status: 502 }))
      const err = (await api('/console').catch((e) => e)) as ApiError
      expect(err).toBeInstanceOf(ApiError)
      expect(err.message).toBe('HTTP 502')
      expect(err.status).toBe(502)
    })

    it('keeps the status line when the body is JSON with no recognised shape', async () => {
      stubFetch(json({ oops: true }, 500))
      const err = (await api('/console').catch((e) => e)) as ApiError
      expect(err.message).toBe('HTTP 500')
    })
  })
})

/**
 * The 401 classifier, tested directly.
 *
 * It is exported because the Queue's SSE reader runs its own `fetch` and must reach
 * the same verdict — a second copy of this decision is precisely how §4.1's four-way
 * error parse came about.
 */
describe('unauthorizedError()', () => {
  it('is a credential failure for E_UNAUTHORIZED, whatever the message says', () => {
    // One code, six different `verify_jwt` messages plus two token checks. All of them
    // mean "the credential this browser holds is not accepted", so all of them belong
    // in the dialog — they differ only in what the operator does next, which is what
    // `reason` is for.
    for (const message of [
      'Missing or invalid operator token.',
      'Missing or invalid API key.',
      'JWT required.',
      'Malformed JWT.',
      'JWT is expired.',
      'JWT signature verification failed.',
    ]) {
      const e = unauthorizedError({ ok: false, error: { code: 'E_UNAUTHORIZED', message } })
      expect(e).toBeInstanceOf(UnauthorizedError)
      expect((e as UnauthorizedError).reason).toBe(message)
    }
  })

  it('is not a credential failure for any other code', () => {
    for (const code of ['E_APPROVER_IDENTITY_REQUIRED', 'E_BAD_SIGNATURE']) {
      const e = unauthorizedError({ ok: false, error: { code, message: 'no' } })
      expect(e).toBeInstanceOf(ApiError)
      expect(e).not.toBeInstanceOf(UnauthorizedError)
    }
  })

  it('falls back to a credential failure for a body it cannot read', () => {
    // No code to go on, so no basis for a more specific claim than "not authenticated".
    for (const body of [null, undefined, 'nope', {}, { error: {} }, { detail: 'Unauthorized' }]) {
      const e = unauthorizedError(body)
      expect(e).toBeInstanceOf(UnauthorizedError)
      expect((e as UnauthorizedError).code).toBeUndefined()
    }
  })
})

describe('token and session storage', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('round-trips the workspace key', () => {
    expect(getToken()).toBe('')
    setToken('wk_live_123')
    expect(getToken()).toBe('wk_live_123')
  })

  it('round-trips the account session and email, and keeps them separate from the key', () => {
    setToken('wk_live_123')
    saveSession('sess_abc', 'operator@example.com')

    expect(getSession()).toBe('sess_abc')
    expect(getEmail()).toBe('operator@example.com')
    // Three distinct credentials in three distinct keys: the session authenticates a
    // PERSON to the account API, the workspace key authenticates a DEPLOYMENT.
    expect(getToken()).toBe('wk_live_123')
  })

  it('leaves a previously saved email alone when saveSession omits one', () => {
    saveSession('sess_abc', 'operator@example.com')
    saveSession('sess_def')
    expect(getSession()).toBe('sess_def')
    expect(getEmail()).toBe('operator@example.com')
  })

  it('clearAuth drops the user token too, not just the key and session', () => {
    // Leaving it behind would keep sending a stale identity with the NEXT operator's
    // workspace key — a cross-attribution bug, and one that only shows up on a shared
    // machine after a sign-out.
    setToken('wk_live_123')
    saveSession('sess_abc', 'operator@example.com')
    stubFetch(json({ userToken: 'jwt.header.sig' }))
    return mintUserToken().then(() => {
      expect(getUserToken()).toBe('jwt.header.sig')
      clearAuth()
      expect(getUserToken()).toBe('')
      expect(getToken()).toBe('')
      expect(getSession()).toBeNull()
    })
  })
})

/**
 * §4.3: the console authenticates as a workspace. This is what makes it also
 * authenticate as a PERSON — the bridge from the account session to the short-lived
 * user JWT the data plane verifies.
 */
describe('the user identity bridge', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('exchanges the session for a user token and remembers it', async () => {
    saveSession('sess_abc', 'operator@example.com')
    const fn = stubFetch(json({ userToken: 'jwt.abc.sig', expiresInSeconds: 43200 }))

    await expect(mintUserToken()).resolves.toBe('jwt.abc.sig')

    expect(sentUrl(fn)).toBe('/v1/token')
    // Minted with the SESSION, not the workspace key: /v1/token is an account route.
    expect(sentHeaders(fn).get('authorization')).toBe('Bearer sess_abc')
    expect(getUserToken()).toBe('jwt.abc.sig')
  })

  it('does nothing at all without a session, and makes no request', async () => {
    // An operator who pasted an API key never signed in, so there is nothing to
    // exchange. This must be silent, not an error: that is a supported way in.
    const fn = stubFetch(json({}))
    await expect(mintUserToken()).resolves.toBe('')
    expect(fn).not.toHaveBeenCalled()
  })

  it('resolves empty when the runtime declines, and never throws', async () => {
    // Single-tenant: /v1/token calls `_require_mt()` and answers 400. The console has
    // to keep working — identity there comes from the operator's own bearer JWT.
    saveSession('sess_abc')
    stubFetch(
      json(
        { ok: false, error: { code: 'E_VALIDATION', message: 'Onboarding/accounts require multi-tenant mode.' } },
        400,
      ),
    )
    await expect(mintUserToken()).resolves.toBe('')
    expect(getUserToken()).toBe('')
  })

  it('sends the user token alongside the workspace key, never instead of it', async () => {
    setToken('wk_live_123')
    saveSession('sess_abc')
    stubFetch(json({ userToken: 'jwt.abc.sig' }))
    await mintUserToken()

    const fn = stubFetch(json({ ok: true }))
    await api('/console')

    const h = sentHeaders(fn)
    expect(h.get('authorization')).toBe('Bearer wk_live_123')
    expect(h.get('x-rya-user-token')).toBe('jwt.abc.sig')
  })

  it('sends no user-token header when there is none', async () => {
    setToken('wk_live_123')
    const fn = stubFetch(json({ ok: true }))
    await api('/console')
    // Absent, not empty: an empty header would be a malformed credential the server
    // has to reject, where an absent one is simply an unattributed request.
    expect(sentHeaders(fn).has('x-rya-user-token')).toBe(false)
  })

  it('re-mints once and retries when the user token has expired', async () => {
    // The JWT lasts 12 hours and the session outlives it, so this is what a console
    // left open overnight hits on its first poll. Before the retry the operator got a
    // Connect dialog for a workspace key that was perfectly good.
    setToken('wk_live_123')
    saveSession('sess_abc')
    stubFetch(json({ userToken: 'jwt.old.sig' }))
    await mintUserToken()

    const calls: string[] = []
    const fn = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      calls.push(url)
      if (url === '/v1/token') return Promise.resolve(json({ userToken: 'jwt.fresh.sig' }))
      const sent = new Headers(init?.headers).get('x-rya-user-token')
      if (sent === 'jwt.fresh.sig') return Promise.resolve(json({ ok: true, agent: null }))
      return Promise.resolve(
        json({ ok: false, error: { code: 'E_UNAUTHORIZED', message: 'JWT is expired.' } }, 401),
      )
    })
    vi.stubGlobal('fetch', fn)

    await expect(api('/console')).resolves.toEqual({ ok: true, agent: null })
    expect(calls).toEqual(['/console', '/v1/token', '/console'])
    expect(getUserToken()).toBe('jwt.fresh.sig')
  })

  it('gives up after ONE re-mint rather than looping on a genuine 401', async () => {
    // A bad workspace key also answers E_UNAUTHORIZED, and is indistinguishable from an
    // expired user token without string-matching the message. So the retry fires once,
    // costs one request, and then the 401 is reported normally.
    setToken('wk_live_dead')
    saveSession('sess_abc')
    stubFetch(json({ userToken: 'jwt.old.sig' }))
    await mintUserToken()

    const calls: string[] = []
    const fn = vi.fn((input: RequestInfo | URL) => {
      calls.push(String(input))
      if (String(input) === '/v1/token') return Promise.resolve(json({ userToken: 'jwt.new.sig' }))
      return Promise.resolve(
        json({ ok: false, error: { code: 'E_UNAUTHORIZED', message: 'Missing or invalid API key.' } }, 401),
      )
    })
    vi.stubGlobal('fetch', fn)

    const err = (await api('/console').catch((e) => e)) as UnauthorizedError
    expect(err).toBeInstanceOf(UnauthorizedError)
    expect(err.reason).toBe('Missing or invalid API key.')
    expect(calls).toEqual(['/console', '/v1/token', '/console'])
  })

  it('does not retry when no user token was sent', async () => {
    // Nothing to re-mint, so a 401 must reach the operator immediately rather than
    // costing a pointless round trip first.
    setToken('wk_live_dead')
    saveSession('sess_abc')
    const fn = stubFetch(json({ ok: false, error: { code: 'E_UNAUTHORIZED', message: 'nope' } }, 401))
    await api('/console').catch(() => {})
    expect(fn).toHaveBeenCalledTimes(1)
  })
})

describe('authPost()', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('posts JSON with no bearer, because it is the call that mints one', async () => {
    setToken('wk_live_123') // present, and deliberately not used
    const fn = stubFetch(json({ token: 'wk_live_new' }))

    await expect(authPost('/v1/signup', { email: 'a@b.c' })).resolves.toEqual({ token: 'wk_live_new' })

    const init = sentInit(fn)
    expect(init.method).toBe('POST')
    expect(init.body).toBe('{"email":"a@b.c"}')
    expect(sentHeaders(fn).has('authorization')).toBe(false)
  })

  it('rejects with the server message and does not fan out a 401', async () => {
    // Signup/login answer 401 on bad credentials. Raising the Connect dialog from
    // inside the Connect dialog is not a useful thing to do, so this path is
    // deliberately outside `api()`'s handler set.
    const seen = vi.fn()
    const off = onUnauthorized(seen)
    stubFetch(json({ ok: false, error: { message: 'that email is already registered' } }, 409))

    try {
      await expect(authPost('/v1/signup', {})).rejects.toThrow('that email is already registered')
      expect(seen).not.toHaveBeenCalled()
    } finally {
      off()
    }
  })

  it('falls back to the status line when the failure body is unparseable', async () => {
    stubFetch(new Response('nope', { status: 500 }))
    await expect(authPost('/v1/login', {})).rejects.toThrow('HTTP 500')
  })
})

describe('sessionPost()', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('refuses to call at all when there is no session', async () => {
    setToken('wk_live_123')
    const fn = stubFetch(json({}))
    // The workspace key is present but is the wrong credential for an account-scoped
    // route, so this must not silently fall back to it.
    await expect(sessionPost('/v1/workspaces', {})).rejects.toThrow('Not signed in.')
    expect(fn).not.toHaveBeenCalled()
  })

  it('sends the session token, never the workspace key', async () => {
    setToken('wk_live_123')
    saveSession('sess_abc')
    const fn = stubFetch(json({ id: 'ws_1' }))

    await sessionPost('/v1/workspaces', { name: 'prod' })

    expect(sentHeaders(fn).get('authorization')).toBe('Bearer sess_abc')
    expect(sentInit(fn).body).toBe('{"name":"prod"}')
  })

  it('sends an empty object rather than "undefined" when given no body', async () => {
    saveSession('sess_abc')
    const fn = stubFetch(json({}))
    await sessionPost('/v1/workspaces/ws_1/rotate', undefined)
    expect(sentInit(fn).body).toBe('{}')
  })
})

describe('the API base', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.resetModules()
    history.replaceState({}, '', '/')
  })

  it('is empty by default, so every call is same-origin', async () => {
    vi.resetModules()
    const mod = await import('./api')
    expect(mod.API).toBe('')
  })

  it('honours ?api= for `npm run dev` against a separately-running server', async () => {
    history.replaceState({}, '', '/?api=http://127.0.0.1:8787')
    vi.resetModules()
    const mod = await import('./api')
    const fn = stubFetch(json({}))

    expect(mod.API).toBe('http://127.0.0.1:8787')
    await mod.api('/console')
    expect(sentUrl(fn)).toBe('http://127.0.0.1:8787/console')
  })

  it('strips a trailing slash so the joined path has exactly one', async () => {
    history.replaceState({}, '', '/?api=http://127.0.0.1:8787/')
    vi.resetModules()
    const mod = await import('./api')
    expect(mod.API).toBe('http://127.0.0.1:8787')
  })

  /**
   * The credential never leaves the origin.
   *
   * `API` is prefixed onto every request in this file and onto `Team.tsx` and the
   * Queue's SSE reader, all of which attach `Authorization: Bearer …`. Read from the
   * query string unconditionally, `?api=` made
   * `https://console.example.com/?api=https://evil.tld` a one-click handover of a
   * key that can approve actions, rewrite the Action Guard and flip kill switches.
   */
  it('ignores an off-origin ?api= and does NOT send the bearer token to it', async () => {
    history.replaceState({}, '', '/?api=https://evil.tld')
    vi.resetModules()
    const mod = await import('./api')
    mod.setToken('rya_sk_secret')
    const fn = stubFetch(json({}))

    expect(mod.API).toBe('')
    await mod.api('/console')

    // Same-origin, and the key went with it — to us, not to them.
    expect(sentUrl(fn)).toBe('/console')
    expect(sentUrl(fn)).not.toContain('evil.tld')
    expect(sentHeaders(fn).get('Authorization')).toBe('Bearer rya_sk_secret')
  })

  it('rejects the spellings that dodge a naive scheme check', async () => {
    const { devApiBase } = await import('./api')
    const origin = 'https://console.example.com'
    // Protocol-relative: no `scheme:` to match on, still an off-origin destination.
    expect(devApiBase('?api=//evil.tld', origin)).toBe('')
    expect(devApiBase('?api=https://evil.tld', origin)).toBe('')
    expect(devApiBase('?api=https://console.example.com.evil.tld', origin)).toBe('')
    // A loopback-looking hostname that is not loopback.
    expect(devApiBase('?api=http://127.0.0.1.evil.tld', origin)).toBe('')
    expect(devApiBase('?api=javascript:alert(1)', origin)).toBe('')
    // Userinfo pointing the real host somewhere else.
    expect(devApiBase('?api=https://127.0.0.1@evil.tld', origin)).toBe('')
  })

  it('still allows the two bases that cannot leak: same-origin and loopback', async () => {
    const { devApiBase } = await import('./api')
    const origin = 'https://console.example.com'
    expect(devApiBase('?api=/api/v2', origin)).toBe('/api/v2')
    expect(devApiBase('?api=https://console.example.com/base', origin)).toBe(
      'https://console.example.com/base',
    )
    expect(devApiBase('?api=http://localhost:8000', origin)).toBe('http://localhost:8000')
    expect(devApiBase('?api=http://127.0.0.1:8000', origin)).toBe('http://127.0.0.1:8000')
    expect(devApiBase('?api=http://[::1]:8000', origin)).toBe('http://[::1]:8000')
    expect(devApiBase('', origin)).toBe('')
  })

  /**
   * The override is a DEV affordance, so it does not exist in the artefact that gets
   * deployed. Vite replaces `import.meta.env.DEV` with the literal `false`, the
   * ternary folds and `devApiBase` is dropped — verified against a real build: the
   * bundle contains no `URLSearchParams` at all.
   */
  it('is inert in a production build, even for a loopback base', async () => {
    vi.stubEnv('DEV', false)
    history.replaceState({}, '', '/?api=http://127.0.0.1:8787')
    vi.resetModules()
    const mod = await import('./api')
    expect(mod.API).toBe('')
    vi.unstubAllEnvs()
  })
})
