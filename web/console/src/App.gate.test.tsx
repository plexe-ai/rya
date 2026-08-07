import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import App from './App'
import type { ConsoleState } from './lib/types'

/**
 * The auth gate — audit §5.12.
 *
 * The console used to decide whether a credential was needed by looking in its own
 * localStorage. That is a question about the BROWSER standing in for a question about
 * the RUNTIME, and the two disagree on the most common way to run Rya: a plain
 * `rya serve` with no `RYA_TOKEN`, where `auth_enabled()` is false and every request
 * would have been answered. The first thing that deployment showed a new operator was
 * a full-page modal demanding a token the server neither wants nor checks, whose only
 * exit was an Escape key nothing mentioned.
 *
 * `/v1/info.authRequired` has answered this all along and was fetched and discarded.
 * These tests pin down that the answer now decides, and — just as important — that it
 * only ever decides in the safe direction: an unreachable or silent discovery endpoint
 * still gets you the dialog, because "I could not ask" is not "the door is open".
 */

const AGENT = 'support-agent'

const STATE = {
  agent: {
    name: AGENT,
    version: '1',
    environment: 'dev',
    status: 'ready',
    runtime: 'python',
    handlers: null,
  },
  agents: [{ name: AGENT }],
  runtime: { store: 'file', llmProvider: 'anthropic', multiTenant: false },
  stats: {
    runs: 0,
    byStatus: {},
    approvalsPending: 0,
    inputTokens: 0,
    outputTokens: 0,
    costUsd: 0,
    jobsPending: 0,
    sessions: 0,
    messages: 0,
  },
  tools: [],
  models: [],
  channels: [],
  runs: [],
  approvals: [],
  memory: { blocks: [], facts: 0, collections: [] },
  secrets: [],
  triggers: [],
  viewer: { workspace: 'default', mode: 'single-tenant', user: 'operator' },
} as unknown as ConsoleState

/** What `GET /v1/info` answers, per test. `null` = the request fails outright. */
let info: Record<string, unknown> | null = { multiTenant: false, authRequired: true }

const json = (body: unknown) =>
  Promise.resolve(
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    }),
  )

beforeEach(() => {
  location.hash = 'overview'
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/v1/info')) {
        return info === null ? Promise.reject(new Error('connection refused')) : json(info)
      }
      if (url.startsWith('/console')) return json(STATE)
      if (url.startsWith('/queue/stats')) return json({ counts: { pending: 0 } })
      return json({})
    }),
  )
})
afterEach(() => {
  localStorage.clear()
  location.hash = ''
  vi.unstubAllGlobals()
})

const dialog = () => screen.queryByRole('dialog')
const calls = () => (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.map((c) => String(c[0]))

describe('the auth gate asks the runtime (§5.12)', () => {
  it('does not open on a runtime that requires no credential', async () => {
    info = { multiTenant: false, authRequired: false }
    render(<App />)

    // The console loads. This is the whole finding: a default `rya serve` should be
    // usable, and it was not.
    await waitFor(() => expect(screen.getAllByText(AGENT).length).toBeGreaterThan(0))
    expect(dialog()).toBeNull()
  })

  it('opens on a runtime that does require one', async () => {
    info = { multiTenant: false, authRequired: true }
    render(<App />)

    await waitFor(() => expect(dialog()).toBeTruthy())
    // And nothing was read behind it. A console that has not decided whether it may
    // talk to the runtime must not talk to the runtime — a doomed `/console` is one
    // request per page load spent proving something `/v1/info` already said.
    expect(calls().some((u) => u.startsWith('/console'))).toBe(false)
  })

  it('opens when the runtime cannot be asked at all', async () => {
    // Passes against the unfixed code too, deliberately — the OLD console always
    // opened, so it could not fail this. That is the point: this and the test below are
    // the guards that the fix only ever loosens the gate on an explicit answer, and
    // they are what would go red if someone later "simplified" `{}` into a default of
    // `authRequired: false`.
    //
    // The safe direction, and the one that keeps this change from being a security
    // regression: an unreachable discovery endpoint is not evidence of an open door.
    info = null
    render(<App />)
    await waitFor(() => expect(dialog()).toBeTruthy())
  })

  it('opens when the runtime answers without saying', async () => {
    // An older runtime, or something that is not Rya at all answering 200 on that
    // path. `authRequired` absent must not read as `false`.
    info = { multiTenant: false }
    render(<App />)
    await waitFor(() => expect(dialog()).toBeTruthy())
  })

  it('never asks when the browser already holds a token', async () => {
    localStorage.setItem('rya_token', 'test-token')
    localStorage.setItem('rya_agent', AGENT)
    info = { multiTenant: false, authRequired: true }
    render(<App />)

    await waitFor(() => expect(screen.getAllByText(AGENT).length).toBeGreaterThan(0))
    // Also green against the unfixed code, and kept for the same reason: it pins the
    // COST of the fix at zero for the common case, and fails if the probe is ever made
    // unconditional.
    //
    // A credential in hand settles the gate synchronously, so the probe is skipped and
    // the first paint costs no round trip. `authRequired: true` above is deliberate:
    // holding a token must not be turned into a reason to interrogate the runtime.
    expect(dialog()).toBeNull()
    expect(calls().some((u) => u.includes('/v1/info'))).toBe(false)
  })

  it('is one request, shared with the dialog', async () => {
    // The shell and the modal both need `/v1/info` and must not be able to disagree
    // about it — see `runtimeInfo()`. Also the reason the gate costs one RTT and not
    // two on a tokenless boot.
    info = { multiTenant: false, authRequired: true }
    render(<App />)
    await waitFor(() => expect(dialog()).toBeTruthy())
    // The modal renders its API-key-only tab set from the same answer.
    await waitFor(() => expect(screen.getByLabelText('API key')).toBeTruthy())
    expect(calls().filter((u) => u.includes('/v1/info')).length).toBe(1)
  })

  it('can be dismissed with a visible control, not only with Escape', async () => {
    info = { multiTenant: false, authRequired: true }
    render(<App />)
    await waitFor(() => expect(dialog()).toBeTruthy())

    // A modal whose only exit is an undocumented keystroke is a modal with no exit.
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(dialog()).toBeNull()

    // Closing releases the poll, so let it land inside the test rather than after it.
    // The stub answers `/console` without checking a credential, deliberately: what is
    // behind the dialog on a runtime that really does require one is the pre-existing
    // 401 path, and re-testing it here would obscure the one thing this test is for.
    await waitFor(() => expect(screen.getAllByText(AGENT).length).toBeGreaterThan(0))
  })

  it('says what it is asking for when the runtime wants nothing', async () => {
    // Reachable through the workspace button on an open runtime, where the static copy
    // ("This runtime requires an operator token") is the §5.12 misstatement arriving
    // through the other door.
    info = { multiTenant: false, authRequired: false }
    render(<App />)
    await waitFor(() => expect(screen.getAllByText(AGENT).length).toBeGreaterThan(0))

    fireEvent.click(screen.getByRole('button', { name: 'Workspace' }))
    const d = await waitFor(() => {
      const el = dialog()
      expect(el).toBeTruthy()
      return el!
    })
    expect(d.textContent).toMatch(/accepts requests without a credential/i)
    expect(d.textContent).not.toMatch(/requires an operator token/i)
  })
})
