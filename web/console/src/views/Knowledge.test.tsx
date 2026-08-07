import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { KnowledgeView } from './Knowledge'
import type { ConsoleState } from '../lib/types'

// `fetch` is stubbed rather than `lib/api` mocked so the search's URL, method and
// body are all pinned: the search is agent-PREFIXED, and the unprefixed spelling
// would 400 with E_AGENT_AMBIGUOUS on a two-agent workspace.

const AGENT = 'support-agent'

const doc = (over: Record<string, unknown> = {}) => ({
  id: 'doc_1',
  source: 'handbook.md',
  chunks: 4,
  chars: 2480,
  createdAt: '2026-08-01T09:30:00Z',
  ...over,
})

const stateWith = (documents: unknown[], chunks = 0) =>
  ({ agent: { name: AGENT }, knowledge: { documents, chunks } } as unknown as ConsoleState)

const hit = (over: Record<string, unknown> = {}) => ({
  text: 'Refunds are approved by a human when the amount exceeds fifty dollars.',
  source: 'handbook.md',
  docId: 'doc_1',
  _score: 0.8421,
  ...over,
})

interface Call {
  url: string
  method: string
  body: string | null
}

// Both `hits` and `fail` may be given as a function of the call INDEX. The §5.16
// tests turn on the second response differing from the first — a corpus that grew
// between two identical queries, and a retry that fails again — and a stub that
// answers every call the same way cannot tell a re-fetch from a re-render.
function stubFetch(
  hits: unknown[] | ((n: number) => unknown[]),
  opts: { fail?: boolean | ((n: number) => boolean) } = {},
) {
  const calls: Call[] = []
  const fn = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const n = calls.length
    calls.push({
      url: String(input),
      method: init?.method ?? 'GET',
      body: (init?.body as string) ?? null,
    })
    if (typeof opts.fail === 'function' ? opts.fail(n) : opts.fail)
      return Promise.resolve(
        new Response(JSON.stringify({ ok: false, error: { message: 'embedding backend unreachable' } }), {
          status: 503,
          headers: { 'content-type': 'application/json' },
        }),
      )
    return Promise.resolve(
      new Response(JSON.stringify({ query: 'refund', hits: typeof hits === 'function' ? hits(n) : hits }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    )
  })
  vi.stubGlobal('fetch', fn)
  return calls
}

const search = (q: string) => {
  fireEvent.change(screen.getByLabelText('Search the knowledge base'), { target: { value: q } })
  fireEvent.click(screen.getByRole('button', { name: /Search/ }))
}

