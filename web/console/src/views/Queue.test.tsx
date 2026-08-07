import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueueView, groupFrames } from './Queue'
import type { QueueJob, TurnFrame } from './Queue'
import type { ConsoleState } from '../lib/types'

const STATE = { agent: { name: 'acme' } } as unknown as ConsoleState

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
  counts?: Record<string, number>
  jobs?: QueueJob[]
  streams?: (ReadableStream<Uint8Array> | (() => Response))[]
}) {
  const streams = [...(opts.streams ?? [])]
  const fn = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
    const url = String(input)
    const json = (body: unknown) =>
      Promise.resolve(
        new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } }),
      )
    if (url.includes('/stream')) {
      const next = streams.shift()
      if (!next) return Promise.resolve(streamResponse(sseStream([])))
      return Promise.resolve(typeof next === 'function' ? next() : streamResponse(next))
    }
    if (url.includes('/queue/stats')) return json({ counts: opts.counts ?? {} })
    if (url.includes('/queue/jobs')) return json({ jobs: opts.jobs ?? [] })
    return json({})
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

describe('QueueView', () => {
  beforeEach(() => localStorage.setItem('rya_token', 'test-token'))
  afterEach(() => vi.unstubAllGlobals())

  it('renders the counts and the job table from the payload', async () => {
    stubFetch({
      counts: { pending: 2, running: 1, completed: 7, failed: 1 },
      jobs: [job(), job({ id: 'job_turn', type: 'chat-turn', status: 'running', attempts: 1 })],
    })
    render(<QueueView state={STATE} onToast={() => {}} />)

    await waitFor(() => expect(screen.getByText('job_1')).toBeTruthy())
    expect(screen.getByText('job_turn')).toBeTruthy()
    expect(screen.getByText('chat-turn')).toBeTruthy()
    expect(screen.getByText('turn')).toBeTruthy() // the chat-turn marker chip
    expect(screen.getByText('1/3')).toBeTruthy()
    expect(screen.getByText('7')).toBeTruthy() // Completed tile
  })

  it('reads an empty queue as the healthy normal state', async () => {
    stubFetch({ counts: { pending: 0 }, jobs: [] })
    render(<QueueView state={STATE} onToast={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Queue is empty/)).toBeTruthy())
  })

  it('offers retry only on terminal jobs and cancel only on live ones', async () => {
    stubFetch({ jobs: [job({ id: 'job_dead', status: 'failed', deadLetter: true }), job({ id: 'job_live', status: 'running' })] })
    render(<QueueView state={STATE} onToast={() => {}} />)

    await waitFor(() => expect(screen.getByText('job_dead')).toBeTruthy())
    expect(screen.getAllByRole('button', { name: 'Retry' }).length).toBe(1)
    expect(screen.getAllByRole('button', { name: 'Cancel' }).length).toBe(1)
    expect(screen.getByText('DLQ')).toBeTruthy()
  })

  it('retries a dead-lettered job and toasts the outcome', async () => {
    const fetchMock = stubFetch({ jobs: [job({ id: 'job_dead', status: 'failed' })] })
    const toasts: string[] = []
    render(<QueueView state={STATE} onToast={(m) => toasts.push(m)} />)
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
      render(<QueueView state={STATE} onToast={() => {}} />)
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
      render(<QueueView state={STATE} onToast={() => {}} />)
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
      const { unmount } = render(<QueueView state={STATE} onToast={() => {}} />)
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
