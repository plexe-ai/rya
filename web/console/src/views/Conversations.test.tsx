import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { ConversationsView } from './Conversations'
import type { SessionSummary } from './Conversations'
import type { ConsoleState, Stats } from '../lib/types'

const session = (id: string, over: Partial<SessionSummary> = {}): SessionSummary => ({
  id,
  title: `Thread ${id}`,
  channel: 'email',
  externalId: 'ada@example.com',
  messageCount: 2,
  lastMessageAt: '2026-08-01T11:00:00Z',
  ...over,
})

const many = (n: number): SessionSummary[] =>
  Array.from({ length: n }, (_, i) => session(`ses_${i + 1}`))

/**
 * Only the fields this view reads — plus one it must NOT read.
 *
 * `sessions` on the aggregate is present here and deliberately wrong. It is the
 * `/console` 50-row preview that was §5.2's root cause, and a fixture that simply
 * omitted the field could not tell a view that fetches its own list from one that
 * still reads the preview and happened to look right. So the decoy row is the
 * assertion: if it ever appears on screen, the regression is back.
 */
const stateWith = (stats: Partial<Stats> = {}) =>
  ({
    agent: { name: 'support-agent' },
    stats: { sessions: 2, messages: 4, ...stats },
    sessions: [session('ses_decoy', { title: 'Aggregate preview row' })],
  }) as unknown as ConsoleState