describe('KnowledgeView', () => {
  beforeEach(() => localStorage.setItem('rya_token', 'test-token'))
  afterEach(() => vi.unstubAllGlobals())

  // The four tests below are assertions about the FIRST paint, but `useLoad` still
  // fires on mount and — with an empty query — resolves to `null` without a request.
  // That resolution sets three pieces of state one microtask after the synchronous
  // test body has already finished, which is what React reports as "an update was not
  // wrapped in act(...)". `findBy*` awaits the settle inside act, so the warning goes
  // away without weakening what is being asserted.

  it('lists the ingested documents from state, with no request of its own', async () => {
    const calls = stubFetch([])
    render(<KnowledgeView state={stateWith([doc()], 12)} onToast={() => {}} />)

    expect(await screen.findByText('handbook.md')).toBeTruthy()
    expect(screen.getByText('4')).toBeTruthy()
    expect(screen.getByText('2480 ch')).toBeTruthy()
    expect(screen.getByText('1 docs · 12 chunks · vector + lexical recall')).toBeTruthy()
    // Landing on the view asks the server nothing: the corpus is already in the poll.
    expect(calls.length).toBe(0)
  })

  it('falls back to the document id when it was ingested without a source', async () => {
    stubFetch([])
    render(<KnowledgeView state={stateWith([doc({ source: null })], 4)} onToast={() => {}} />)
    expect(await screen.findByText('doc_1')).toBeTruthy()
  })

  it('reads an empty corpus as an ordinary state, naming the call that fills it', async () => {
    stubFetch([])
    render(<KnowledgeView state={stateWith([], 0)} onToast={() => {}} />)
    expect(await screen.findByText(/No documents/)).toBeTruthy()
    expect(screen.getByText(/ctx\.knowledge\.add/)).toBeTruthy()
  })

  it('treats an absent knowledge block the same as an empty one', async () => {
    stubFetch([])
    render(<KnowledgeView state={{ agent: { name: AGENT } } as unknown as ConsoleState} onToast={() => {}} />)
    expect(await screen.findByText(/No documents/)).toBeTruthy()
    expect(screen.getByText('0 docs · 0 chunks · vector + lexical recall')).toBeTruthy()
  })

  it('POSTs the search to the agent-prefixed path and renders the hits', async () => {
    const calls = stubFetch([hit(), hit({ docId: 'doc_2', source: 'faq.md', _score: 0.5 })])
    render(<KnowledgeView state={stateWith([doc()], 12)} onToast={() => {}} />)

    search('refund')

    await waitFor(() => expect(screen.getByText('Top matches')).toBeTruthy())
    expect(calls.length).toBe(1)
    expect(calls[0]!.url).toBe(`/agents/${AGENT}/knowledge/search`)
    expect(calls[0]!.method).toBe('POST')
    expect(JSON.parse(calls[0]!.body!)).toEqual({ query: 'refund', limit: 5 })

    expect(screen.getByText('0.842')).toBeTruthy() // score, fixed to 3dp
    expect(screen.getByText('0.500')).toBeTruthy()
    // Both hits quote the same chunk text; each is its own row.
    expect(screen.getAllByText(/Refunds are approved by a human/).length).toBe(2)
    expect(screen.getByText('2 · query: refund')).toBeTruthy()
  })

  /** The point of the whole exercise: an empty result set is an answer. */
  it('reads an empty result as "no matches" and not as an error', async () => {
    stubFetch([])
    const toasts: string[] = []
    render(<KnowledgeView state={stateWith([doc()], 12)} onToast={(m) => toasts.push(m)} />)

    search('zebra')

    await waitFor(() => expect(screen.getByText(/No matches for/)).toBeTruthy())
    expect(screen.getByText(/zebra/)).toBeTruthy()
    expect(screen.queryByText('Top matches')).toBeNull()
    // Nothing failed, so nothing is announced as a failure.
    expect(toasts).toEqual([])
    // ...and the corpus is still listed underneath.
    expect(screen.getByText('handbook.md')).toBeTruthy()
  })

  it('does not search on an empty or whitespace-only query', async () => {
    const calls = stubFetch([hit()])
    render(<KnowledgeView state={stateWith([doc()], 12)} onToast={() => {}} />)

    search('   ')
    await waitFor(() => expect(screen.getByText('handbook.md')).toBeTruthy())
    expect(calls.length).toBe(0)
    expect(screen.queryByText(/No matches for/)).toBeNull()
  })

  it('searches on Enter as well as on the button', async () => {
    const calls = stubFetch([hit()])
    render(<KnowledgeView state={stateWith([doc()], 12)} onToast={() => {}} />)

    const box = screen.getByLabelText('Search the knowledge base')
    fireEvent.change(box, { target: { value: 'refund' } })
    fireEvent.keyDown(box, { key: 'Enter' })

    await waitFor(() => expect(calls.length).toBe(1))
    expect(JSON.parse(calls[0]!.body!)).toEqual({ query: 'refund', limit: 5 })
  })

  /**
   * §5.16, and the assertion that matters is the REQUEST COUNT rather than anything on
   * screen: the two responses here are identical, so a view that silently re-fetched
   * nothing would paint exactly like one that did.
   *
   * `submitted` was both the query text and the "go" signal. Setting state to the
   * value it already holds is a no-op, so `useLoad`'s deps never moved and the second
   * press produced no request, no spinner and no change at all.
   */
  it('re-runs an identical query instead of deduplicating the second press', async () => {
    const calls = stubFetch([hit()])
    render(<KnowledgeView state={stateWith([doc()], 12)} onToast={() => {}} />)

    search('refunds')
    // Settled, not merely sent: the button is disabled mid-flight, so clicking before
    // the first search lands would prove nothing about the second.
    expect(await screen.findByText('Top matches')).toBeTruthy()
    expect(calls.length).toBe(1)

    fireEvent.click(screen.getByRole('button', { name: /Search/ }))

    await waitFor(() => expect(calls.length).toBe(2))
    expect(JSON.parse(calls[1]!.body!)).toEqual({ query: 'refunds', limit: 5 })
    expect(calls[1]!.url).toBe(`/agents/${AGENT}/knowledge/search`)
  })

  it('re-runs on a second Enter with the query unchanged', async () => {
    const calls = stubFetch([hit()])
    render(<KnowledgeView state={stateWith([doc()], 12)} onToast={() => {}} />)

    const box = screen.getByLabelText('Search the knowledge base')
    fireEvent.change(box, { target: { value: 'refunds' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(await screen.findByText('Top matches')).toBeTruthy()

    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(calls.length).toBe(2))
  })

  /** The operator's actual sequence: search, ingest a document, search the same term. */
  it('shows the new answer when the same query is re-run over a grown corpus', async () => {
    const calls = stubFetch((n) => (n === 0 ? [] : [hit()]))
    render(<KnowledgeView state={stateWith([doc()], 12)} onToast={() => {}} />)

    search('refunds')
    expect(await screen.findByText(/No matches for/)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /Search/ }))

    expect(await screen.findByText('Top matches')).toBeTruthy()
    expect(calls.length).toBe(2)
    expect(screen.queryByText(/No matches for/)).toBeNull()
  })

  it('truncates a long chunk rather than dumping it into the row', async () => {
    const long = 'a'.repeat(500)
    stubFetch([hit({ text: long })])
    render(<KnowledgeView state={stateWith([doc()], 12)} onToast={() => {}} />)

    search('refund')
    await waitFor(() => expect(screen.getByText(`${'a'.repeat(300)}…`)).toBeTruthy())
  })

  it('toasts a failed search but keeps the document list on screen', async () => {
    stubFetch([], { fail: true })
    const toasts: string[] = []
    render(<KnowledgeView state={stateWith([doc()], 12)} onToast={(m) => toasts.push(m)} />)

    search('refund')

    await waitFor(() => expect(toasts.length).toBe(1))
    expect(toasts[0]).toMatch(/^Search failed —/)
    expect(toasts[0]).toContain('embedding backend unreachable')
    // A failed search says nothing about what is ingested.
    expect(screen.getByText('handbook.md')).toBeTruthy()
    expect(screen.queryByText(/No matches for/)).toBeNull()
  })

  /**
   * §5.16's second instance. The toast effect was keyed on `[error, submitted]`, so a
   * retry that failed again with the identical message on the identical query changed
   * neither dep and the console swallowed the second failure entirely — which reads as
   * "the button did nothing", the same complaint from the other end.
   */
  it('announces a repeated failure and not only the first one', async () => {
    const calls = stubFetch([], { fail: true })
    const toasts: string[] = []
    render(<KnowledgeView state={stateWith([doc()], 12)} onToast={(m) => toasts.push(m)} />)

    search('refunds')
    await waitFor(() => expect(toasts.length).toBe(1))

    fireEvent.click(screen.getByRole('button', { name: /Search/ }))

    await waitFor(() => expect(calls.length).toBe(2))
    await waitFor(() => expect(toasts.length).toBe(2))
    // Same words, deliberately: identical text is exactly the case the old deps ate.
    expect(toasts[1]).toBe(toasts[0])
  })

  /**
   * The trap in fixing the above. `useLoad` clears `error` only on SUCCESS — `reload`
   * opens with `setLoading(true)` and leaves the standing failure in place until the
   * new attempt settles — so an effect keyed on anything that changes at SUBMIT time
   * re-announces the PREVIOUS failure the instant the next search begins, for a request
   * that has not been made yet. It is why the trigger is the settle and not the submit.
   */
  it('does not re-announce the previous failure when the next search starts', async () => {
    const calls = stubFetch([hit()], { fail: (n) => n === 0 })
    const toasts: string[] = []
    render(<KnowledgeView state={stateWith([doc()], 12)} onToast={(m) => toasts.push(m)} />)

    search('refunds')
    await waitFor(() => expect(toasts.length).toBe(1))

    search('shipping')

    expect(await screen.findByText('Top matches')).toBeTruthy()
    expect(calls.length).toBe(2)
    // The second search succeeded, so there was never a second thing to announce.
    expect(toasts.length).toBe(1)
  })

  /**
   * The regression this migration deletes.
   *
   * The legacy console read the query straight off the DOM and rebuilt this panel
   * from an HTML string every 6s, so it had to sniff `document.activeElement` to
   * avoid eating a half-typed query. The input is React state here, so a poll
   * arriving as new props cannot touch its value, its focus or its caret.
   */
  it('keeps focus, value and caret in the search box when polled data arrives', async () => {
    stubFetch([hit()])
    const { rerender } = render(<KnowledgeView state={stateWith([doc()], 12)} onToast={() => {}} />)
    // Let the on-entry load settle (it resolves to null without a request) so the
    // assertions below are about the poll and nothing else.
    await waitFor(() => expect(screen.getByText('handbook.md')).toBeTruthy())

    const box = screen.getByLabelText('Search the knowledge base') as HTMLInputElement
    box.focus()
    fireEvent.change(box, { target: { value: 'refund' } })
    box.setSelectionRange(3, 3)
    expect(document.activeElement).toBe(box)

    // A poll lands: a new document was ingested and the chunk count moved.
    rerender(
      <KnowledgeView
        state={stateWith([doc(), doc({ id: 'doc_2', source: 'faq.md' })], 19)}
        onToast={() => {}}
      />,
    )

    const after = screen.getByLabelText('Search the knowledge base') as HTMLInputElement
    expect(document.activeElement).toBe(after)
    expect(after.value).toBe('refund')
    expect(after.selectionStart).toBe(3)
    // The new data really did land.
    expect(screen.getByText('faq.md')).toBeTruthy()
    expect(screen.getByText('2 docs · 19 chunks · vector + lexical recall')).toBeTruthy()
  })

  it('keeps the rendered hits across a poll', async () => {
    stubFetch([hit()])
    const { rerender } = render(<KnowledgeView state={stateWith([doc()], 12)} onToast={() => {}} />)

    search('refund')
    await waitFor(() => expect(screen.getByText('Top matches')).toBeTruthy())

    rerender(<KnowledgeView state={stateWith([doc(), doc({ id: 'doc_2' })], 19)} onToast={() => {}} />)
    expect(screen.getByText('Top matches')).toBeTruthy()
    expect(screen.getByText('0.842')).toBeTruthy()
  })

  it('renders hit text and a hostile source as text', async () => {
    const nasty = '<img src=x onerror=alert(1)>'
    stubFetch([hit({ source: nasty })])
    render(<KnowledgeView state={stateWith([doc()], 12)} onToast={() => {}} />)

    search('refund')
    await waitFor(() => expect(screen.getByText(nasty)).toBeTruthy())
    expect(document.querySelector('img')).toBeNull()
  })
})
