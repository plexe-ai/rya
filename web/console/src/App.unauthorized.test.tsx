import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import App from './App'
import type { ConsoleState } from './lib/types'

/**
 * §4.2, end to end: what the operator is actually told when the server answers 401.
 *
 * The console used to derive two conclusions from one fact. The fact was "the status
 * is 401"; the conclusions were *your credential is stale* and *re-pasting it is the
 * fix*. The server means at least five different things by 401, and for some of them
 * both conclusions are false — so the remedy offered was the wrong one and the
 * server's own diagnosis, which it sent, was discarded unread.
 *
 * These tests pin the two halves of the fix at the level the operator experiences it:
 * a credential 401 must SAY WHY, and a non-credential 401 must not blame the
 * credential at all.
 *
 * Its own file rather than a case in `App.test.tsx`: both tests need `fetch` to fail
 * on the shell's own `/console` poll, which every other App test needs to succeed.
 */

const APPROVAL_STATE: ConsoleState = {
  agent: {
    name: 'support-agent',
    version: '3',
    environment: 'dev',
    status: 'ready',
    runtime: 'python',
    handlers: { event: true },
  },
  runtime: { store: 'file', llmProvider: 'anthropic', multiTenant: false },
  stats: {
    runs: 1,
    byStatus: { paused: 1 },
    approvalsPending: 1,
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
  approvals: [
    {
      id: 'apr_1',
      title: 'Send refund confirmation',
      runId: 'run_1',
      action: { tool: 'send_email', input: { to: 'customer@example.com' } },
    },
  ],
  memory: { blocks: [], facts: 0, collections: [] },
  secrets: [],
  triggers: [],
  viewer: { workspace: 'default', mode: 'single-tenant', user: 'operator' },
}

const json = (body: unknown, status = 200) =>
  Promise.resolve(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    }),
  )

beforeEach(() => {
  localStorage.setItem('rya_token', 'test-token')
  location.hash = ''
})
afterEach(() => {
  localStorage.clear()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('a 401 the credential explains', () => {
  it('opens the Connect dialog AND says what the server said', async () => {
    // `get_plane` skips `_check_token` entirely when `RYA_JWT_SECRET` is set, so an
    // operator token stops authenticating anything and every request answers this.
    // The dialog's static copy says "This runtime requires an operator token" and its
    // placeholder offers `rya_sk_… or operator token` — both name a credential that
    // cannot work here. Without the reason the operator has nothing to go on.
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        if (String(input).includes('/v1/info')) return json({ multiTenant: false })
        return json(
          {
            ok: false,
            error: {
              code: 'E_UNAUTHORIZED',
              message: 'JWT required.',
              hint: "Send 'Authorization: Bearer <jwt>'.",
              exit_code: 5,
            },
          },
          401,
        )
      }),
    )

    render(<App />)

    await waitFor(() => expect(screen.getByText(/JWT required\./)).toBeTruthy())
    // The dialog is open, and the reason sits inside it.
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(screen.getByText(/Send 'Authorization: Bearer <jwt>'\./)).toBeTruthy()
  })

  it('shows no reason at all for a bodiless 401, rather than "HTTP 401"', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        if (String(input).includes('/v1/info')) return json({ multiTenant: false })
        return Promise.resolve(new Response(null, { status: 401 }))
      }),
    )

    render(<App />)

    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    // A status line is not an explanation. The dialog falls back to its own copy.
    expect(screen.queryByText(/HTTP 401/)).toBeNull()
    expect(screen.getByText(/requires an operator token/)).toBeTruthy()
  })
})

describe('a 401 the credential does not explain', () => {
  it('leaves the dialog shut and reports what is actually wrong', async () => {
    // Bank mode: RYA_REQUIRE_APPROVER_IDENTITY=1. `_actor_from` raises this on
    // /approvals/{id}/approve only. The workspace key is valid — the request needs an
    // X-Rya-User-Token as well, which nothing in the console mints yet (§4.3). The old
    // behaviour threw the Connect dialog over the operator's work, froze the shell's
    // poll while it was open (usePoll is gated on `!authOpen`), and toasted the single
    // word "unauthorized" for a failure the server had described precisely.
    const approve = vi.fn(() =>
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
      ),
    )
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input)
        if (url.includes('/approve')) return approve()
        if (url.includes('/console')) return json(APPROVAL_STATE)
        if (url.includes('/queue/stats')) return json({ counts: {} })
        return json({})
      }),
    )

    // Deep-linked rather than clicked through: "Approvals" is both a nav item and an
    // Overview card, so the accessible name is ambiguous.
    location.hash = 'approvals'
    render(<App />)
    await waitFor(() => expect(screen.getAllByText('support-agent').length).toBeGreaterThan(0))

    fireEvent.click(await screen.findByRole('button', { name: /^Approve$/ }))

    // The toast carries the server's diagnosis and its remedy...
    await waitFor(() => expect(screen.getByText(/requires a user identity/)).toBeTruthy())
    expect(screen.getByText(/X-Rya-User-Token/)).toBeTruthy()
    // ...and the operator is NOT asked to re-paste a key that was never the problem.
    expect(screen.queryByRole('dialog')).toBeNull()
    // The approvals list is still on screen, not buried under a modal.
    expect(screen.getByText('Send refund confirmation')).toBeTruthy()
  })
})
