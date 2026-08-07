import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { RunsView } from './Runs'
import type { RunRow } from './Runs'
import type { ConsoleState, Run, Stats } from '../lib/types'

// `lib/api` is mocked by path rather than `fetch` being stubbed, because this view
// now talks to TWO endpoints with different scoping rules — the agent-prefixed paged
// list and the workspace-scoped `/runs/{id}/trace` — and the mock receives the path
// each was called with, which is the thing worth asserting. `vi.hoisted` is what lets
// the spy be typed: `vi.mock`'s factory is hoisted above every `const`, so a plain
// module-level `vi.fn()` would still be undefined when the factory runs.
const { apiMock } = vi.hoisted(() => ({
  apiMock: vi.fn<(path: string, opts?: RequestInit) => Promise<unknown>>(),
}))
vi.mock('../lib/api', () => ({ api: apiMock }))

const AGENT = 'support-agent'
const LIST = `/agents/${AGENT}/runs`

/**
 * 412 runs, newest first — the workspace audit §5.1 is written about.
 *
 * The 12 failures are the OLDEST runs on purpose, so they are all beyond the first
 * page. That is the shape of the real complaint: the operator sees "failed · 12",
 * and the only way that number can be honoured is if the request that answers the
 * pill is made against the server rather than against the rows already on screen.
 */
const CORPUS: RunRow[] = Array.from({ length: 412 }, (_, i) => ({
  id: `run_${String(412 - i).padStart(4, '0')}`,
  status: i >= 400 ? 'failed' : 'completed',
  trigger: i % 2 ? 'cron.nightly' : 'message.received',
  tokens: 100,
  createdAt: '2026-08-01T11:00:00Z',
}))

const STATS: Stats = {
  runs: 412,
  byStatus: { completed: 400, failed: 12 },
  approvalsPending: 0,
  inputTokens: 0,
  outputTokens: 0,
  jobsPending: 0,
}

/**
 * The 30-row preview `/console` ships in its `runs` key (`snapshot.py`, `runs[:30]`).
 *
 * Deliberately given ids that cannot occur in the endpoint's answer: this view used
 * to render, filter, search and COUNT this array, so a fixture that overlapped with
 * the real rows would let the old behaviour pass. Audit §9 is about fixtures that
 * encoded the bug instead of catching it; this one is built to fail loudly if a row
 * ever comes from the aggregate again.
 */
const PREVIEW: Run[] = Array.from({ length: 30 }, (_, i) => ({
  id: `preview_only_${i}`,
  status: 'completed',
  trigger: 'message.received',
  tokens: 1,
  createdAt: '2026-08-01T11:00:00Z',
}))

/** Only the fields RunsView reads; the rest of the aggregate is irrelevant here. */
const stateWith = (stats: Partial<Stats> = {}, runs: Run[] = PREVIEW) =>
  ({ agent: { name: AGENT }, stats: { ...STATS, ...stats }, runs }) as unknown as ConsoleState

interface Recorded {
  path: string
  params: URLSearchParams
}

/**
 * A stand-in for `GET /agents/{a}/runs?summary=1&limit&offset[&status][&q]`,
 * implementing the contract rather than replaying a canned page: `status` is an
 * equality match, `q` a case-insensitive substring of the id or the trigger, `count`
 * the size of the FILTERED set, and the page a slice. Paging and search are only
 * meaningfully testable against something that actually pages and searches.
 */
function serve(corpus: RunRow[] = CORPUS, opts: { fail?: Error; clamp?: number } = {}) {
  const list: Recorded[] = []
  apiMock.mockImplementation((path: string) => {
    if (path.endsWith('/trace')) {
      return Promise.resolve({ trace: [{ kind: 'run.started', label: 'event', ts: '2026-08-01T11:00:00Z' }] })
    }
    if (opts.fail) return Promise.reject(opts.fail)
    const qm = path.indexOf('?')
    const params = new URLSearchParams(qm < 0 ? '' : path.slice(qm + 1))
    list.push({ path: qm < 0 ? path : path.slice(0, qm), params })
    const status = params.get('status')
    const q = (params.get('q') ?? '').toLowerCase()
    const matched = corpus.filter(
      (r) =>
        (!status || r.status === status) &&
        (!q || r.id.toLowerCase().includes(q) || String(r.trigger).toLowerCase().includes(q)),
    )
    const offset = Number(params.get('offset') ?? 0)
    // Clamped exactly as `_page_limit` does it, and echoed: the ceiling is how the
    // view learns that a window cannot be widened any further.
    const limit = Math.max(1, Math.min(Number(params.get('limit') ?? 50), opts.clamp ?? 500))
    return Promise.resolve({
      runs: matched.slice(offset, offset + limit),
      count: matched.length,
      limit,
      offset,
    })
  })
  return list
}

