import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { ConversationsView } from './Conversations'
import type { SessionSummary } from './Conversations'
import type { ConsoleState } from '../lib/types'

const session = (id: string, over: Partial<SessionSummary> = {}): SessionSummary => ({
  id,
  title: `Thread ${id}`,
  channel: 'email',
  externalId: 'ada@example.com',
  messageCount: 2,
  lastMessageAt: '2026-08-01T11:00:00Z',
  ...over,
})

/** Only the fields this view reads; the rest of the aggregate is irrelevant here. */
const stateWith = (sessions: SessionSummary[]) =>
  ({ agent: { name: 'support-agent' }, sessions }) as unknown as ConsoleState

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

function stubFetch(handler: (url: string) => unknown) {
  const fn = vi.fn((input: RequestInfo | URL) =>
    Promise.resolve(
      new Response(JSON.stringify(handler(String(input))), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    ),
  )
  vi.stubGlobal('fetch', fn)
  return fn
}

describe('ConversationsView', () => {
  beforeEach(() => localStorage.setItem('rya_token', 'test-token'))
  afterEach(() => vi.unstubAllGlobals())

  it('lists the sessions carried by the /console aggregate', () => {
    stubFetch(() => ({}))
    render(<ConversationsView state={stateWith([session('ses_1'), session('ses_2')])} onToast={() => {}} />)
    expect(screen.getByText('Thread ses_1')).toBeTruthy()
    expect(screen.getByText('Thread ses_2')).toBeTruthy()
    expect(screen.getAllByText('ada@example.com').length).toBe(2)
  })

  it('reads an empty session list as an ordinary state, not a fault', () => {
    stubFetch(() => ({}))
    render(<ConversationsView state={stateWith([])} onToast={() => {}} />)
    expect(screen.getByText(/No conversations yet/)).toBeTruthy()
  })

  it('loads the transcript on demand from the workspace-scoped session route', async () => {
    const fetchMock = stubFetch(() => DETAIL)
    render(<ConversationsView state={stateWith([session('ses_1')])} onToast={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: 'Open conversation Thread ses_1' }))

    await waitFor(() => expect(screen.getByText('where is my order')).toBeTruthy())
    expect(screen.getByText('checking now')).toBeTruthy()
    expect(screen.getByText('run_9')).toBeTruthy()
    expect(screen.getByText('3 messages · ada@example.com')).toBeTruthy()

    // `/sessions/{id}` is NOT agent-prefixed: the session id already identifies
    // the agent, and the server serves no `/agents/{name}/sessions/{id}`.
    const url = String(fetchMock.mock.calls[0]?.[0])
    expect(url).toBe('/sessions/ses_1')
    expect(url).not.toContain('/agents/')
  })

  it('places the roles on the right sides of the thread', async () => {
    stubFetch(() => DETAIL)
    render(<ConversationsView state={stateWith([session('ses_1')])} onToast={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Open conversation Thread ses_1' }))
    await waitFor(() => expect(screen.getByText('where is my order')).toBeTruthy())

    const bubble = (text: string) => screen.getByText(text).closest('.msg')!
    expect(bubble('where is my order').className).toContain('in')
    expect(bubble('checking now').className).toContain('out')
    // tool/system messages are centred asides, not either party's speech
    expect(bubble('orders.lookup ok').className).toContain('sys')
  })

  it('says so when a session exists but has no messages', async () => {
    stubFetch(() => ({ id: 'ses_1', title: 'Thread ses_1', channel: 'email', messages: [] }))
    render(<ConversationsView state={stateWith([session('ses_1')])} onToast={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Open conversation Thread ses_1' }))
    await waitFor(() => expect(screen.getByText(/No messages in this conversation/)).toBeTruthy())
  })

  it('toasts a failed transcript instead of blanking the list', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ detail: { message: "No session 'ses_1'." } }), { status: 404 }),
        ),
      ),
    )
    const toasts: string[] = []
    render(<ConversationsView state={stateWith([session('ses_1')])} onToast={(m) => toasts.push(m)} />)
    fireEvent.click(screen.getByRole('button', { name: 'Open conversation Thread ses_1' }))

    await waitFor(() => expect(toasts.length).toBe(1))
    expect(toasts[0]).toContain("No session 'ses_1'.")
    expect(screen.getByText('Thread ses_1')).toBeTruthy()
  })

  it('renders titles as text, so a hostile session title cannot inject markup', () => {
    stubFetch(() => ({}))
    const nasty = '<img src=x onerror=alert(1)>'
    render(<ConversationsView state={stateWith([session('ses_1', { title: nasty })])} onToast={() => {}} />)
    expect(screen.getByText(nasty)).toBeTruthy()
    expect(document.querySelector('img')).toBeNull()
  })
})
