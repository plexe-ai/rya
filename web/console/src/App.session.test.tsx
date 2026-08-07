import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import App from './App'
import type { ConsoleState } from './lib/types'

/**
 * Sign-out — audit §5.11.
 *
 * Signing out removed four keys from localStorage, which is the credentials and
 * nothing the credentials had fetched. The previous tenant's agent name, runs,
 * secrets, traces and queue depths stayed mounted behind the dialog — readable by
 * pressing Escape, and readable *by whoever signed in next*, because a shared or
 * handed-over browser is the whole reason a console has a sign-out button. And
 * `rya_agent` survived to be sent as the next tenant's first request.
 *
 * The fix models a session as a component lifetime, so these tests are written
 * against the observable consequence rather than against the mechanism: after signing
 * out, nothing the previous tenant could see is on the page, and nothing they selected
 * is in storage. A test that asserted "the epoch incremented" would pass a
 * reimplementation that leaked.
 */

const json = (body: unknown) =>
  Promise.resolve(
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    }),
  )

function stateFor(agent: string, secret: string): ConsoleState {
  return {
    agent: {
      name: agent,
      version: '1',
      environment: 'dev',
      status: 'ready',
      runtime: 'python',
      handlers: null,
    },
    agents: [{ name: agent }],
    runtime: { store: 'file', llmProvider: 'anthropic', multiTenant: true },
    stats: {
      runs: 1,
      byStatus: { completed: 1 },
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
    secrets: [secret],
    triggers: [],
    viewer: { workspace: agent === 'acme-agent' ? 'acme' : 'globex', mode: 'multi-tenant', user: 'operator' },
  } as unknown as ConsoleState
}

/** Whose console the runtime is currently serving. Flipped to model a new sign-in. */
let tenant = stateFor('acme-agent', 'ACME_STRIPE_KEY')

beforeEach(() => {
  tenant = stateFor('acme-agent', 'ACME_STRIPE_KEY')
  localStorage.setItem('rya_token', 'acme-key')
  localStorage.setItem('rya_session', 'acme-session')
  localStorage.setItem('rya_email', 'ada@acme.test')
  localStorage.setItem('rya_user_token', 'acme-jwt')
  localStorage.setItem('rya_agent', 'acme-agent')
  location.hash = 'secrets'
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/v1/info')) return json({ multiTenant: true, authRequired: true })
      // Data endpoints honour the credential, because this runtime is multi-tenant and
      // a stub that serves tenant data to an unauthenticated caller would let a leak
      // test pass for the wrong reason — the console would look clean while merely
      // being handed the same workspace again.
      if (!localStorage.getItem('rya_token')) {
        return Promise.resolve(
          new Response(JSON.stringify({ ok: false, error: { code: 'E_UNAUTHORIZED', message: 'Token required.' } }), {
            status: 401,
            headers: { 'content-type': 'application/json' },
          }),
        )
      }
      if (url.startsWith('/console')) return json(tenant)
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

/** Sign in with a workspace key. Multi-tenant, so the key tab has to be chosen. */
function connectWith(key: string) {
  fireEvent.click(screen.getByRole('button', { name: 'API key' }))
  fireEvent.change(screen.getByLabelText('API key'), { target: { value: key } })
  // Exact, not `/Connect/`: the sidebar's "Connections" nav button is still mounted
  // behind the dialog and a loose match finds both.
  fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
}

/** Mount, wait for the first tenant's data, then sign out. */
async function signOut() {
  render(<App />)
  await waitFor(() => expect(screen.getByText('ACME_STRIPE_KEY')).toBeTruthy())
  fireEvent.click(screen.getByRole('button', { name: /Sign out/i }))
  await waitFor(() => expect(dialog()).toBeTruthy())
}

describe('signing out ends the session (§5.11)', () => {
  it('takes the previous tenant’s data off the page', async () => {
    await signOut()
    // Not "hidden behind the dialog" — gone. The dialog is dismissible (§5.12 added a
    // visible Close, and Escape always worked), so anything merely covered by it is
    // one keystroke from being read by the next person at this browser.
    expect(screen.queryByText('ACME_STRIPE_KEY')).toBeNull()
    expect(screen.queryByText('acme-agent')).toBeNull()
  })

  it('survives dismissing the dialog', async () => {
    await signOut()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(dialog()).toBeNull())

    // The exact walk-up the finding describes: sign out, press the one control the
    // dialog offers, read the workspace you were signed out of.
    expect(screen.queryByText('ACME_STRIPE_KEY')).toBeNull()
    expect(screen.queryByText('acme-agent')).toBeNull()
  })

  it('forgets the remembered agent', async () => {
    await signOut()
    // `rya_agent` is not a credential, which is why `clearAuth` never touched it. It is
    // the PREVIOUS TENANT'S agent name, and the next sign-in put it straight into
    // `/console?agent=…` — asking a workspace that has never heard of it for an agent
    // it does not serve.
    expect(localStorage.getItem('rya_agent')).toBeNull()
  })

  it('forgets every credential', async () => {
    // `clearAuth()` already did this before §5.11, so this passes either way. Kept as a
    // guard on the FIX: sign-out now also remounts the tree, and a reordering that put
    // the remount before the storage writes would hand the new instance the old
    // session's token — the new code's most plausible way to go wrong.
    await signOut()
    for (const k of ['rya_token', 'rya_session', 'rya_email', 'rya_user_token']) {
      expect(localStorage.getItem(k)).toBeNull()
    }
  })

  it('does not carry the old agent into the next tenant’s first request', async () => {
    await signOut()
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>
    fetchMock.mockClear()

    // A different tenant signs in at the same browser.
    tenant = stateFor('globex-agent', 'GLOBEX_SMTP_PASS')
    connectWith('globex-key')

    await waitFor(() => expect(screen.getByText('GLOBEX_SMTP_PASS')).toBeTruthy())

    // Every `/console` read of the new session is unqualified or names the new agent.
    // A stale `?agent=acme-agent` would come back `E_AGENT_NOT_FOUND` and land the new
    // operator on a toast about an agent they have never heard of.
    const consoleCalls = fetchMock.mock.calls.map((c) => String(c[0])).filter((u) => u.startsWith('/console'))
    expect(consoleCalls.length).toBeGreaterThan(0)
    expect(consoleCalls.some((u) => u.includes('acme-agent'))).toBe(false)
  })

  it('shows the new tenant’s data and only the new tenant’s data', async () => {
    // The happy path, and green against the unfixed code — the incoming poll overwrote
    // the outgoing tenant's data all by itself, which is exactly why the leak was easy
    // to miss. Kept because discarding a whole component tree is a blunt instrument:
    // this is what fails if the remount leaves the console unable to come back up.
    await signOut()
    tenant = stateFor('globex-agent', 'GLOBEX_SMTP_PASS')
    connectWith('globex-key')

    await waitFor(() => expect(screen.getByText('GLOBEX_SMTP_PASS')).toBeTruthy())
    expect(screen.queryByText('ACME_STRIPE_KEY')).toBeNull()
    expect(screen.queryByText('acme-agent')).toBeNull()
  })
})
