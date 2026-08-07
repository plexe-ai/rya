import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import App from './App'
import type { ConsoleState } from './lib/types'

/**
 * The Refresh button — audit §5.6.
 *
 * `onRefresh` called `refresh()` on the shell's `/console` poll and nothing else, so
 * on every view that owns a fetch the most prominent control in the console did
 * nothing whatsoever: Environments, Versions, Workers, Quotas, Evals, Guard,
 * Knowledge, and — since §5.1 and §5.2 moved them off the aggregate — Runs and
 * Conversations. Nine of twenty-three, and the nine most operationally live ones.
 *
 * It is tested here, through the real shell and a real view, rather than only at the
 * hook (`lib/usePoll.test.tsx` does that). The hook test proves the mechanism; this
 * proves it is actually WIRED — that the provider is above the views, that the button
 * bumps the signal, and that a view acquires the behaviour with no code of its own.
 * A green hook and an unwired provider is exactly the shape of the original bug.
 *
 * Quotas is the vehicle because `/quotas` has one consumer and the shell never calls
 * it, so counting requests to it is unambiguous.
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

let calls: string[]

/** How many times a path prefix has been requested since the render. */
const hits = (prefix: string) => calls.filter((u) => u.startsWith(prefix)).length

beforeEach(() => {
  calls = []
  localStorage.setItem('rya_token', 'test-token')
  localStorage.setItem('rya_agent', AGENT)
  location.hash = 'quotas'
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      calls.push(url)
      const json = (body: unknown) =>
        Promise.resolve(
          new Response(JSON.stringify(body), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          }),
        )
      if (url.startsWith('/console')) return json(STATE)
      if (url.startsWith('/quotas')) return json({ quota: { enforced: false }, usage: {} })
      if (url.startsWith('/usage')) return json({ usage: {} })
      if (url.startsWith('/posture')) return json({ untrusted: false, ok: true, conditions: [] })
      if (url.startsWith('/queue/stats')) return json({ counts: { pending: 0 } })
      if (url.includes('/environments')) return json({ environments: [] })
      if (url.includes('/versions')) return json({ versions: [] })
      if (url.startsWith('/workers')) return json({ workers: [] })
      return json({})
    }),
  )
})
afterEach(() => {
  localStorage.clear()
  location.hash = ''
  vi.unstubAllGlobals()
})

describe('Refresh reaches a view that owns its fetch (§5.6)', () => {
  it('re-reads the view’s own endpoint, not just the aggregate', async () => {
    render(<App />)
    await waitFor(() => expect(hits('/quotas')).toBe(1))
    const consoleBefore = hits('/console')

    fireEvent.click(screen.getByRole('button', { name: /Refresh/ }))

    // The finding: this used to stay at 1 forever.
    await waitFor(() => expect(hits('/quotas')).toBe(2))
    // And the aggregate still refreshes — exactly once. The shell publishes the
    // signal rather than subscribing to it, so a second code path here would mean
    // two `/console` requests per press.
    expect(hits('/console')).toBe(consoleBefore + 1)
  })

  it('re-reads the sidebar’s deploy counts, which are on a 30s timer of their own', async () => {
    render(<App />)
    await waitFor(() => expect(hits('/workers')).toBe(1))

    fireEvent.click(screen.getByRole('button', { name: /Refresh/ }))
    // Thirty seconds is a long time to keep showing a number an operator has just
    // asked to have checked.
    await waitFor(() => expect(hits('/workers')).toBe(2))
  })

  it('does not re-read anything until it is pressed', async () => {
    // The counterpart the fix must not break: `useLoad` is "on entry", and turning
    // it into a poller by accident is the mistake §5.5 just finished undoing.
    render(<App />)
    await waitFor(() => expect(hits('/quotas')).toBe(1))
    await new Promise((r) => setTimeout(r, 60))
    expect(hits('/quotas')).toBe(1)
  })
})
