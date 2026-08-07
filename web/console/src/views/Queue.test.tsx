import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueueView, groupFrames } from './Queue'
import type { QueueJob, QueueStatsFeed, TurnFrame } from './Queue'
import type { ConsoleState, QueueCounts } from '../lib/types'

const STATE = { agent: { name: 'acme' } } as unknown as ConsoleState

/**
 * The shell's polled `/queue/stats` result, which the view now receives instead of
 * fetching (audit §5.5). `counts: null` is "not known yet", NOT an empty queue.
 */
const feed = (counts: QueueCounts | null = {}, over: Partial<QueueStatsFeed> = {}): QueueStatsFeed => ({
  counts,
  error: null,
  loading: false,
  ...over,
})

const job = (over: Partial<QueueJob> = {}): QueueJob => ({
  id: 'job_1',
  type: 'email.send',
  status: 'pending',
  attempts: 0,
  maxAttempts: 3,
  ...over,
})

const enc = new TextEncoder()

/** An SSE body as a real `ReadableStream`, one chunk per element. */
function sseStream(chunks: string[]) {
  return new ReadableStream<Uint8Array>({
    start(c) {
      for (const chunk of chunks) c.enqueue(enc.encode(chunk))
      c.close()
    },
  })
}

/**
 * The stream response is duck-typed rather than a real `Response`: the reader only
 * reads `ok`/`status`/`body`, and keeping the ReadableStream un-piped is what lets
 * a test observe `cancel()` on the underlying source.
 */
const streamResponse = (body: ReadableStream<Uint8Array>) =>
  ({ ok: true, status: 200, body }) as unknown as Response

const frame = (seq: number, kind: string, data: unknown) =>
  `id: ${seq}\nevent: ${kind}\ndata: ${JSON.stringify(data)}\n\n`

/**
 * One fetch stub for both worlds: JSON for `/queue/*`, an SSE body for the turn
 * stream. `streams` is consumed in order, so a reconnect gets the next one.
 */
function stubFetch(opts: {
  // `jobs` may be a function so a test can change the answer between polls — the
  // table re-reads itself now, and a fixed array cannot show that it did.
  jobs?: QueueJob[] | (() => QueueJob[] | Error)
  streams?: (ReadableStream<Uint8Array> | (() => Response))[]
}) {
  const streams = [...(opts.streams ?? [])]
  const fn = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
    const url = String(input)
    const json = (body: unknown, status = 200) =>
      Promise.resolve(
        new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } }),
      )
    if (url.includes('/stream')) {
      const next = streams.shift()
      if (!next) return Promise.resolve(streamResponse(sseStream([])))
      return Promise.resolve(typeof next === 'function' ? next() : streamResponse(next))
    }
    if (url.includes('/queue/jobs')) {
      const jobs = typeof opts.jobs === 'function' ? opts.jobs() : (opts.jobs ?? [])
      if (jobs instanceof Error) {
        return json({ ok: false, error: { code: 'E_INTERNAL', message: jobs.message } }, 500)
      }
      return json({ jobs })
    }
    return json({})
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

