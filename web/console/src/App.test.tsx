import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import App from './App'
import { ALL_VIEWS } from './lib/nav'
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

  it('renders a real component for EVERY view in the nav', async () => {
    // The port's completion condition, as a test. While it was in flight the fallback
    // arm rendered "not migrated to this console yet" — so a view nobody had wired up
    // looked like a deliberate state instead of an omission. That component is gone
    // and the chain now ends in a `never`-typed branch, which makes a missing wire a
    // build error. This is the runtime half: mount every nav id and assert none of
    // them lands on the unknown-view fallback or throws.
    for (const id of ALL_VIEWS) {
      location.hash = `#${id}`
      const { unmount } = render(<App />)
      // Every view paints its own header; waiting on the shell being live is enough to
      // know the branch resolved rather than crashed the tree.
      await waitFor(() => expect(screen.getAllByText('support-agent').length).toBeGreaterThan(0))
      expect(screen.queryByText(/Unknown view/)).toBeNull()
      expect(screen.queryByText(/Not migrated/)).toBeNull()
      unmount()
    }
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

/**
 * `GET /console` returns `agent: null` in two ordinary situations — a workspace with
 * nothing published, and a workspace serving several agents with none selected. The
 * route documents this ("a fresh workspace with nothing published yet still has a
 * dashboard"); the console used to type `agent` as non-nullable and dereference it in
 * `render`, so both cases threw `Cannot read properties of null (reading 'name')` and
 * took the agent picker — the only control that resolves the second case — down with
 * the page. These pin the states so that cannot come back.
 */
describe('App shell — no agent selected', () => {
  const ROSTER = [{ name: 'admissions-agent' }, { name: 'retention-agent' }]

  function mockRoster(agents: { name: string }[]) {
    return vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      const json = (body: unknown, status = 200) =>
        Promise.resolve(new Response(JSON.stringify(body), {
          status,
          headers: { 'content-type': 'application/json' },
        }))
      if (url.includes('/console')) {
        const m = url.match(/[?&]agent=([^&]+)/)
        if (m?.[1]) {
          const want = decodeURIComponent(m[1])
          if (!agents.some((a) => a.name === want))
            return json({ detail: { code: 'E_AGENT_NOT_FOUND', message: `No agent named '${want}' is served here.` } }, 404)
          return json({ ...STATE, agent: { ...STATE.agent, name: want }, agents, selectedAgent: want })
        }
        // The server only auto-selects when the workspace serves exactly one agent.
        const sole = agents[0]
        if (agents.length === 1 && sole)
          return json({ ...STATE, agent: { ...STATE.agent, name: sole.name }, agents, selectedAgent: sole.name })
        return json({ ok: true, agent: null, agents, selectedAgent: null, viewer: STATE.viewer })
      }
      if (url.includes('/queue/stats')) return json({ counts: {} })
      return json({})
    })
  }

  beforeEach(() => {
    localStorage.setItem('rya_token', 'test-token')
    location.hash = ''
  })
  afterEach(() => {
    localStorage.clear()
    vi.unstubAllGlobals()
  })

  it('offers a choice instead of crashing when a workspace serves several agents', async () => {
    vi.stubGlobal('fetch', mockRoster(ROSTER))
    render(<App />)
    await waitFor(() => expect(screen.getByText(/serves 2 agents/)).toBeTruthy())
    // The picker must be reachable — that was the deadlock: it rendered AFTER the throw.
    expect(screen.getByRole('combobox', { name: 'Agent' })).toBeTruthy()
    expect(screen.getByRole('button', { name: /admissions-agent/ })).toBeTruthy()
  })

  it('selecting an agent loads it and re-requests with ?agent=', async () => {
    const fetchMock = mockRoster(ROSTER)
    vi.stubGlobal('fetch', fetchMock)
    render(<App />)
    await waitFor(() => expect(screen.getByText(/serves 2 agents/)).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: /retention-agent/ }))

    // Wait on the REQUEST, not on the name appearing: 'retention-agent' is already on
    // screen as an <option> in the picker, so asserting the text would pass before
    // anything had been selected.
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([u]) => String(u).includes('/console?agent=retention-agent'))).toBe(true))
    // The chooser gives way to the agent's own dashboard.
    await waitFor(() => expect(screen.queryByText(/serves 2 agents/)).toBeNull())
    // And it is remembered, so a reload lands on the same agent.
    expect(localStorage.getItem('rya_agent')).toBe('retention-agent')
  })

  it('tells a fresh workspace what to do rather than showing an error', async () => {
    vi.stubGlobal('fetch', mockRoster([]))
    render(<App />)
    await waitFor(() => expect(screen.getByText('Nothing published yet')).toBeTruthy())
    expect(screen.getByText(/rya publish --env prod/)).toBeTruthy()
    // Not an outage: the poll succeeded.
    expect(screen.queryByText(/Can't reach the runtime/)).toBeNull()
  })

  it('drops a remembered agent the workspace does not serve, instead of reporting an outage', async () => {
    localStorage.setItem('rya_agent', 'gone-agent')
    vi.stubGlobal('fetch', mockRoster(ROSTER))
    render(<App />)
    await waitFor(() => expect(screen.getByText(/serves 2 agents/)).toBeTruthy())
    expect(localStorage.getItem('rya_agent')).toBeNull()
    expect(screen.queryByText(/Can't reach the runtime/)).toBeNull()
  })

  it('adopts the sole agent of a single-agent workspace without asking', async () => {
    vi.stubGlobal('fetch', mockRoster([{ name: 'admissions-agent' }]))
    render(<App />)
    await waitFor(() => expect(screen.getAllByText('admissions-agent').length).toBeGreaterThan(0))
    // One agent is not a choice, so no selector is offered.
    expect(screen.queryByRole('combobox', { name: 'Agent' })).toBeNull()
    await waitFor(() => expect(localStorage.getItem('rya_agent')).toBe('admissions-agent'))
  })

  it('addresses the test event to the selected agent, never the "_" alias', async () => {
    const fetchMock = mockRoster([{ name: 'admissions-agent' }])
    vi.stubGlobal('fetch', fetchMock)
    render(<App />)
    // Wait for the selection to SETTLE, not just for the name to appear: `ag()` falls
    // back to `_` until the sole agent has actually been adopted, so clicking early
    // would exercise the very path this asserts against.
    await waitFor(() => expect(localStorage.getItem('rya_agent')).toBe('admissions-agent'))

    fireEvent.click(screen.getByRole('button', { name: /Send test event/ }))
    // The unprefixed spelling resolves `_` server-side and 400s once a workspace has
    // two agents, so the prefix is the whole point.
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([u]) => String(u).includes('/agents/admissions-agent/events'))).toBe(true))
    expect(fetchMock.mock.calls.some(([u]) => /\/agents\/_\/events/.test(String(u)))).toBe(false)
  })

  it('reports "not introspected" when the control plane never imported the bundle', async () => {
    // snapshot.py sends `handlers: null` whenever it holds no loaded agent module,
    // which since D21 is every published bundle. Typing it non-nullable made the
    // Overview throw on `handlers.event` for a perfectly healthy agent.
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      const json = (body: unknown) =>
        Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } }))
      if (url.includes('/console'))
        return json({ ...STATE, agent: { ...STATE.agent, handlers: null }, agents: [{ name: STATE.agent.name }] })
      if (url.includes('/queue/stats')) return json({ counts: {} })
      return json({})
    }))
    render(<App />)
    await waitFor(() => expect(screen.getByText('handlers not introspected')).toBeTruthy())
    expect(screen.queryByText('no handler')).toBeNull()
  })
})
