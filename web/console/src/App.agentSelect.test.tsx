import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import App from './App'
import type { ConsoleState } from './lib/types'

/**
 * The agent selector — audit §5.14.
 *
 * The `<select>` was bound to `loaded.agent.name`: the server's ECHO, not the
 * operator's choice. So choosing an agent moved the control, React re-rendered it from
 * the echo, and it snapped back to the previous name until a full round trip finished.
 * A control that undoes the interaction you just had with it reads as broken.
 *
 * The worse half is what happens when that request does not finish. The selection is
 * real — it is in localStorage and it is what `ag()` prefixes onto every agent-scoped
 * request in the console — so a failed switch left the selector naming agent A while
 * every request went to agent B, with nothing on screen admitting the difference.
 *
 * Binding the control to the choice fixes the snap-back and opens a second way to
 * lie: the control now names an agent whose data is not on the page. So the pair is
 * tested together — the control follows the click, AND the gap between the click and
 * the data is stated out loud for as long as it exists.
 */

const json = (body: unknown) =>
  Promise.resolve(
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    }),
  )

const ROSTER = [{ name: 'support-agent' }, { name: 'billing-agent' }]

function stateFor(agent: string): ConsoleState {
  return {
    agent: {
      name: agent,
      version: agent === 'support-agent' ? '1' : '7',
      environment: agent === 'support-agent' ? 'dev' : 'prod',
      status: 'ready',
      runtime: 'python',
      handlers: null,
    },
    agents: ROSTER,
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
}

/** Which agents `/console` will answer for. Removing one models a failing switch. */
let served = new Set(['support-agent', 'billing-agent'])
/** Set to fail every `/console` read, as if the runtime went away mid-switch. */
let down = false

beforeEach(() => {
  served = new Set(['support-agent', 'billing-agent'])
  down = false
  localStorage.setItem('rya_token', 'test-token')
  localStorage.setItem('rya_agent', 'support-agent')
  location.hash = 'overview'
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.startsWith('/console')) {
        if (down) return Promise.reject(new Error('connection refused'))
        const asked = new URL(url, 'http://x').searchParams.get('agent')
        // Deliberately NOT `E_AGENT_NOT_FOUND`: that code has its own recovery path in
        // `load()` which drops the selection, and this test is about the selection
        // being KEPT. This is the ordinary case — the switch request simply failed.
        if (asked && !served.has(asked)) return Promise.reject(new Error('boom'))
        return json(stateFor(asked ?? 'support-agent'))
      }
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

const picker = () => screen.getByLabelText('Agent') as HTMLSelectElement

/** Mount and wait for the first agent's data to land. */
async function mounted() {
  render(<App />)
  await waitFor(() => expect(screen.getByText('v1 · dev')).toBeTruthy())
}

describe('the agent selector follows the operator (§5.14)', () => {
  it('holds the new name immediately, without waiting for the runtime', async () => {
    // Freeze every `/console` read so the assertion lands inside the window the bug
    // lived in: after the click, before any response. The control used to show the OLD
    // name for the whole of this window.
    await mounted()
    down = true
    fireEvent.change(picker(), { target: { value: 'billing-agent' } })

    // Synchronous, before any `await`: this is exactly the window the bug lived in.
    expect(picker().value).toBe('billing-agent')

    // Then settle the switch this test started, so the failure lands inside the test
    // rather than after it. A state update that arrives once the body has returned is
    // React's `act` warning, and it leaves the next test starting mid-request.
    await waitFor(() => expect(screen.getByText('offline')).toBeTruthy())
  })

  it('keeps the new name when the switch fails', async () => {
    await mounted()
    down = true
    fireEvent.change(picker(), { target: { value: 'billing-agent' } })
    await waitFor(() => expect(screen.getByText('offline')).toBeTruthy())

    // The selection is real: it is in storage and `ag()` is prefixing it onto every
    // agent-scoped request. Reverting the control would be the console disagreeing with
    // itself about which agent this page is addressed to.
    expect(picker().value).toBe('billing-agent')
    expect(localStorage.getItem('rya_agent')).toBe('billing-agent')
  })

  it('says which agent is actually on screen while the two disagree', async () => {
    await mounted()
    down = true
    fireEvent.change(picker(), { target: { value: 'billing-agent' } })

    // Not silence, and not `v1 · dev` — that line describes the DATA, so under the name
    // `billing-agent` it is a sentence about `support-agent` with the wrong subject.
    await waitFor(() => expect(screen.getByText(/still showing support-agent/)).toBeTruthy())
    expect(screen.queryByText('v1 · dev')).toBeNull()
  })

  it('stops saying it the moment the data catches up', async () => {
    await mounted()
    fireEvent.change(picker(), { target: { value: 'billing-agent' } })

    await waitFor(() => expect(screen.getByText('v7 · prod')).toBeTruthy())
    expect(picker().value).toBe('billing-agent')
    expect(screen.queryByText(/still showing/)).toBeNull()
  })

  it('does not cry mismatch on a first load', async () => {
    // `selected` is read from localStorage synchronously and `showing` arrives a round
    // trip later, so the two differ on every single boot. That is not a disagreement,
    // and treating it as one would put a warning on a healthy console every time.
    render(<App />)
    expect(screen.queryByText(/still showing/)).toBeNull()
    await waitFor(() => expect(screen.getByText('v1 · dev')).toBeTruthy())
    expect(screen.queryByText(/still showing/)).toBeNull()
  })

  it('reports a mismatch as loading, not as broken, while the runtime is healthy', async () => {
    // Same disagreement, different cause, and the operator's next move differs: one
    // resolves itself, the other needs looking into. `switchFailed` is what separates
    // them, so a 200ms switch does not flash a warning colour.
    await mounted()
    served.delete('billing-agent')
    fireEvent.change(picker(), { target: { value: 'billing-agent' } })

    expect(screen.getByText('loading billing-agent…')).toBeTruthy()
    // …and it becomes the louder wording once the read has actually failed.
    await waitFor(() => expect(screen.getByText(/still showing support-agent/)).toBeTruthy())
  })
})