describe('QueueView', () => {
  beforeEach(() => localStorage.setItem('rya_token', 'test-token'))
  afterEach(() => vi.unstubAllGlobals())

  it('renders the job table from the payload and the tiles from the shell poll', async () => {
    const fetchMock = stubFetch({
      jobs: [job(), job({ id: 'job_turn', type: 'chat-turn', status: 'running', attempts: 1 })],
    })
    render(
      <QueueView
        state={STATE}
        onToast={() => {}}
        stats={feed({ pending: 2, running: 1, completed: 7, failed: 1 })}
      />,
    )

    await waitFor(() => expect(screen.getByText('job_1')).toBeTruthy())
    expect(screen.getByText('job_turn')).toBeTruthy()
    expect(screen.getByText('chat-turn')).toBeTruthy()
    expect(screen.getByText('turn')).toBeTruthy() // the chat-turn marker chip
    expect(screen.getByText('1/3')).toBeTruthy()
    expect(screen.getByText('7')).toBeTruthy() // Completed tile, from the prop

    // §5.5, structurally: the view must not read `/queue/stats` itself. The shell
    // already polls it every 6s for the sidebar badge, and a second reader on its
    // own clock is how the tiles came to disagree with the badge beside them.
    expect(fetchMock.mock.calls.filter((c) => String(c[0]).includes('/queue/stats'))).toEqual([])
  })

  it('reads an empty queue as the healthy normal state', async () => {
    stubFetch({ jobs: [] })
    render(<QueueView state={STATE} onToast={() => {}} stats={feed({ pending: 0 })} />)
    await waitFor(() => expect(screen.getByText(/Queue is empty/)).toBeTruthy())
  })

  describe('the tiles (audit §5.4)', () => {
    /** The four tile values, in render order: pending, running, completed, DLQ. */
    const tileValues = () => Array.from(document.querySelectorAll('.stat .v')).map((n) => n.textContent)

    /**
     * The tiles read props, so asserting them is synchronous — but the job table's
     * first poll resolves one microtask after the test body would otherwise end,
     * and that state update would land outside `act` (audit §9 records the same
     * pattern in `Knowledge.test.tsx`). Awaiting the settled table is what puts it
     * inside one; every test here stubs an empty queue for that reason.
     */
    const settled = () => screen.findByText(/Queue is empty/)

    it('shows a real zero as 0', async () => {
      stubFetch({ jobs: [] })
      render(
        <QueueView
          state={STATE}
          onToast={() => {}}
          stats={feed({ pending: 0, running: 0, completed: 0, failed: 0 })}
        />,
      )
      await waitFor(() => expect(screen.getByText(/Queue is empty/)).toBeTruthy())
      // A drained queue IS zero, and must still read as zero — the fix must not
      // trade a false zero for a false unknown.
      expect(tileValues()).toEqual(['0', '0', '0', '0'])
    })

    it('shows an unknown depth as — while the first poll is in flight', async () => {
      stubFetch({ jobs: [] })
      render(<QueueView state={STATE} onToast={() => {}} stats={feed(null, { loading: true })} />)
      await settled()
      // The bug: `num(undefined)` is '0', so every tile claimed a drained queue
      // before anything had answered.
      expect(tileValues()).toEqual(['—', '—', '—', '—'])
      expect(screen.getAllByText('reading…').length).toBe(4)
    })

    it('shows an unknown depth as — and names the failure, never "Dead-letter 0"', async () => {
      stubFetch({ jobs: [] })
      render(
        <QueueView
          state={STATE}
          onToast={() => {}}
          stats={feed(null, { error: 'HTTP 500' })}
        />,
      )
      await settled()
      expect(tileValues()).toEqual(['—', '—', '—', '—'])
      expect(screen.getByText(/Queue depth unavailable/)).toBeTruthy()
      expect(screen.getByText('HTTP 500')).toBeTruthy()
    })

    it('distinguishes an unauthenticated console from an outage', async () => {
      stubFetch({ jobs: [] })
      render(
        <QueueView state={STATE} onToast={() => {}} stats={feed(null, { error: 'unauthorized' })} />,
      )
      await settled()
      expect(screen.getByText(/Connect to read queue depth/)).toBeTruthy()
    })

    it('keeps last-known counts on a failed refresh, marked as stale', async () => {
      stubFetch({ jobs: [] })
      render(
        <QueueView
          state={STATE}
          onToast={() => {}}
          stats={feed({ pending: 4, failed: 2 }, { error: 'HTTP 502' })}
        />,
      )
      await settled()
      // Real numbers stay — blanking a dashboard on one bad tick is its own lie —
      // but they are labelled unconfirmed rather than presented as current.
      expect(tileValues()).toEqual(['4', '0', '0', '2'])
      expect(screen.getByText(/Queue depth is stale/)).toBeTruthy()
    })
  })

  describe('the job table refresh (audit §5.5)', () => {
    it('re-reads the table on its own interval', async () => {
      vi.useFakeTimers()
      try {
        let jobs = [job({ id: 'job_first' })]
        const fetchMock = stubFetch({ jobs: () => jobs })
        render(<QueueView state={STATE} onToast={() => {}} stats={feed()} />)

        await act(async () => {
          await vi.advanceTimersByTimeAsync(0)
        })
        expect(screen.getByText('job_first')).toBeTruthy()
        const first = fetchMock.mock.calls.filter((c) => String(c[0]).includes('/queue/jobs')).length
        expect(first).toBe(1)

        // A job goes to the dead-letter queue while the operator is looking at the
        // page. Before this fix the view loaded once on entry, so it never appeared
        // — while the sidebar badge, polling the same queue, went amber.
        jobs = [job({ id: 'job_first' }), job({ id: 'job_dlq', status: 'failed', deadLetter: true })]
        await act(async () => {
          await vi.advanceTimersByTimeAsync(6000)
        })
        expect(screen.getByText('job_dlq')).toBeTruthy()
        expect(
          fetchMock.mock.calls.filter((c) => String(c[0]).includes('/queue/jobs')).length,
        ).toBeGreaterThan(first)
      } finally {
        vi.useRealTimers()
      }
    })

    it('stops polling once unmounted', async () => {
      vi.useFakeTimers()
      try {
        const fetchMock = stubFetch({ jobs: [job()] })
        const { unmount } = render(<QueueView state={STATE} onToast={() => {}} stats={feed()} />)
        await act(async () => {
          await vi.advanceTimersByTimeAsync(0)
        })
        const before = fetchMock.mock.calls.length
        unmount()
        await act(async () => {
          await vi.advanceTimersByTimeAsync(30_000)
        })
        // An interval that outlives the view is a leak that compounds every time the
        // operator navigates back to this page.
        expect(fetchMock.mock.calls.length).toBe(before)
      } finally {
        vi.useRealTimers()
      }
    })

    it('does not blank the table when one refresh fails', async () => {
      vi.useFakeTimers()
      try {
        let answer: () => QueueJob[] | Error = () => [job({ id: 'job_kept' })]
        stubFetch({ jobs: () => answer() })
        render(<QueueView state={STATE} onToast={() => {}} stats={feed()} />)
        await act(async () => {
          await vi.advanceTimersByTimeAsync(0)
        })
        expect(screen.getByText('job_kept')).toBeTruthy()

        answer = () => new Error('boom')
        await act(async () => {
          await vi.advanceTimersByTimeAsync(6000)
        })
        // The rows an operator was reading survive the blip, and the blip is
        // visible rather than silent (console/AGENTS.md: don't clobber a live view).
        expect(screen.getByText('job_kept')).toBeTruthy()
        expect(screen.getByText(/Job list is stale/)).toBeTruthy()
      } finally {
        vi.useRealTimers()
      }
    })

    it('takes the screen with an error only when there is nothing to show', async () => {
      stubFetch({ jobs: () => new Error('boom') })
      render(<QueueView state={STATE} onToast={() => {}} stats={feed()} />)
      await waitFor(() => expect(screen.getByText(/Queue unavailable/)).toBeTruthy())
      expect(screen.queryByText(/Queue is empty/)).toBeNull()
    })
  })

  it('offers retry only on terminal jobs and cancel only on live ones', async () => {
    stubFetch({ jobs: [job({ id: 'job_dead', status: 'failed', deadLetter: true }), job({ id: 'job_live', status: 'running' })] })
    render(<QueueView state={STATE} onToast={() => {}} stats={feed()} />)

    await waitFor(() => expect(screen.getByText('job_dead')).toBeTruthy())
    expect(screen.getAllByRole('button', { name: 'Retry' }).length).toBe(1)
    expect(screen.getAllByRole('button', { name: 'Cancel' }).length).toBe(1)
    expect(screen.getByText('DLQ')).toBeTruthy()
  })

  it('retries a dead-lettered job and toasts the outcome', async () => {
    const fetchMock = stubFetch({ jobs: [job({ id: 'job_dead', status: 'failed' })] })
    const toasts: string[] = []
    render(<QueueView state={STATE} onToast={(m) => toasts.push(m)} stats={feed()} />)
    await waitFor(() => expect(screen.getByText('job_dead')).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(toasts).toEqual(['Requeued job_dead']))

    const posted = fetchMock.mock.calls.map((c) => String(c[0])).filter((u) => u.includes('/retry'))
    // Workspace-scoped: the queue is a per-workspace resource, and there is no
    // agent-prefixed spelling of `/queue/*` on the server.
    expect(posted).toEqual(['/queue/jobs/job_dead/retry'])
  })

  describe('the durable turn-stream inspector', () => {
    const turnJob = [job({ id: 'turn_1', type: 'chat-turn', status: 'running' })]

    async function openInspector(fetchMock: ReturnType<typeof stubFetch>) {
      render(<QueueView state={STATE} onToast={() => {}} stats={feed()} />)
      await waitFor(() => expect(screen.getByText('turn_1')).toBeTruthy())
      fireEvent.click(screen.getByRole('button', { name: 'Inspect turn stream turn_1' }))
      return fetchMock
    }

    it('parses each frame kind into its own rendering, folding tokens into one bubble', async () => {
      const fetchMock = stubFetch({
        jobs: turnJob,
        streams: [
          sseStream([
            ': keep-alive\n\n', // a comment, never a frame
            frame(1, 'trace', { kind: 'tool.call', label: 'orders.lookup' }),
            frame(2, 'token', { text: 'check' }),
            frame(3, 'token', { text: 'ing now' }),
            frame(4, 'restart', { attempt: 2 }),
            frame(5, 'message', { role: 'assistant', content: 'your order ships today' }),
            frame(6, 'run', { status: 'completed', id: 'run_x', tokens: 1234 }),
          ]),
        ],
      })
      await openInspector(fetchMock)

      await waitFor(() => expect(screen.getByText('run · completed')).toBeTruthy())
      expect(screen.getByText('tool.call')).toBeTruthy()
      expect(screen.getByText('orders.lookup')).toBeTruthy()
      // Two token frames, one bubble.
      expect(screen.getByText('checking now')).toBeTruthy()
      expect(screen.getByText('streamed text')).toBeTruthy()
      expect(screen.getByText(/reclaimed after a crash \(attempt 2\)/)).toBeTruthy()
      expect(screen.getByText('your order ships today')).toBeTruthy()
      expect(screen.getByText('run_x · 1,234 tokens')).toBeTruthy()
      // The keep-alive comment must not have become a frame.
      expect(screen.getByText(/6 frames · durable buffer/)).toBeTruthy()
    })

    it('resumes from the last seq when the server sends its idle notice', async () => {
      const fetchMock = stubFetch({
        jobs: turnJob,
        streams: [
          sseStream([
            frame(1, 'trace', { kind: 'run.started', label: 'turn' }),
            frame(2, 'token', { text: 'thinking' }),
            ': idle-timeout\n\n', // nothing appended for a while; reconnect, don't hang
          ]),
          sseStream([frame(3, 'run', { status: 'completed', id: 'run_y', tokens: 10 })]),
        ],
      })
      await openInspector(fetchMock)

      await waitFor(() => expect(screen.getByText('run · completed')).toBeTruthy())

      const streamUrls = fetchMock.mock.calls.map((c) => String(c[0])).filter((u) => u.includes('/stream'))
      expect(streamUrls.length).toBe(2)
      // Agent-prefixed, and the first connect carries no cursor.
      expect(streamUrls[0]).toBe('/agents/acme/turns/turn_1/stream')
      // The reconnect resumes AFTER the last frame it saw, rather than replaying.
      expect(streamUrls[1]).toBe('/agents/acme/turns/turn_1/stream?after=2')
      // Frames from both connections are in one list, in order.
      expect(screen.getByText(/3 frames/)).toBeTruthy()
      expect(screen.getByText(/resumed 1×/)).toBeTruthy()
      // The bearer token has to ride along, which is why this is `fetch` and not
      // `EventSource`.
      const init = fetchMock.mock.calls.find((c) => String(c[0]).includes('/stream'))?.[1]
      expect((init?.headers as Record<string, string>).Authorization).toBe('Bearer test-token')
    })

    it('keeps tailing across waiting_approval — a pause is not an ending', async () => {
      const fetchMock = stubFetch({
        jobs: turnJob,
        streams: [
          sseStream([
            frame(1, 'run', { status: 'waiting_approval', id: 'run_z', tokens: 5 }),
            ': idle-timeout\n\n',
          ]),
          sseStream([frame(2, 'run', { status: 'completed', id: 'run_z', tokens: 9 })]),
        ],
      })
      await openInspector(fetchMock)

      await waitFor(() => expect(screen.getByText('run · completed')).toBeTruthy())
      expect(screen.getByText('run · waiting_approval')).toBeTruthy()
      expect(fetchMock.mock.calls.filter((c) => String(c[0]).includes('/stream')).length).toBe(2)
    })

    it('abandons the previous turn when another is inspected', async () => {
      let firstCancelled = false
      const live = new ReadableStream<Uint8Array>({
        start(c) {
          c.enqueue(enc.encode(frame(1, 'trace', { kind: 'run.started', label: 'first turn' })))
        },
        cancel() {
          firstCancelled = true
        },
      })
      const fetchMock = stubFetch({
        jobs: [
          job({ id: 'turn_1', type: 'chat-turn', status: 'running' }),
          job({ id: 'turn_2', type: 'chat-turn', status: 'running' }),
        ],
        streams: [live, sseStream([frame(9, 'run', { status: 'completed', id: 'run_2', tokens: 3 })])],
      })
      render(<QueueView state={STATE} onToast={() => {}} stats={feed()} />)
      await waitFor(() => expect(screen.getByText('turn_1')).toBeTruthy())

      fireEvent.click(screen.getByRole('button', { name: 'Inspect turn stream turn_1' }))
      await waitFor(() => expect(screen.getByText('first turn')).toBeTruthy())

      fireEvent.click(screen.getByRole('button', { name: 'Inspect turn stream turn_2' }))

      // The inspector is keyed by turn id, so it remounts: the first reader is
      // released and its frames do not bleed into the second turn's list.
      await waitFor(() => expect(firstCancelled).toBe(true))
      await waitFor(() => expect(screen.getByText('run · completed')).toBeTruthy())
      expect(screen.queryByText('first turn')).toBeNull()
      expect(screen.getByText(/1 frames/)).toBeTruthy()
      expect(fetchMock.mock.calls.filter((c) => String(c[0]).includes('turn_2/stream')).length).toBe(1)
    })

    it('stops the reader when the component unmounts', async () => {
      let cancelled = false
      let controller: ReadableStreamDefaultController<Uint8Array> | null = null
      // A stream that never ends, so only the abort can stop the reader.
      const live = new ReadableStream<Uint8Array>({
        start(c) {
          controller = c
          c.enqueue(enc.encode(frame(1, 'trace', { kind: 'run.started', label: 'turn' })))
        },
        cancel() {
          cancelled = true
        },
      })
      const fetchMock = stubFetch({ jobs: turnJob, streams: [live] })
      const { unmount } = render(<QueueView state={STATE} onToast={() => {}} stats={feed()} />)
      await waitFor(() => expect(screen.getByText('turn_1')).toBeTruthy())
      fireEvent.click(screen.getByRole('button', { name: 'Inspect turn stream turn_1' }))
      await waitFor(() => expect(screen.getByText('run.started')).toBeTruthy())

      unmount()

      await waitFor(() => expect(cancelled).toBe(true))
      // The cancel really closed the source, so a late frame has nowhere to go:
      // nothing decodes into an unmounted component, and the loop did not open a
      // replacement connection on the way out.
      expect(() => controller!.enqueue(enc.encode(frame(2, 'token', { text: 'late' })))).toThrow()
      await new Promise((r) => setTimeout(r, 20))
      expect(fetchMock.mock.calls.filter((c) => String(c[0]).includes('/stream')).length).toBe(1)
      expect(document.body.textContent).not.toContain('late')
    })
  })
})

describe('groupFrames', () => {
  it('coalesces consecutive tokens and keys blocks by seq, not position', () => {
    const frames: TurnFrame[] = [
      { seq: 4, kind: 'token', data: { text: 'a' } },
      { seq: 5, kind: 'token', data: { text: 'b' } },
      { seq: 6, kind: 'trace', data: { kind: 'log', label: 'x' } },
      { seq: 7, kind: 'token', data: { text: 'c' } },
    ]
    expect(groupFrames(frames)).toEqual([
      { key: 'text-4', type: 'text', text: 'ab' },
      { key: 'trace-6', type: 'trace', kind: 'log', label: 'x' },
      { key: 'text-7', type: 'text', text: 'c' },
    ])
  })

  it('drops `ui` frames, which are chat-surface directives rather than evidence', () => {
    expect(groupFrames([{ seq: 1, kind: 'ui', data: { component: 'card' } }])).toEqual([])
  })
})
