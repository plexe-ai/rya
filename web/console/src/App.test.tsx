import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import App from './App'
import type { ConsoleState } from './lib/types'

// A minimal-but-complete `/console` aggregate: every field the shell and the
// ported views read, at the values a fresh install would actually have.
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
    runs: 2,
    byStatus: { completed: 1, waiting_approval: 1 },
    approvalsPending: 1,
    inputTokens: 1200,
    outputTokens: 340,
    costUsd: 0.0042,
    jobsPending: 0,
    sessions: 1,
    messages: 4,
  },
  tools: [{ id: 'email.send', permission: 'approval_required' }],
  models: [{ id: 'claude-opus-5', type: 'anthropic', permission: 'allowed', calls: 2 }],
  channels: [{ type: 'email', path: '/inbound', enabled: true }],
  runs: [
    { id: 'run_one', status: 'completed', trigger: 'message.received', tokens: 800, createdAt: '2026-08-01T11:00:00Z' },
    { id: 'run_two', status: 'waiting_approval', trigger: 'message.received', tokens: 740, createdAt: '2026-08-01T11:30:00Z' },
  ],
  approvals: [
    { id: 'ap_1', title: 'Issue refund', runId: 'run_two', action: { tool: 'refund.issue', input: { to: 'ada@x.com' } } },
  ],
  memory: { blocks: [], facts: 0, collections: [] },
  secrets: ['ANTHROPIC_API_KEY'],
  triggers: [],
  viewer: { workspace: 'default', mode: 'single-tenant', user: 'operator' },
}

function mockFetch() {
  return vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    const json = (body: unknown) =>
      Promise.resolve(new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }))
    if (url.includes('/console')) return json(STATE)
    if (url.includes('/queue/stats')) return json({ counts: { pending: 0, running: 0, failed: 0 } })
    if (url.includes('/v1/info')) return json({ multiTenant: false })
    return json({})
  })
}

describe('App shell', () => {
  beforeEach(() => {
    localStorage.setItem('rya_token', 'test-token') // skip the auth gate
    location.hash = ''
    vi.stubGlobal('fetch', mockFetch())
  })
  afterEach(() => {
    localStorage.clear()
    vi.unstubAllGlobals()
  })

  it('mounts, loads state, and renders the agent identity', async () => {
    render(<App />)
    // Appears in the topbar heading and the sidebar agent picker.
    await waitFor(() => expect(screen.getAllByText('support-agent').length).toBeGreaterThan(0))
    expect(screen.getByText('live')).toBeTruthy()
    expect(screen.getByText('env: dev')).toBeTruthy()
    expect(screen.getByText('store: file')).toBeTruthy()
  })

  it('renders the overview tiles from the aggregate', async () => {
    render(<App />)
    await waitFor(() => expect(screen.getByText('Runs')).toBeTruthy())
    expect(screen.getByText('1 completed')).toBeTruthy()
    expect(screen.getByText('$0.0042')).toBeTruthy() // sub-dollar cost keeps 4dp
    expect(screen.getByText('1,540')).toBeTruthy() // input + output tokens
  })

  it('shows an amber approvals count in the sidebar when one is pending', async () => {
    render(<App />)
    await waitFor(() => expect(screen.getByText('Approvals', { selector: 'button *, button' })).toBeTruthy())
    const badge = document.querySelector('.nav .ct.amber')
    expect(badge?.textContent).toBe('1')
  })

  it('navigates to a ported view and reflects it in the URL hash', async () => {
    render(<App />)
    await waitFor(() => expect(screen.getAllByText('support-agent').length).toBeGreaterThan(0))

    fireEvent.click(screen.getByRole('button', { name: /Runs & traces/ }))

    await waitFor(() => expect(screen.getByLabelText('Filter runs')).toBeTruthy())
    expect(location.hash).toBe('#runs')
    expect(screen.getByText('run_one')).toBeTruthy()
    expect(screen.getByText('run_two')).toBeTruthy()
  })

  it('deep-links straight into a view from the hash on first load', async () => {
    location.hash = '#approvals'
    render(<App />)
    await waitFor(() => expect(screen.getByText('Issue refund')).toBeTruthy())
    expect(screen.getByRole('button', { name: /Approve/ })).toBeTruthy()
  })

  it('tells the operator where to find a view that is not ported yet', async () => {
    location.hash = '#workers'
    render(<App />)
    await waitFor(() => expect(screen.getByText(/Not migrated to this console yet/)).toBeTruthy())
    // The link must point at the legacy console, not nowhere.
    expect(screen.getByRole('link', { name: /current console/ }).getAttribute('href')).toBe('/#workers')
  })

  it('keeps the last good data and flips to offline when a poll fails', async () => {
    render(<App />)
    await waitFor(() => expect(screen.getByText('live')).toBeTruthy())

    // Every subsequent request fails, as if the runtime went away.
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('connection refused'))))
    fireEvent.click(screen.getByRole('button', { name: /Refresh/ }))

    await waitFor(() => expect(screen.getByText('offline')).toBeTruthy())
    // Stale-but-useful beats a blank dashboard: the data is still on screen.
    expect(screen.getAllByText('support-agent').length).toBeGreaterThan(0)
    expect(screen.getByText('1 completed')).toBeTruthy()
  })

  it('opens the auth gate when there is no token', async () => {
    localStorage.clear()
    render(<App />)
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    expect(screen.getByText('Welcome to Rya')).toBeTruthy()
    // Single-tenant: only the API-key tab is offered.
    expect(screen.getByRole('button', { name: 'API key' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Sign up' })).toBeNull()
  })
})
