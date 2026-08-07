import { describe, expect, it, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { TeamView } from './Team'
import type { ConsoleState } from '../lib/types'

/**
 * Team & access is the one view with two credentials in play, so most of what is
 * pinned here is which state the page lands in — signed out, single-tenant, member
 * rather than owner — because each of those is an ANSWER and reading any of them as
 * an error (or as an empty workspace) is the specific way this view goes wrong.
 */

/** Only the fields TeamView reads. */
const stateWith = (over: Record<string, unknown> = {}) =>
  ({
    runtime: { store: 'postgres', llmProvider: 'anthropic', multiTenant: true },
    viewer: { workspace: 'Acme', workspaceId: 'ws_1', mode: 'multi-tenant' },
    ...over,
  }) as unknown as ConsoleState

interface Call {
  url: string
  method: string
  body?: string
  auth?: string
}

/** Route table keyed `"<METHOD> <path>"`; anything unrouted is a loud 404. */
function stubFetch(routes: Record<string, { status?: number; body?: unknown }>) {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init: RequestInit = {}) => {
      const method = init.method || 'GET'
      const headers = (init.headers || {}) as Record<string, string>
      calls.push({ url: String(url), method, body: init.body as string, auth: headers.Authorization })
      const r = routes[`${method} ${String(url)}`] ?? {
        status: 404,
        body: { ok: false, error: { message: `unrouted ${method} ${url}` } },
      }
      const status = r.status ?? 200
      return Promise.resolve({
        ok: status < 400,
        status,
        json: () => Promise.resolve(r.body ?? {}),
      })
    }),
  )
  return calls
}

const MEMBERS = {
  body: {
    members: [
      { email: 'ada@example.com', role: 'owner', claimed: true, invitedAt: '2026-08-01T10:00:00Z' },
      { email: 'grace@example.com', role: 'member', claimed: false, invitedAt: '2026-08-04T10:00:00Z' },
    ],
  },
}
const KEYS = {
  body: { keys: [{ id: 'key_abc', label: 'ci-deploy', createdAt: '2026-08-02T09:00:00Z', createdBy: 'usr_1' }] },
}

afterEach(() => vi.unstubAllGlobals())