const DETAIL = {
  id: 'ses_1',
  title: 'Thread ses_1',
  channel: 'email',
  externalId: 'ada@example.com',
  messages: [
    { role: 'user', content: 'where is my order', ts: '2026-08-01T11:00:00Z' },
    { role: 'assistant', content: 'checking now', ts: '2026-08-01T11:00:04Z', runId: 'run_9' },
    { role: 'tool', content: 'orders.lookup ok', ts: '2026-08-01T11:00:05Z' },
  ],
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

/**
 * `fetch` is stubbed rather than `../lib/api` mocked.
 *
 * The view calls two endpoints now, so the double has to tell them apart — and doing
 * that at the fetch boundary keeps `lib/api.ts`'s real classification in play, which
 * several assertions below are actually about: the `{ok:false,error:{…}}` envelope, the
 * `'unauthorized'` sentinel that only a credential 401 carries, and the `HTTP <status>`
 * fallback. A mocked `api()` would let this file invent those instead of exercising
 * them, which is §9's fixtures-that-encode-the-bug failure mode.
 *
 * A route may return a body (wrapped in a 200), a `Response`, or a promise — a detail
 * request that never settles is how the loading state is asserted.
 */
function stubFetch(routes: {
  list?: (url: string) => Response | Promise<Response> | unknown
  detail?: (id: string) => Response | Promise<Response> | unknown
}) {
  const fn = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    const detail = /^\/sessions\/(.+)$/.exec(url)
    const out = detail
      ? routes.detail?.(decodeURIComponent(detail[1] ?? ''))
      : routes.list?.(url)
    if (out instanceof Promise) return out
    return Promise.resolve(out instanceof Response ? out : json(out ?? {}))
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

const urlsOf = (fn: ReturnType<typeof stubFetch>) => fn.mock.calls.map((c) => String(c[0]))
const limitOf = (url: string) => Number(new URL(url, 'http://console.test').searchParams.get('limit'))

describe('ConversationsView', () => {
  beforeEach(() => localStorage.setItem('rya_token', 'test-token'))
  afterEach(() => vi.unstubAllGlobals())

  it('lists the sessions it fetched itself, not the aggregate 50-row preview', async () => {
    const fetchMock = stubFetch({
      list: () => ({ sessions: [session('ses_1'), session('ses_2')], count: 2 }),
    })
    render(<ConversationsView state={stateWith()} onToast={() => {}} />)

    await waitFor(() => expect(screen.getByText('Thread ses_1')).toBeTruthy())
    expect(screen.getByText('Thread ses_2')).toBeTruthy()
    expect(screen.getAllByText('ada@example.com').length).toBe(2)
    // The field the old view read is still on the state and must not reach the DOM.
    expect(screen.queryByText('Aggregate preview row')).toBeNull()

    // Agent-scoped and paged. The unprefixed spelling resolves the `_` alias and 400s
    // `E_AGENT_AMBIGUOUS` on a two-agent workspace (lib/agent.ts).
    expect(urlsOf(fetchMock)[0]).toBe('/agents/support-agent/sessions?limit=50&offset=0')
    // Everything loaded, so there is nothing to load: the button is a claim about
    // what is missing, and here nothing is.
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull()
    expect(screen.getByText(/Showing 2 of 2 conversations/)).toBeTruthy()
  })

  it('separates a first read still in flight from a genuinely empty workspace', async () => {
    stubFetch({ list: () => ({ sessions: [], count: 0 }) })
    render(<ConversationsView state={stateWith({ sessions: 0, messages: 0 })} onToast={() => {}} />)

    // Before the response lands, "no conversations" is a claim we have not verified.
    expect(screen.getByText(/Loading conversations/)).toBeTruthy()
    await waitFor(() => expect(screen.getByText(/No conversations yet/)).toBeTruthy())
    // An empty list carries no count line — "showing 0 of 0" is noise, not honesty.
    expect(screen.queryByText(/Showing 0 of 0/)).toBeNull()
  })

  // §5.2, defect 1
  it('says how much of the list is on screen, and pages past the 50-row cut', async () => {
    const seen: number[] = []
    stubFetch({
      list: (url) => {
        const limit = limitOf(url)
        seen.push(limit)
        return { sessions: many(Math.min(limit, 137)), count: 137, limit, offset: 0 }
      },
    })
    render(<ConversationsView state={stateWith({ sessions: 137, messages: 900 })} onToast={() => {}} />)

    await waitFor(() => expect(screen.getByText(/Showing 50 of 137 conversations/)).toBeTruthy())
    // Conversation 51 was unreachable from the console entirely before this.
    expect(screen.queryByText('Thread ses_97')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Load more' }))
    await waitFor(() => expect(screen.getByText(/Showing 100 of 137 conversations/)).toBeTruthy())
    expect(seen).toEqual([50, 100])
    expect(screen.getByText('Thread ses_97')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Load more' }))
    await waitFor(() => expect(screen.getByText(/Showing 137 of 137 conversations/)).toBeTruthy())
    // Nothing left to fetch, so the affordance goes away rather than lying.
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull()
  })

  it('stops paging at the server ceiling instead of offering a button that cannot help', async () => {
    // `limit` is clamped to 1..500 server-side, so a tenth page is the last one a
    // single request can serve. A "Load more" beyond it would re-fetch the same 500
    // rows for ever and look like a broken button rather than a boundary.
    stubFetch({
      list: (url) => {
        const limit = limitOf(url)
        return { sessions: many(Math.min(limit, 900)), count: 900, limit, offset: 0 }
      },
    })
    render(<ConversationsView state={stateWith({ sessions: 900, messages: 4000 })} onToast={() => {}} />)

    for (let page = 2; page <= 10; page++) {
      fireEvent.click(await waitFor(() => screen.getByRole('button', { name: 'Load more' })))
      await waitFor(() =>
        expect(screen.getByText(new RegExp(`Showing ${page * 50} of 900 conversations`))).toBeTruthy(),
      )
    }
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull()
    expect(screen.getByText(/500 is the server's per-request ceiling/)).toBeTruthy()
  })

  it('falls back to the aggregate total when the runtime predates `count`', async () => {
    // The paging response is `{sessions, count, limit, offset}`, but a server one
    // release older answers `{sessions}` — and `stats.sessions` is the same number,
    // computed by `snapshot.py` over the same store call. A missing `count` must not
    // become a silent "showing 50 of 50".
    stubFetch({ list: () => ({ sessions: many(50) }) })
    render(<ConversationsView state={stateWith({ sessions: 137, messages: 900 })} onToast={() => {}} />)

    await waitFor(() => expect(screen.getByText(/Showing 50 of 137 conversations/)).toBeTruthy())
    expect(screen.getByRole('button', { name: 'Load more' })).toBeTruthy()
  })

  it('re-reads the window when the poll reports new activity, and not otherwise', async () => {
    const fetchMock = stubFetch({ list: () => ({ sessions: [session('ses_1')], count: 1 }) })
    const { rerender } = render(<ConversationsView state={stateWith()} onToast={() => {}} />)
    await waitFor(() => expect(screen.getByText('Thread ses_1')).toBeTruthy())
    expect(urlsOf(fetchMock).length).toBe(1)

    // The shell hands every view a brand-new `state` object every 6s. Same numbers,
    // so nothing changed and nothing is re-read: the list is kept live by the poll's
    // own signal, not by a second timer.
    rerender(<ConversationsView state={stateWith()} onToast={() => {}} />)
    await Promise.resolve()
    expect(urlsOf(fetchMock).length).toBe(1)

    rerender(<ConversationsView state={stateWith({ messages: 5 })} onToast={() => {}} />)
    await waitFor(() => expect(urlsOf(fetchMock).length).toBe(2))
  })

  it('loads the transcript on demand from the workspace-scoped session route', async () => {
    const fetchMock = stubFetch({
      list: () => ({ sessions: [session('ses_1')], count: 1 }),
      detail: () => DETAIL,
    })
    render(<ConversationsView state={stateWith()} onToast={() => {}} />)
    await waitFor(() => expect(screen.getByText('Thread ses_1')).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: 'Open conversation Thread ses_1' }))

    await waitFor(() => expect(screen.getByText('where is my order')).toBeTruthy())
    expect(screen.getByText('checking now')).toBeTruthy()
    expect(screen.getByText('run_9')).toBeTruthy()
    expect(screen.getByText('3 messages · ada@example.com')).toBeTruthy()

    // The asymmetry is deliberate, and both halves are pinned: the LIST is
    // agent-prefixed, `/sessions/{id}` is not — the session id already identifies the
    // agent, and the server serves no `/agents/{name}/sessions/{id}`.
    const urls = urlsOf(fetchMock)
    expect(urls[0]).toContain('/agents/support-agent/sessions?')
    expect(urls).toContain('/sessions/ses_1')
    expect(urls[1]).not.toContain('/agents/')
  })

  it('places the roles on the right sides of the thread', async () => {
    stubFetch({ list: () => ({ sessions: [session('ses_1')], count: 1 }), detail: () => DETAIL })
    render(<ConversationsView state={stateWith()} onToast={() => {}} />)
    await waitFor(() => expect(screen.getByText('Thread ses_1')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Open conversation Thread ses_1' }))
    await waitFor(() => expect(screen.getByText('where is my order')).toBeTruthy())

    const bubble = (text: string) => screen.getByText(text).closest('.msg')!
    expect(bubble('where is my order').className).toContain('in')
    expect(bubble('checking now').className).toContain('out')
    // tool/system messages are centred asides, not either party's speech
    expect(bubble('orders.lookup ok').className).toContain('sys')
  })

  it('says so when a session exists but has no messages', async () => {
    stubFetch({
      list: () => ({ sessions: [session('ses_1')], count: 1 }),
      detail: () => ({ id: 'ses_1', title: 'Thread ses_1', channel: 'email', messages: [] }),
    })
    render(<ConversationsView state={stateWith()} onToast={() => {}} />)
    await waitFor(() => expect(screen.getByText('Thread ses_1')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Open conversation Thread ses_1' }))
    await waitFor(() => expect(screen.getByText(/No messages in this conversation/)).toBeTruthy())
  })

  // §5.2, defect 2
  it('re-reads the open transcript when the list shows new activity in it', async () => {
    let messages = DETAIL.messages
    let row = session('ses_1', { messageCount: 3, lastMessageAt: '2026-08-01T11:00:05Z' })
    stubFetch({
      list: () => ({ sessions: [row], count: 1 }),
      detail: () => ({ ...DETAIL, messages }),
    })
    const { rerender } = render(<ConversationsView state={stateWith()} onToast={() => {}} />)
    await waitFor(() => expect(screen.getByText('Thread ses_1')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Open conversation Thread ses_1' }))
    await waitFor(() => expect(screen.getByText('where is my order')).toBeTruthy())

    // The conversation moves on: one more message, so the row's activity moves too.
    messages = [...DETAIL.messages, { role: 'assistant', content: 'shipped today', ts: '2026-08-01T11:04:00Z' }]
    row = session('ses_1', { messageCount: 4, lastMessageAt: '2026-08-01T11:04:00Z' })
    rerender(<ConversationsView state={stateWith({ messages: 5 })} onToast={() => {}} />)

    // Before the fix the transcript was fetched once on click and never again, so an
    // operator watching a live conversation never saw this line.
    await waitFor(() => expect(screen.getByText('shipped today')).toBeTruthy())
    expect(screen.getByText('4 messages · ada@example.com')).toBeTruthy()
    // A re-read of the SAME conversation must not blank what is already correct.
    expect(screen.getByText('where is my order')).toBeTruthy()
  })

  // §5.2, defect 3 — the assertion that fails against the pre-fix view
  it('never shows one conversation’s messages under another’s selection', async () => {
    stubFetch({
      list: () => ({ sessions: [session('ses_1'), session('ses_2')], count: 2 }),
      // ses_2 never settles, which is the whole point: the gap between the click and
      // the response is exactly where the old view showed ses_1's transcript.
      detail: (id) => (id === 'ses_1' ? DETAIL : new Promise<Response>(() => {})),
    })
    render(<ConversationsView state={stateWith()} onToast={() => {}} />)
    await waitFor(() => expect(screen.getByText('Thread ses_1')).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: 'Open conversation Thread ses_1' }))
    await waitFor(() => expect(screen.getByText('where is my order')).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: 'Open conversation Thread ses_2' }))
    expect(screen.queryByText('where is my order')).toBeNull()
    expect(screen.queryByText('checking now')).toBeNull()
    // …and the panel says a read is in flight, for the row that was actually clicked.
    expect(screen.getByText(/Reading transcript/)).toBeTruthy()
    // The header comes from the LIST row, so the panel names the right conversation
    // from the first frame rather than inheriting the last one's title.
    expect(document.querySelector('.secrow .l')?.textContent).toContain('Thread ses_2')
  })

  // §5.2, defect 4
  it('closes the transcript', async () => {
    stubFetch({ list: () => ({ sessions: [session('ses_1')], count: 1 }), detail: () => DETAIL })
    render(<ConversationsView state={stateWith()} onToast={() => {}} />)
    await waitFor(() => expect(screen.getByText('Thread ses_1')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Open conversation Thread ses_1' }))
    await waitFor(() => expect(screen.getByText('where is my order')).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: 'Close conversation' }))
    expect(screen.queryByText('where is my order')).toBeNull()
    expect(screen.queryByText(/Reading transcript/)).toBeNull()
    // The list it was opened from is untouched.
    expect(screen.getByText('Thread ses_1')).toBeTruthy()
  })

  it('re-opening a closed conversation loads it again', async () => {
    const fetchMock = stubFetch({
      list: () => ({ sessions: [session('ses_1')], count: 1 }),
      detail: () => DETAIL,
    })
    render(<ConversationsView state={stateWith()} onToast={() => {}} />)
    await waitFor(() => expect(screen.getByText('Thread ses_1')).toBeTruthy())
    const open = () =>
      fireEvent.click(screen.getByRole('button', { name: 'Open conversation Thread ses_1' }))

    open()
    await waitFor(() => expect(screen.getByText('where is my order')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Close conversation' }))
    open()
    await waitFor(() => expect(screen.getByText('where is my order')).toBeTruthy())
    expect(urlsOf(fetchMock).filter((u) => u === '/sessions/ses_1').length).toBe(2)
  })

  it('reports a failed transcript in the panel as well as in a toast', async () => {
    stubFetch({
      list: () => ({ sessions: [session('ses_1')], count: 1 }),
      detail: () => json({ ok: false, error: { message: "No session 'ses_1'." } }, 404),
    })
    const toasts: string[] = []
    render(<ConversationsView state={stateWith()} onToast={(m) => toasts.push(m)} />)
    await waitFor(() => expect(screen.getByText('Thread ses_1')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Open conversation Thread ses_1' }))

    await waitFor(() => expect(toasts.length).toBe(1))
    expect(toasts[0]).toContain("No session 'ses_1'.")
    // A 2.6s toast is not a state. The panel keeps saying what happened, and the loader
    // does not sit there for ever pretending to read.
    expect(screen.getByText(/Transcript unavailable — No session 'ses_1'./)).toBeTruthy()
    expect(screen.queryByText(/Reading transcript/)).toBeNull()
    // …and the header does not claim to be reading something it has given up on.
    expect(document.querySelector('.secrow .r')?.textContent).toContain('unavailable')
    expect(screen.getByText('Thread ses_1')).toBeTruthy()
  })

  it('reads a failed session list as a failure, never as an empty workspace', async () => {
    stubFetch({ list: () => json({ ok: false, error: { message: 'session store unavailable' } }, 500) })
    render(<ConversationsView state={stateWith()} onToast={() => {}} />)

    await waitFor(() =>
      expect(screen.getByText(/Conversations unavailable — session store unavailable/)).toBeTruthy(),
    )
    // The bug class this console exists to prevent: an outage reported as "nothing
    // here yet", which reads as a healthy fresh install.
    expect(screen.queryByText(/No conversations yet/)).toBeNull()
    expect(screen.queryByText(/Loading conversations/)).toBeNull()
  })

  it('distinguishes an unauthenticated console from an outage', async () => {
    stubFetch({
      list: () =>
        json({ ok: false, error: { code: 'E_UNAUTHORIZED', message: 'Missing bearer token.' } }, 401),
    })
    render(<ConversationsView state={stateWith()} onToast={() => {}} />)

    // Only a 401 the stored credential explains carries the `'unauthorized'` sentinel
    // (lib/api.ts); it is not an outage and must not be phrased as one.
    await waitFor(() => expect(screen.getByText('Connect to load conversations.')).toBeTruthy())
    expect(screen.queryByText(/No conversations yet/)).toBeNull()
  })

  it('keeps the last good window on screen when a re-read fails, and says it is stale', async () => {
    let fail = false
    stubFetch({
      list: () =>
        fail
          ? json({ ok: false, error: { message: 'session store unavailable' } }, 500)
          : { sessions: [session('ses_1')], count: 1 },
    })
    const { rerender } = render(<ConversationsView state={stateWith()} onToast={() => {}} />)
    await waitFor(() => expect(screen.getByText('Thread ses_1')).toBeTruthy())

    fail = true
    rerender(<ConversationsView state={stateWith({ messages: 5 })} onToast={() => {}} />)

    await waitFor(() => expect(screen.getByText(/This list is the last good read/)).toBeTruthy())
    // usePoll's rule: a blip must not blank a view an operator is reading — but the
    // view has to be honest about being stale.
    expect(screen.getByText('Thread ses_1')).toBeTruthy()
  })

  it('renders titles as text, so a hostile session title cannot inject markup', async () => {
    const nasty = '<img src=x onerror=alert(1)>'
    stubFetch({ list: () => ({ sessions: [session('ses_1', { title: nasty })], count: 1 }) })
    render(<ConversationsView state={stateWith()} onToast={() => {}} />)

    await waitFor(() => expect(screen.getByText(nasty)).toBeTruthy())
    expect(document.querySelector('img')).toBeNull()
  })
})
