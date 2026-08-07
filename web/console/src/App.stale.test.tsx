import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import App from './App'
import type { ConsoleState } from './lib/types'

/**
 * Stale data — audit §5.9.
 *
 * `usePoll` keeps the last good value when a poll fails, deliberately: blanking a
 * dashboard over a blip is worse than showing something slightly old. But the ONLY
 * notice of a failure was a 2.6-second toast fired on the leading edge and never
 * repeated, so a runtime that died and stayed dead was masked indefinitely. An
 * operator who looked away at the wrong moment — or who opened the tab afterwards —
 * saw a complete dashboard, every number frozen, nothing on screen saying so.
 *
 * The fix has to hold two lines at once, and both are tested here: **loud when the
 * data is old**, and **silent on a blip**, because a banner that fires on every
 * hiccup is one operators learn to scroll past.
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
    runs: 3,
    byStatus: { completed: 3 },
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

/** Flip to make every request fail, as if the runtime went away. */
let down = false

const banner = () => screen.queryByRole('alert')

beforeEach(() => {
  down = false
  localStorage.setItem('rya_token', 'test-token')
  localStorage.setItem('rya_agent', AGENT)
  location.hash = 'overview'
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      if (down) return Promise.reject(new Error('connection refused'))
      const url = String(input)
      const json = (body: unknown) =>
        Promise.resolve(
          new Response(JSON.stringify(body), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          }),
        )
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

/** Render, wait for the first good poll, then take the runtime away. */
async function goLive() {
  render(<App />)
  await waitFor(() => expect(screen.getByText('live')).toBeTruthy())
  down = true
}

const pressRefresh = () => fireEvent.click(screen.getByRole('button', { name: /Refresh/ }))

describe('a runtime that stops answering (§5.9)', () => {
  it('says nothing extra about a single missed poll', async () => {
    await goLive()
    pressRefresh()
    await waitFor(() => expect(screen.getByText('offline')).toBeTruthy())

    // A blip is a blip. The pill and the existing one-shot toast are the whole of
    // the response; a banner here would be the thing operators stop reading.
    expect(banner()).toBeNull()
  })

  it('raises a permanent banner once the silence is no longer a blip', async () => {
    await goLive()
    pressRefresh()
    await waitFor(() => expect(screen.getByText('offline')).toBeTruthy())
    pressRefresh()

    const b = await waitFor(() => {
      const el = banner()
      expect(el).toBeTruthy()
      return el!
    })
    // It says how old, not merely that something is wrong: "offline" is equally
    // true of a runtime that blinked two seconds ago and one that died last night,
    // and the operator's next move is different for each.
    expect(b.textContent).toMatch(/Showing data from .+ ago/)
    // And in the runtime's own words, so the operator knows where to look.
    expect(b.textContent).toContain('connection refused')

    // Stale-but-useful is still on screen — the banner qualifies the dashboard, it
    // does not replace it. Blanking a view an operator is reading is the failure
    // mode `usePoll` keeps the last good value to avoid.
    expect(screen.getAllByText(AGENT).length).toBeGreaterThan(0)
  })

  it('keeps the banner up when the operator’s Retry also fails', async () => {
    await goLive()
    pressRefresh()
    // Awaited between presses: a second click landing while the first request is
    // still out SUPERSEDES it (§5.8), and a superseded request is not a failure.
    await waitFor(() => expect(screen.getByText('offline')).toBeTruthy())
    pressRefresh()
    await waitFor(() => expect(banner()).toBeTruthy())

    const before = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    // Waited out by the request the click made, so the assertion lands after the
    // retry has actually failed rather than before it was tried.
    await waitFor(() =>
      expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(
        before,
      ),
    )

    // The console must not answer "it's fine now" to a click that proved the
    // opposite. This is why the failure count and the backoff counter are separate:
    // a Retry re-times the loop, but it does not un-know a failure.
    expect(banner()).toBeTruthy()
  })

  it('clears the moment the runtime answers again', async () => {
    await goLive()
    pressRefresh()
    await waitFor(() => expect(screen.getByText('offline')).toBeTruthy())
    pressRefresh()
    await waitFor(() => expect(banner()).toBeTruthy())

    down = false
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(banner()).toBeNull())
    expect(screen.getByText('live')).toBeTruthy()
  })

  it('shows the age of the data beside the offline pill', async () => {
    await goLive()
    pressRefresh()
    await waitFor(() => expect(screen.getByText('offline')).toBeTruthy())
    // A duration, in the pill, from the first failure — not only once the banner
    // threshold is crossed. `offline` alone was the whole of §5.9's complaint.
    await waitFor(() => expect(screen.getByText(/^· \d+[smhd] old$/)).toBeTruthy())
  })

  it('does not raise a staleness banner when nothing ever loaded', async () => {
    // There is no age to report, so there is nothing for this banner to say. The
    // shell paints its runtime-down card instead, which offers the right actions
    // (retry, enter a token) rather than qualifying data that does not exist.
    down = true
    render(<App />)
    await waitFor(() => expect(screen.getByText(/reach the runtime/i)).toBeTruthy())
    expect(banner()).toBeNull()
  })
})
