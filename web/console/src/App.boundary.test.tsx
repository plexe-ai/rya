import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import App from './App'
import type { ConsoleState } from './lib/types'

/**
 * The failure this boundary exists for, end to end.
 *
 * `Guard` is replaced with a component that throws on render — standing in for the
 * class of bug the audit's screenshot shows, `Cannot read properties of null
 * (reading 'name')`. Without a boundary React 19 unmounts the entire tree: no
 * sidebar, no nav, no recovery, and a reload reproduces it because the hash still
 * points at the broken view. These two tests are the proof that it no longer can.
 *
 * Its own file because `vi.mock` is hoisted to module scope and would break the
 * Guard view for every other App test.
 */
vi.mock('./views/Guard', () => ({
  GuardView: () => {
    throw new Error("Cannot read properties of null (reading 'name')")
  },
}))

const STATE: ConsoleState = {
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
  secrets: [],
  triggers: [],
  viewer: { workspace: 'default', mode: 'single-tenant', user: 'operator' },
}

beforeEach(() => {
  localStorage.setItem('rya_token', 'test-token')
  location.hash = ''
  // React logs the caught error itself; silenced so a deliberate throw does not
  // bury a real failure in the suite output.
  vi.spyOn(console, 'error').mockImplementation(() => {})
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      const json = (body: unknown) =>
        Promise.resolve(
          new Response(JSON.stringify(body), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          }),
        )
      if (url.includes('/console')) return json(STATE)
      if (url.includes('/queue/stats')) return json({ counts: {} })
      return json({})
    }),
  )
})
afterEach(() => {
  localStorage.clear()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('a view that throws during render', () => {
  it('keeps the shell mounted and lets the operator navigate away', async () => {
    render(<App />)
    await waitFor(() => expect(screen.getAllByText('support-agent').length).toBeGreaterThan(0))

    fireEvent.click(screen.getByRole('button', { name: /Action Guard/ }))

    // Contained: the fallback replaced the view...
    expect(screen.getByText('This view failed to render.')).toBeTruthy()
    expect(screen.getByText("Cannot read properties of null (reading 'name')")).toBeTruthy()
    // ...and the sidebar, the top bar and the agent identity are all still there,
    // which is the difference between a broken page and a broken console.
    expect(screen.getByRole('button', { name: /Runs & traces/ })).toBeTruthy()
    expect(screen.getAllByText('support-agent').length).toBeGreaterThan(0)

    // Navigating away clears it without a reload.
    fireEvent.click(screen.getByRole('button', { name: /Runs & traces/ }))
    expect(screen.queryByText('This view failed to render.')).toBeNull()

    // ...and going back re-enters the broken view rather than latching clear.
    fireEvent.click(screen.getByRole('button', { name: /Action Guard/ }))
    expect(screen.getByText('This view failed to render.')).toBeTruthy()
  })

  it('recovers via Back to overview, so the hash stops pointing at the throw', async () => {
    location.hash = 'guard'
    render(<App />)
    await waitFor(() => expect(screen.getByText('This view failed to render.')).toBeTruthy())

    // Deep-linking straight into a broken view is the reload case: before the
    // boundary this was a blank page that reproduced on every refresh.
    fireEvent.click(screen.getByRole('button', { name: /back to overview/i }))

    expect(screen.queryByText('This view failed to render.')).toBeNull()
    expect(location.hash).toBe('#overview')
  })
})
