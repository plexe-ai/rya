import { describe, expect, it, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { EvalsView } from './Evals'
import type { ConsoleState } from '../lib/types'

const state = { agent: { name: 'acme' } } as unknown as ConsoleState

interface Call {
  url: string
  method: string
}

/** Stubbed at the `fetch` seam so the URL and method are part of what is asserted. */
function stubFetch(reply: (call: Call) => { status?: number; body?: unknown }) {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      const call: Call = { url: String(url), method: (init?.method ?? 'GET').toUpperCase() }
      calls.push(call)
      const { status = 200, body = {} } = reply(call)
      return Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) } as Response)
    }),
  )
  return calls
}

const CASES = {
  agent: 'acme',
  exists: true,
  cases: [
    {
      id: 'greets_new_lead',
      trigger: { type: 'message.received', payload: { body: 'hi' } },
      expect: { contains: 'hello', tool_called: 'send_email' },
    },
    { id: 'refuses_refund', trigger: { type: 'message.received' }, expect: { not_contains: 'refund' } },
  ],
}

const RUN = {
  ok: false,
  total: 2,
  passed: 1,
  failed: 1,
  score: 0.5,
  results: [
    {
      id: 'greets_new_lead',
      pass: true,
      runId: 'run_11',
      status: 'completed',
      checks: [{ check: 'contains', pass: true, detail: 'found "hello"' }],
    },
    {
      id: 'refuses_refund',
      pass: false,
      runId: 'run_12',
      status: 'failed',
      checks: [{ check: 'not_contains', pass: false, detail: 'said "refund" anyway' }],
      error: null,
    },
  ],
}

afterEach(() => vi.unstubAllGlobals())

describe('EvalsView', () => {
  it('loads declared cases on entry from the agent-prefixed route', async () => {
    const calls = stubFetch(() => ({ body: CASES }))
    render(<EvalsView state={state} onToast={() => {}} />)

    expect(await screen.findByText('greets_new_lead')).toBeTruthy()
    expect(calls).toEqual([{ url: '/agents/acme/evals', method: 'GET' }])
    expect(screen.getByText('2 declared')).toBeTruthy()
    // Expectations render as one chip per scorer.
    expect(screen.getByText('contains')).toBeTruthy()
    expect(screen.getByText('tool_called')).toBeTruthy()
    expect(screen.getByText('refuses_refund')).toBeTruthy()
  })

  it('triggers a run with POST and surfaces the per-case checks', async () => {
    const calls = stubFetch((c) => (c.method === 'POST' ? { body: RUN } : { body: CASES }))
    const toast = vi.fn()
    render(<EvalsView state={state} onToast={toast} />)
    await screen.findByText('greets_new_lead')

    fireEvent.click(screen.getByRole('button', { name: /run evals/i }))

    expect(await screen.findByText('found "hello"', { exact: false })).toBeTruthy()
    expect(screen.getByText('said "refund" anyway', { exact: false })).toBeTruthy()
    expect(screen.getByText('pass')).toBeTruthy()
    expect(screen.getByText('fail')).toBeTruthy()
    // The run's own status and run id come through, so a failure is traceable.
    expect(screen.getByText('run_11')).toBeTruthy()
    expect(screen.getByText('completed')).toBeTruthy()

    expect(calls).toEqual([
      { url: '/agents/acme/evals', method: 'GET' },
      { url: '/agents/acme/evals/run', method: 'POST' },
    ])
    await waitFor(() => expect(toast).toHaveBeenCalledWith('1 eval(s) failed'))
  })

  it('toasts the all-passed case and keeps the declared list visible', async () => {
    stubFetch((c) =>
      c.method === 'POST'
        ? { body: { ok: true, total: 1, passed: 1, failed: 0, score: 1, results: [{ id: 'greets_new_lead', pass: true, checks: [] }] } }
        : { body: CASES },
    )
    const toast = vi.fn()
    render(<EvalsView state={state} onToast={toast} />)
    await screen.findByText('greets_new_lead')

    fireEvent.click(screen.getByRole('button', { name: /run evals/i }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith('All evals passed (1/1)'))
    // The result does not replace the case list: "1/1 passed" out of a suite of two
    // needs its denominator on screen.
    expect(screen.getByText('2 declared')).toBeTruthy()
  })

  /**
   * A missing suite is a warning, not an error and not silence: evals are the only
   * behavioural evidence a promotion gate can require, so "no suite" has to be
   * legible as a gap.
   */
  it('names a missing rya.evals.yaml rather than showing an empty page', async () => {
    stubFetch(() => ({ body: { agent: 'acme', cases: [], exists: false } }))
    render(<EvalsView state={state} onToast={() => {}} />)
    expect(await screen.findByText(/No rya.evals.yaml/)).toBeTruthy()
  })

  it('repeats the server note when the project is not mounted here', async () => {
    stubFetch(() => ({
      body: {
        agent: 'acme',
        cases: [],
        exists: false,
        note: "Eval cases live in the project tree; this deployment does not have 'acme' mounted.",
      },
    }))
    render(<EvalsView state={state} onToast={() => {}} />)
    expect(await screen.findByText(/does not have 'acme' mounted/)).toBeTruthy()
  })

  it('distinguishes a declared-but-empty suite from a missing one', async () => {
    stubFetch(() => ({ body: { agent: 'acme', cases: [], exists: true } }))
    render(<EvalsView state={state} onToast={() => {}} />)
    expect(await screen.findByText('No cases.')).toBeTruthy()
    expect(screen.getByText('0 declared')).toBeTruthy()
  })

  it('asks for a token on a 401 instead of claiming an outage', async () => {
    stubFetch(() => ({ status: 401, body: {} }))
    render(<EvalsView state={state} onToast={() => {}} />)
    expect(await screen.findByText('Connect with an operator token to load evals.')).toBeTruthy()
  })

  /**
   * `POST /evals/run` 409s with `E_NO_INLINE_WORKER` when the api process cannot
   * import the agent (D21). The server's message names the fix, so it is surfaced
   * verbatim rather than flattened into "failed".
   */
  it('surfaces the server message when this process cannot run the suite', async () => {
    stubFetch((c) =>
      c.method === 'POST'
        ? {
            status: 409,
            body: { detail: { code: 'E_NO_INLINE_WORKER', message: "This api process cannot execute 'acme'." } },
          }
        : { body: CASES },
    )
    const toast = vi.fn()
    render(<EvalsView state={state} onToast={toast} />)
    await screen.findByText('greets_new_lead')

    fireEvent.click(screen.getByRole('button', { name: /run evals/i }))
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith("Eval run failed — This api process cannot execute 'acme'."),
    )
    // The button comes back: the operator has to be able to retry after fixing it.
    expect((screen.getByRole('button', { name: /run evals/i }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('renders case ids as text, so a hostile id cannot inject markup', async () => {
    const nasty = '<img src=x onerror=alert(1)>'
    stubFetch(() => ({ body: { exists: true, cases: [{ id: nasty, expect: {} }] } }))
    render(<EvalsView state={state} onToast={() => {}} />)
    expect(await screen.findByText(nasty)).toBeTruthy()
    expect(document.querySelector('img')).toBeNull()
  })
})
