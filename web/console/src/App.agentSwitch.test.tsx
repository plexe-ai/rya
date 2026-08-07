import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import App from './App'
import type { ConsoleState } from './lib/types'

/**
 * Audit §5.3 — view-local detail state must not survive an agent switch.
 *
 * The views are rendered by one long ternary in `App.tsx`, and they used to be
 * unkeyed: React therefore preserved each view's component state across an agent
 * change, so the trace, thread, turn, environment, version and eval-result panels
 * all kept the PREVIOUS agent's data under the new agent's header. In Guard it was
 * not merely confusing — the draft survived too, so Save wrote agent A's policy
 * into agent B.
 *
 * `Runs` is replaced with a component that owns a piece of detail state, standing
 * in for all seven panels: this is a property of the shell, not of any one view, and
 * pinning it through a real view would re-pin it to that view's internals. The
 * second test is the one that matters most — the fix must reset on an agent switch
 * and NOT on a poll, because the shell hands every view a brand-new `state` object
 * every 6 seconds and a key that noticed would blow away an open panel while the
 * operator was reading it.
 *
 * Its own file because `vi.mock` is hoisted to module scope and would replace the
 * Runs view for every other App test.
 */
vi.mock('./views/Runs', () => ({
  RunsView: ({ state }: { state: ConsoleState }) => {
    const [open, setOpen] = useState<string | null>(null)
    return (
      <div>
        <button onClick={() => setOpen(`trace of ${state.agent.name}`)}>Open trace</button>
        {open && <div>{open}</div>}
      </div>
    )
  },
}))

const ROSTER = [{ name: 'alpha' }, { name: 'beta' }]

const stateFor = (name: string): ConsoleState =>
  ({
    agent: {
      name,
      version: '1',
      environment: 'dev',
      status: 'ready',
      runtime: 'python',
      handlers: { event: true },
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
  }) as unknown as ConsoleState

beforeEach(() => {
  localStorage.setItem('rya_token', 'test-token')
  localStorage.setItem('rya_agent', 'alpha')
  location.hash = 'runs'
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
      if (url.includes('/console')) {
        // The response echoes the addressed agent, which is what the key tracks:
        // the agent the data on screen BELONGS to, not the pending selection.
        const addressed = /[?&]agent=([^&]+)/.exec(url)?.[1]
        return json(stateFor(addressed ? decodeURIComponent(addressed) : 'alpha'))
      }
      if (url.includes('/queue/stats')) return json({ counts: {} })
      return json({})
    }),
  )
})
afterEach(() => {
  localStorage.clear()
  location.hash = ''
  vi.unstubAllGlobals()
})

describe('switching agent', () => {
  it('clears the previous agent’s open detail panel', async () => {
    render(<App />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Open trace' })).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: 'Open trace' }))
    expect(screen.getByText('trace of alpha')).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Agent'), { target: { value: 'beta' } })

    // The panel is gone rather than relabelled: it held alpha's trace, and there is
    // no such thing as "the same panel, for another agent".
    await waitFor(() => expect(screen.queryByText('trace of alpha')).toBeNull())
    expect(screen.getByRole('button', { name: 'Open trace' })).toBeTruthy()

    // And the new agent's own panel opens with the new agent's data.
    fireEvent.click(screen.getByRole('button', { name: 'Open trace' }))
    expect(screen.getByText('trace of beta')).toBeTruthy()
  })

  it('does not clear it on a poll of the same agent', async () => {
    render(<App />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Open trace' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Open trace' }))
    expect(screen.getByText('trace of alpha')).toBeTruthy()

    // Refresh delivers a fresh, structurally-identical `state` OBJECT — exactly what
    // the 6s poll does. A key derived from the response rather than from the agent's
    // name would remount here, and an operator reading a trace would watch it vanish
    // every six seconds.
    fireEvent.click(screen.getByRole('button', { name: /Refresh/ }))
    await waitFor(() =>
      expect(
        (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.filter((c) =>
          String(c[0]).includes('/console'),
        ).length,
      ).toBeGreaterThan(1),
    )

    expect(screen.getByText('trace of alpha')).toBeTruthy()
  })
})