// Braced, not a concise arrow body: `mockReset()` returns the mock itself, and a
// `beforeEach` that RETURNS a function has handed vitest a teardown hook — which it
// then calls with the test context, i.e. one extra `api(undefined)` per test.
beforeEach(() => {
  apiMock.mockReset()
})

describe('RunsView — the paged fetch (§5.1)', () => {
  it('asks the runs endpoint for a compact first page and renders THAT, not the aggregate preview', async () => {
    const calls = serve()
    render(<RunsView state={stateWith()} onToast={() => {}} />)

    await screen.findByText('run_0412')
    expect(calls).toHaveLength(1)
    const [first] = calls
    // Agent-prefixed: the unprefixed spelling resolves the `_` alias and 400s
    // E_AGENT_AMBIGUOUS on a two-agent workspace (console/AGENTS.md).
    expect(first?.path).toBe(LIST)
    // `summary=1` or the route answers with full run documents, every trace event
    // included — megabytes for a table that renders five columns.
    expect(first?.params.get('summary')).toBe('1')
    expect(first?.params.get('limit')).toBe('50')
    expect(first?.params.get('offset')).toBe('0')
    // Absent, not empty: unset means "every status" / "no search".
    expect(first?.params.has('status')).toBe(false)
    expect(first?.params.has('q')).toBe(false)

    // The window, and nothing from the 30-row preview the view used to render.
    expect(screen.getByText('run_0363')).toBeTruthy()
    expect(screen.queryByText('run_0362')).toBeNull()
    expect(screen.queryByText('preview_only_0')).toBeNull()
  })

  it('counts the pills from stats, over every run — not from the rows it happens to hold', async () => {
    // The regression itself, at the size the finding describes: two rows loaded, one
    // of them failed, while the workspace has 12 failures among 412 runs. The old
    // `runCounts(state.runs)` could only ever have said 1 here (or 2, over the
    // preview) — and the Overview tile one screen up was already saying 412.
    serve([
      { id: 'run_recent_ok', status: 'completed', trigger: 'message.received', tokens: 5 },
      { id: 'run_recent_bad', status: 'failed', trigger: 'message.received', tokens: 5 },
    ])
    render(<RunsView state={stateWith()} onToast={() => {}} />)

    await screen.findByText('run_recent_ok')
    expect(screen.getByRole('button', { name: 'failed · 12' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'failed · 2' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'failed · 1' })).toBeNull()
    // And `All` is the tile's number, so the two cannot disagree.
    expect(screen.getByRole('button', { name: 'All · 412' })).toBeTruthy()
  })

  it('says how much of the set is on screen, and loads the next page on demand', async () => {
    const calls = serve()
    render(<RunsView state={stateWith()} onToast={() => {}} />)

    // The line the view did not have. Without it, 50 rows are indistinguishable
    // from all of them.
    await screen.findByText('showing 50 of 412')

    fireEvent.click(screen.getByRole('button', { name: 'Load more' }))

    await screen.findByText('showing 100 of 412')
    // Refetched as one whole window (`offset=0&limit=100`), not merged from two
    // pages: the list is newest-first and moves under the reader, so an appended
    // page 2 would duplicate whatever arrived in between.
    expect(calls[1]?.params.get('offset')).toBe('0')
    expect(calls[1]?.params.get('limit')).toBe('100')
    expect(screen.getByText('run_0313')).toBeTruthy()
  })

  it('offers Load more only while rows are missing', async () => {
    serve(CORPUS.slice(0, 3))
    render(
      <RunsView state={stateWith({ runs: 3, byStatus: { completed: 3 } })} onToast={() => {}} />,
    )

    await screen.findByText('showing 3 of 3')
    // A control that refetches the same window is a button that appears to do
    // nothing, which is its own kind of dishonesty.
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull()
  })

  it('says when a workspace is wider than one request can reach', async () => {
    // The server clamps `limit` to 1..500 (`_page_limit`), so growing a single
    // `offset=0` request cannot reach every run of a big workspace. Clamped low here
    // to keep this to two clicks — the view reads the ceiling off the echoed `limit`
    // rather than from a copy of the server's constant, so the value is irrelevant.
    serve(CORPUS, { clamp: 100 })
    render(<RunsView state={stateWith()} onToast={() => {}} />)
    await screen.findByText('showing 50 of 412')

    fireEvent.click(screen.getByRole('button', { name: 'Load more' }))
    await screen.findByText('showing 100 of 412')

    fireEvent.click(screen.getByRole('button', { name: 'Load more' }))

    // The button goes, and something says why. Dropping it silently would read as
    // "that is all of them" — the exact confident wrong answer §5.1 is about.
    await screen.findByText(/as wide as one request goes/)
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull()
    expect(screen.getByText('showing 100 of 412')).toBeTruthy()
  })

  it('sends a chosen status as status=, and keeps it across a poll', async () => {
    const corpus = [...CORPUS]
    const calls = serve(corpus)
    const { rerender } = render(<RunsView state={stateWith()} onToast={() => {}} />)
    await screen.findByText('run_0412')

    fireEvent.click(screen.getByRole('button', { name: 'failed · 12' }))

    // All 12 failures are older than page 1, so this row can only be here because
    // the server was asked. This is the pill telling the truth AND being able to
    // honour it — the two halves of §5.1.
    await screen.findByText('run_0001')
    expect(calls[1]?.params.get('status')).toBe('failed')
    expect(screen.getByText('showing 12 of 12')).toBeTruthy()
    expect(screen.queryByText('run_0412')).toBeNull()

    // A poll lands with a 13th failure. The refetch it triggers must still be the
    // FILTERED request — a live table that silently reset to 'all' every six seconds
    // would be worse than one that never moved.
    corpus.unshift({ id: 'run_0413', status: 'failed', trigger: 'webhook', tokens: 0 })
    rerender(
      <RunsView
        state={stateWith({ runs: 413, byStatus: { completed: 400, failed: 13 } })}
        onToast={() => {}}
      />,
    )
    await screen.findByText('showing 13 of 13')
    expect(calls[2]?.params.get('status')).toBe('failed')
  })

  it('sends the query as q= — and five keystrokes are ONE request, not five', async () => {
    const calls = serve()
    render(<RunsView state={stateWith()} onToast={() => {}} />)
    await screen.findByText('run_0412')
    expect(calls).toHaveLength(1)

    const input = screen.getByLabelText('Filter runs')
    for (const value of ['c', 'cr', 'cro', 'cron', 'cron.']) {
      fireEvent.change(input, { target: { value } })
    }
    // Nothing has gone out yet: with filtering server-side, a request per keystroke
    // would be five queries over the whole run table for a word half-typed.
    expect(calls).toHaveLength(1)

    // 206 of the 412 triggers are `cron.nightly`, and `count` is the size of the
    // FILTERED set — the N an operator needs in order to know this is a window.
    await screen.findByText('showing 50 of 206')
    expect(calls).toHaveLength(2)
    expect(calls[1]?.params.get('q')).toBe('cron.')
  })

  it('finds a run older than the loaded window instead of denying it exists', async () => {
    // The audit's exact symptom: "Searching a run id older than 30 returns 'No runs
    // match'". `run_0001` is the oldest of 412 — nowhere near any page the console
    // holds — and an operator pasting it has it in front of them, from a log or a
    // support thread, so the old answer was the console contradicting evidence.
    serve()
    render(<RunsView state={stateWith()} onToast={() => {}} />)
    await screen.findByText('run_0412')
    expect(screen.queryByText('run_0001')).toBeNull()

    fireEvent.change(screen.getByLabelText('Filter runs'), { target: { value: 'run_0001' } })

    await screen.findByText('run_0001')
    expect(screen.queryByText(/No runs match/)).toBeNull()
    expect(screen.getByText('showing 1 of 1')).toBeTruthy()
  })
})