describe('TeamView', () => {
  it('renders members and key metadata for an owner', async () => {
    localStorage.setItem('rya_session', 'sess_1')
    const calls = stubFetch({
      'GET /v1/workspaces/ws_1/members': MEMBERS,
      'GET /v1/workspaces/ws_1/keys': KEYS,
    })

    render(<TeamView state={stateWith()} onToast={() => {}} />)

    expect(await screen.findByText('ada@example.com')).toBeTruthy()
    expect(screen.getByText('grace@example.com')).toBeTruthy()
    // An unclaimed invite is a distinct state from an active member.
    expect(screen.getByText('active')).toBeTruthy()
    expect(screen.getByText('invited')).toBeTruthy()
    // Key METADATA only — id and label, never anything that looks like a value.
    expect(screen.getByText('ci-deploy')).toBeTruthy()
    expect(screen.getByText('key_abc')).toBeTruthy()
    expect(screen.getByText(/shown once, at mint time/)).toBeTruthy()

    // Both reads are session-authenticated, and the id used is `workspaceId` — not
    // the display name in `viewer.workspace`.
    expect(calls.every((c) => c.auth === 'Bearer sess_1')).toBe(true)
    expect(calls.map((c) => c.url)).toEqual([
      '/v1/workspaces/ws_1/members',
      '/v1/workspaces/ws_1/keys',
    ])
  })

  /**
   * Holding a workspace API key without a user session is the NORMAL state for
   * anyone who pasted a key into the auth modal. It must read as "sign in" — not as
   * an outage, and above all not as a workspace with no members in it.
   */
  it('asks a keyed-but-not-signed-in operator to sign in, without fetching or crying outage', async () => {
    const calls = stubFetch({})

    render(<TeamView state={stateWith()} onToast={() => {}} />)

    expect(screen.getByText('Sign in to manage the team')).toBeTruthy()
    expect(screen.getByText(/Nothing is wrong with this workspace/)).toBeTruthy()
    expect(screen.queryByText(/unavailable/i)).toBeNull()
    expect(screen.queryByText(/error/i)).toBeNull()
    // No members table at all, empty or otherwise: "we cannot ask" is not "nobody".
    expect(screen.queryByText(/No members/i)).toBeNull()
    // `waitFor` also flushes the on-entry load, which must have asked for nothing.
    await waitFor(() => expect(calls).toEqual([]))
  })

  it('explains single-tenant mode instead of calling routes that would 400', async () => {
    localStorage.setItem('rya_session', 'sess_1')
    const calls = stubFetch({})

    render(
      <TeamView
        state={stateWith({ runtime: { store: 'sqlite', llmProvider: 'anthropic', multiTenant: false } })}
        onToast={() => {}}
      />,
    )

    expect(screen.getByText('Single-tenant runtime')).toBeTruthy()
    expect(screen.getByText(/no accounts, workspaces or invites/)).toBeTruthy()
    await waitFor(() => expect(calls).toEqual([]))
  })

  /** The key list is owner-only. A 403 is an answer about a role, not a failure. */
  it('reads a 403 on the owner-only key list as "not the owner"', async () => {
    localStorage.setItem('rya_session', 'sess_1')
    stubFetch({
      'GET /v1/workspaces/ws_1/members': MEMBERS,
      'GET /v1/workspaces/ws_1/keys': {
        status: 403,
        body: { ok: false, error: { code: 'E_UNAUTHORIZED', message: 'You are not the owner of this workspace.' } },
      },
    })

    render(<TeamView state={stateWith()} onToast={() => {}} />)

    expect(await screen.findByText(/not the owner of this workspace/)).toBeTruthy()
    expect(screen.queryByText(/unavailable/i)).toBeNull()
    // The roster only needs membership, so it still renders; the owner-only controls
    // are the only thing withheld.
    expect(screen.getByText('ada@example.com')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Invite/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /Revoke/ })).toBeNull()
  })

  it('posts the typed email to the members route when inviting', async () => {
    localStorage.setItem('rya_session', 'sess_1')
    const calls = stubFetch({
      'GET /v1/workspaces/ws_1/members': MEMBERS,
      'GET /v1/workspaces/ws_1/keys': KEYS,
      'POST /v1/workspaces/ws_1/members': { body: { ok: true, claimed: false } },
    })
    const toasts: string[] = []

    render(<TeamView state={stateWith()} onToast={(m) => toasts.push(m)} />)
    await screen.findByText('ada@example.com')

    const input = screen.getByLabelText('Invite a teammate by email') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'hopper@example.com' } })
    fireEvent.click(screen.getByRole('button', { name: /Invite/ }))

    await waitFor(() => expect(toasts.length).toBe(1))
    const post = calls.find((c) => c.method === 'POST')!
    expect(post.url).toBe('/v1/workspaces/ws_1/members')
    expect(JSON.parse(post.body!)).toEqual({ email: 'hopper@example.com' })
    expect(post.auth).toBe('Bearer sess_1')
    // An unclaimed email gets access at signup, and the toast says which happened.
    expect(toasts[0]).toMatch(/access starts at signup/)
    expect(input.value).toBe('')
  })

  /**
   * The plaintext exists for exactly one render. The store keeps a SHA-256 hash, so
   * if the UI does not say "this is the only time you will see it" there is no later
   * screen that can.
   */
  it('shows a freshly minted key once, with the shown-once warning', async () => {
    localStorage.setItem('rya_session', 'sess_1')
    stubFetch({
      'GET /v1/workspaces/ws_1/members': MEMBERS,
      'GET /v1/workspaces/ws_1/keys': KEYS,
      'POST /v1/workspaces/ws_1/keys': {
        body: { ok: true, workspace: { name: 'Acme' }, apiKey: 'rya_sk_live_secret' },
      },
    })

    render(<TeamView state={stateWith()} onToast={() => {}} />)
    await screen.findByText('ada@example.com')

    // Nothing secret is on screen before we mint one.
    expect(screen.queryByText('rya_sk_live_secret')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Mint a key/ }))

    expect(await screen.findByText('rya_sk_live_secret')).toBeTruthy()
    expect(screen.getByText(/only time it will ever be shown/)).toBeTruthy()
    expect(screen.getByText(/SHA-256 hash is stored/)).toBeTruthy()

    // Dismissing it takes the value off the page; the list still shows metadata only.
    fireEvent.click(screen.getByRole('button', { name: /Done, I copied it/ }))
    expect(screen.queryByText('rya_sk_live_secret')).toBeNull()
    expect(screen.getByText('ci-deploy')).toBeTruthy()
  })
})
