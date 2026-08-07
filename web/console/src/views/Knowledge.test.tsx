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

function stubFetch(hits: unknown[], opts: { fail?: boolean } = {}) {
  const calls: Call[] = []
  const fn = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({
      url: String(input),
      method: init?.method ?? 'GET',
      body: (init?.body as string) ?? null,
    })
    if (opts.fail)
      return Promise.resolve(
        new Response(JSON.stringify({ detail: { message: 'embedding backend unreachable' } }), {
          status: 503,
          headers: { 'content-type': 'application/json' },
        }),
      )
    return Promise.resolve(
      new Response(JSON.stringify({ query: 'refund', hits }), {
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

  it('lists the ingested documents from state, with no request of its own', () => {
    const calls = stubFetch([])
    render(<KnowledgeView state={stateWith([doc()], 12)} onToast={() => {}} />)

    expect(screen.getByText('handbook.md')).toBeTruthy()
    expect(screen.getByText('4')).toBeTruthy()
    expect(screen.getByText('2480 ch')).toBeTruthy()
    expect(screen.getByText('1 docs · 12 chunks · vector + lexical recall')).toBeTruthy()
    // Landing on the view asks the server nothing: the corpus is already in the poll.
    expect(calls.length).toBe(0)
  })

  it('falls back to the document id when it was ingested without a source', () => {
    stubFetch([])
    render(<KnowledgeView state={stateWith([doc({ source: null })], 4)} onToast={() => {}} />)
    expect(screen.getByText('doc_1')).toBeTruthy()
  })

  it('reads an empty corpus as an ordinary state, naming the call that fills it', () => {
    stubFetch([])
    render(<KnowledgeView state={stateWith([], 0)} onToast={() => {}} />)
    expect(screen.getByText(/No documents/)).toBeTruthy()
    expect(screen.getByText(/ctx\.knowledge\.add/)).toBeTruthy()
  })

  it('treats an absent knowledge block the same as an empty one', () => {
    stubFetch([])
    render(<KnowledgeView state={{ agent: { name: AGENT } } as unknown as ConsoleState} onToast={() => {}} />)
    expect(screen.getByText(/No documents/)).toBeTruthy()
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