describe('RunsView — the three ways a table can be empty', () => {
  it('reports a failed fetch as a failure, never as "no runs match"', async () => {
    // The class of bug this console exists to prevent (§5.4, §5.10): an outage and
    // an over-narrow filter render identically, so the operator goes looking for a
    // data problem that is really a broken request.
    serve(CORPUS, { fail: new Error('connection refused') })
    render(<RunsView state={stateWith()} onToast={() => {}} />)

    await screen.findByText(/Runs unavailable — connection refused/)
    expect(screen.queryByText(/No runs match/)).toBeNull()
    expect(screen.queryByText(/No runs yet/)).toBeNull()
    // And it must not claim a window either.
    expect(screen.queryByText(/^showing /)).toBeNull()
  })

  it('names an unauthenticated console rather than reporting it as an outage', async () => {
    // `UnauthorizedError.message` is the fixed sentinel `'unauthorized'`, which five
    // other views already switch their empty state on (`lib/api.ts`).
    serve(CORPUS, { fail: new Error('unauthorized') })
    render(<RunsView state={stateWith()} onToast={() => {}} />)

    await screen.findByText('Connect to load runs.')
    expect(screen.queryByText(/No runs/)).toBeNull()
    // No Retry: re-sending a rejected credential is theatre, and the shell's own poll
    // raises the Connect dialog for a credential failure.
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })

  it('offers a way back from a failure instead of leaving a dead window', async () => {
    // The shell's Refresh reloads `/console`; if the run numbers come back unchanged
    // the signature does not move and nothing here would refetch, so without this the
    // only recovery from a blip is to navigate away and come back.
    const outage: { fail?: Error } = { fail: new Error('connection refused') }
    serve(CORPUS, outage)
    render(<RunsView state={stateWith()} onToast={() => {}} />)
    await screen.findByText(/Runs unavailable/)

    outage.fail = undefined
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    expect(await screen.findByText('run_0412')).toBeTruthy()
    expect(screen.queryByText(/Runs unavailable/)).toBeNull()
  })

  it('tells a fresh install nothing has run, whatever filter is set', async () => {
    serve([])
    render(<RunsView state={stateWith({ runs: 0, byStatus: {} }, [])} onToast={() => {}} />)

    await screen.findByText(/No runs yet/)
    // Even with a pill active: `stats.runs === 0` is authoritative, and blaming the
    // filter for an agent that has never run is a wrong answer with a wrong remedy.
    fireEvent.click(screen.getByRole('button', { name: 'failed' }))
    expect(await screen.findByText(/No runs yet/)).toBeTruthy()
  })

  it('blames the filter only when runs do exist', async () => {
    serve()
    render(<RunsView state={stateWith()} onToast={() => {}} />)
    await screen.findByText('run_0412')

    fireEvent.change(screen.getByLabelText('Filter runs'), { target: { value: 'nothing-matches-this' } })

    expect(await screen.findByText(/No runs match this filter/)).toBeTruthy()
  })
})

describe('RunsView — the poll', () => {
  /**
   * The regression this whole migration exists to prevent.
   *
   * The legacy console re-rendered the filter block from an HTML string on every
   * 6s poll, which blew away the search input's value and caret. It worked around
   * that by sniffing `document.activeElement` and skipping the re-render while the
   * box had focus (`renderRuns`, and the warning in console/AGENTS.md).
   *
   * Adapted for §5.1: the poll now also triggers a REFETCH — the table owns its
   * request and refetches when `stats` moves — so this pins the stronger property.
   * Not merely "new props do not disturb the caret", but "new props, a new request
   * and a whole new set of rows do not disturb the caret".
   */
  it('keeps focus, value and caret in the filter box across a poll and its refetch', async () => {
    serve()
    const { rerender } = render(<RunsView state={stateWith()} onToast={() => {}} />)
    await screen.findByText('run_0412')

    const input = screen.getByLabelText('Filter runs') as HTMLInputElement
    input.focus()
    fireEvent.change(input, { target: { value: 'cron' } })
    input.setSelectionRange(2, 2)
    expect(document.activeElement).toBe(input)

    // A poll lands: a new run appeared and another changed status.
    rerender(
      <RunsView
        state={stateWith({ runs: 413, byStatus: { completed: 400, failed: 12, running: 1 } })}
        onToast={() => {}}
      />,
    )

    expect(document.activeElement).toBe(input)
    expect(input.value).toBe('cron')
    expect(input.selectionStart).toBe(2)

    // ...and once the debounced search and the poll's refetch have both landed and
    // replaced every row, the caret is still where the operator left it.
    await screen.findByText('showing 50 of 206')
    expect(document.activeElement).toBe(screen.getByLabelText('Filter runs'))
    expect((screen.getByLabelText('Filter runs') as HTMLInputElement).selectionStart).toBe(2)
  })

  it('still shows a new run within one poll, and without a second timer', async () => {
    // The property that must not regress. Before this change the rows came straight
    // from the shell's 6s poll, so a new run appeared for free; a table that owns its
    // own fetch has to earn that back, and the honest way is to refetch when the
    // numbers move rather than to start a timer of its own.
    const corpus = [...CORPUS]
    const calls = serve(corpus)
    const { rerender } = render(<RunsView state={stateWith()} onToast={() => {}} />)
    await screen.findByText('run_0412')

    // An identical poll must NOT refetch: `state` is a brand-new object every six
    // seconds, so depending on the object would turn a dashboard poll into a query
    // over the run table, forever, per tab (§5.8).
    rerender(<RunsView state={stateWith()} onToast={() => {}} />)
    expect(calls).toHaveLength(1)

    corpus.unshift({ id: 'run_0413', status: 'running', trigger: 'webhook', tokens: 0 })
    rerender(
      <RunsView
        state={stateWith({ runs: 413, byStatus: { completed: 400, failed: 12, running: 1 } })}
        onToast={() => {}}
      />,
    )
    expect(await screen.findByText('run_0413')).toBeTruthy()

    // And a run merely CHANGING status moves no total — which is why `byStatus` is
    // in the signature. Watching a run finish is the commonest reason to have this
    // page open at all.
    corpus[0] = { ...corpus[0]!, status: 'completed' }
    rerender(
      <RunsView
        state={stateWith({ runs: 413, byStatus: { completed: 401, failed: 12 } })}
        onToast={() => {}}
      />,
    )
    await waitFor(() => expect(screen.queryByText('running')).toBeNull())
    expect(calls).toHaveLength(3)
  })
})

describe('RunsView — traces and hostile data', () => {
  it('opens a run trace from the workspace-scoped endpoint', async () => {
    serve()
    render(<RunsView state={stateWith()} onToast={() => {}} />)
    await screen.findByText('run_0412')

    fireEvent.click(screen.getByRole('button', { name: 'Open trace for run_0412' }))

    await screen.findByText('Trace · run_0412')
    // NOT agent-prefixed: a run id already identifies its agent, and the server
    // serves this route unprefixed only.
    expect(apiMock.mock.calls.some(([p]) => p === '/runs/run_0412/trace')).toBe(true)
    expect(screen.getByText('run.started')).toBeTruthy()
  })

  it('renders run ids as text, so a hostile id cannot inject markup', async () => {
    // The legacy renderer interpolated ids into HTML strings and relied on esc().
    // React escapes text children, so this is structural rather than a discipline.
    const nasty = '<img src=x onerror=alert(1)>'
    serve([{ id: nasty, status: 'completed', trigger: 'message.received', tokens: 1 }])
    render(<RunsView state={stateWith({ runs: 1, byStatus: { completed: 1 } })} onToast={() => {}} />)

    expect(await screen.findByText(nasty)).toBeTruthy()
    expect(document.querySelector('img')).toBeNull()
  })
})
