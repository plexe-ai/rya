import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { ToolsView } from './Tools'
import type { ConsoleState, Tool } from '../lib/types'

// `fetch` is stubbed rather than `lib/api` mocked, because the URL and the METHOD
// are part of what this view has to get right: the kill switch is a PUT to an
// agent-PREFIXED path, and an unprefixed spelling would 400 with
// E_AGENT_AMBIGUOUS the day the workspace serves a second agent.

const AGENT = 'support-agent'

/** Only the fields ToolsView reads. */
const stateWith = (tools: unknown[]) =>
  ({ agent: { name: AGENT }, tools } as unknown as ConsoleState)

const tool = (id: string, permission: Tool['permission'], extra: Record<string, unknown> = {}) => ({
  id,
  permission,
  calls: 3,
  externalSideEffects: true,
  requiredSecrets: ['SMTP_URL'],
  ...extra,
})

const json = (body: unknown) =>
  Promise.resolve(
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    }),
  )

interface Call {
  url: string
  method: string
  body: string | null
}

/** Records every request and answers the two endpoints this view uses. */
function stubFetch(switches: unknown[], opts: { failList?: boolean; failPut?: boolean } = {}) {
  const calls: Call[] = []
  const fn = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    calls.push({ url, method: init?.method ?? 'GET', body: (init?.body as string) ?? null })
    if (url.includes('/permission')) {
      if (opts.failPut)
        return Promise.resolve(
          new Response(JSON.stringify({ detail: { message: 'policy writes unsupported' } }), {
            status: 501,
            headers: { 'content-type': 'application/json' },
          }),
        )
      return json({ ok: true, tool: 'x', permission: 'disabled', version: 7 })
    }
    if (opts.failList) return Promise.resolve(new Response('{}', { status: 503 }))
    return json({ agent: AGENT, tools: switches })
  })
  vi.stubGlobal('fetch', fn)
  return calls
}

describe('ToolsView', () => {
  beforeEach(() => localStorage.setItem('rya_token', 'test-token'))
  afterEach(() => vi.unstubAllGlobals())

  it('renders the enriched table once effective permissions load', async () => {
    stubFetch([
      { id: 'email.send', permission: 'approval_required', effectivePermission: 'approval_required' },
    ])
    render(<ToolsView state={stateWith([tool('email.send', 'approval_required')])} onToast={() => {}} />)

    await waitFor(() => expect(screen.getByRole('button', { name: /Disable/ })).toBeTruthy())
    expect(screen.getByText('email.send')).toBeTruthy()
    expect(screen.getByText('Effective')).toBeTruthy()
    expect(screen.getByText('external')).toBeTruthy()
    expect(screen.getByText('3')).toBeTruthy()
  })

  it('reads the tool list from `state` and the switch column from the agent-prefixed /tools', async () => {
    const calls = stubFetch([{ id: 'email.send', permission: 'allowed', effectivePermission: 'allowed' }])
    render(<ToolsView state={stateWith([tool('email.send', 'allowed')])} onToast={() => {}} />)

    await waitFor(() => expect(calls.length).toBe(1))
    expect(calls[0]!.url).toBe(`/agents/${AGENT}/tools`)
    expect(calls[0]!.method).toBe('GET')
  })

  it('names the tier a tool is in, and what that tier does at call time', async () => {
    stubFetch([{ id: 'email.send', permission: 'disabled', effectivePermission: 'disabled' }])
    render(<ToolsView state={stateWith([tool('email.send', 'disabled')])} onToast={() => {}} />)

    // The legend must say a disabled tool is REFUSED, not hidden — the switch is
    // runtime enforcement, not a hint dropped into the prompt.
    await waitFor(() => expect(screen.getByText(/REFUSED by the runtime/)).toBeTruthy())
    expect(screen.getByText(/run PAUSES at this call/)).toBeTruthy()
  })

  it('marks a runtime override so the manifest value is not mistaken for the live one', async () => {
    stubFetch([
      {
        id: 'email.send',
        permission: 'allowed',
        effectivePermission: 'disabled',
        override: { permission: 'disabled', ts: '2026-08-05T09:00:00Z', reason: 'console kill switch' },
      },
    ])
    render(<ToolsView state={stateWith([tool('email.send', 'allowed')])} onToast={() => {}} />)

    await waitFor(() => expect(screen.getByText('override')).toBeTruthy())
    // Both tiers are on screen: what was declared, and what is in force. (The tier
    // legend at the foot of the view names every tier too, hence getAllByText.)
    expect(screen.getAllByText('allowed').length).toBeGreaterThan(1)
    expect(screen.getAllByText('disabled').length).toBeGreaterThan(1)
    // ...and the override says WHY, not just that it exists.
    expect(screen.getByTitle(/console kill switch/)).toBeTruthy()
  })

  it('PUTs the kill switch to the agent-prefixed path and toasts the result', async () => {
    const calls = stubFetch([
      { id: 'email.send', permission: 'allowed', effectivePermission: 'allowed' },
    ])
    const toasts: string[] = []
    render(<ToolsView state={stateWith([tool('email.send', 'allowed')])} onToast={(m) => toasts.push(m)} />)

    await waitFor(() => expect(screen.getByRole('button', { name: /Disable/ })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /Disable/ }))

    await waitFor(() => expect(toasts.length).toBe(1))
    const put = calls.find((c) => c.method === 'PUT')!
    expect(put.url).toBe(`/agents/${AGENT}/tools/email.send/permission`)
    expect(JSON.parse(put.body!)).toEqual({ permission: 'disabled', reason: 'console kill switch' })
    expect(toasts[0]).toContain('Disabled email.send')
    expect(toasts[0]).toContain('v7')
    expect(toasts[0]).toContain('effective immediately')
    // Refresh-after-write: the table re-reads the server's view rather than
    // patching one cell from the response.
    await waitFor(() => expect(calls.filter((c) => c.url.endsWith('/tools')).length).toBe(2))
  })

  it('restores by CLEARING the override rather than writing the manifest value back', async () => {
    const calls = stubFetch([
      { id: 'email.send', permission: 'allowed', effectivePermission: 'disabled', override: { permission: 'disabled' } },
    ])
    const toasts: string[] = []
    render(<ToolsView state={stateWith([tool('email.send', 'allowed')])} onToast={(m) => toasts.push(m)} />)

    await waitFor(() => expect(screen.getByRole('button', { name: /Restore/ })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /Restore/ }))

    await waitFor(() => expect(toasts.length).toBe(1))
    const put = calls.find((c) => c.method === 'PUT')!
    expect(JSON.parse(put.body!)).toEqual({ clear: true })
    expect(toasts[0]).toContain('Restored email.send')
  })

  it('encodes a tool id with a slash into the path', async () => {
    const calls = stubFetch([{ id: 'a/b', permission: 'allowed', effectivePermission: 'allowed' }])
    render(<ToolsView state={stateWith([tool('a/b', 'allowed')])} onToast={() => {}} />)

    await waitFor(() => expect(screen.getByRole('button', { name: /Disable/ })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /Disable/ }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    expect(calls.find((c) => c.method === 'PUT')!.url).toBe(`/agents/${AGENT}/tools/a%2Fb/permission`)
  })

  it('surfaces a failed write as a toast and leaves the button usable', async () => {
    stubFetch([{ id: 'email.send', permission: 'allowed', effectivePermission: 'allowed' }], { failPut: true })
    const toasts: string[] = []
    render(<ToolsView state={stateWith([tool('email.send', 'allowed')])} onToast={(m) => toasts.push(m)} />)

    await waitFor(() => expect(screen.getByRole('button', { name: /Disable/ })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /Disable/ }))

    await waitFor(() => expect(toasts[0]).toMatch(/^Error —/))
    await waitFor(() =>
      expect((screen.getByRole('button', { name: /Disable/ }) as HTMLButtonElement).disabled).toBe(false),
    )
  })

  it('keeps the manifest table and says so when effective permissions cannot be read', async () => {
    stubFetch([], { failList: true })
    render(<ToolsView state={stateWith([tool('email.send', 'approval_required')])} onToast={() => {}} />)

    await waitFor(() => expect(screen.getByText(/kill switches are unavailable/)).toBeTruthy())
    // The snapshot's own columns are still true, so they stay.
    expect(screen.getByText('email.send')).toBeTruthy()
    expect(screen.getByText('SMTP_URL')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Disable/ })).toBeNull()
  })

  it('reads an agent with no tools as an ordinary state', async () => {
    stubFetch([])
    render(<ToolsView state={stateWith([])} onToast={() => {}} />)
    await waitFor(() => expect(screen.getByText(/No tools declared/)).toBeTruthy())
  })

  it('flags a mock implementation so demo data is not read as real IO', async () => {
    stubFetch([{ id: 'crm.lookup', permission: 'allowed', effectivePermission: 'allowed' }])
    render(
      <ToolsView
        state={stateWith([tool('crm.lookup', 'allowed', { mockImpl: true })])}
        onToast={() => {}}
      />,
    )
    await waitFor(() => expect(screen.getByText('mock')).toBeTruthy())
  })

  /**
   * The hazard this view exists to avoid. In the legacy console the enriched
   * kill-switch column was cached in a module global (`TOOL_EFF`) precisely so the 6s
   * poll could not rebuild the tbody underneath an operator's cursor. Here the fix is
   * structural: the rows are keyed by tool id, so a poll that adds a tool and changes
   * another's call count re-uses the existing `<tr>`s and the button an operator is
   * about to press stays exactly where it was.
   */
  it('keeps the kill-switch column and the very same button node across a poll', async () => {
    stubFetch([
      { id: 'email.send', permission: 'allowed', effectivePermission: 'allowed' },
      { id: 'crm.lookup', permission: 'read_only', effectivePermission: 'read_only' },
    ])
    const { rerender } = render(
      <ToolsView
        state={stateWith([tool('email.send', 'allowed'), tool('crm.lookup', 'read_only')])}
        onToast={() => {}}
      />,
    )

    await waitFor(() => expect(screen.getAllByRole('button', { name: /Disable/ }).length).toBe(2))
    const before = screen.getAllByRole('button', { name: /Disable/ })[0]

    // A poll lands: a new tool appears and an existing row's counter moves.
    rerender(
      <ToolsView
        state={stateWith([
          tool('email.send', 'allowed', { calls: 9 }),
          tool('crm.lookup', 'read_only'),
          tool('billing.refund', 'approval_required'),
        ])}
        onToast={() => {}}
      />,
    )

    const after = screen.getAllByRole('button', { name: /Disable/ })
    // Same DOM node, not a rebuilt one — that is what the key buys.
    expect(after[0]).toBe(before)
    // The new tool rendered, and the enriched column survived for it too.
    expect(screen.getByText('billing.refund')).toBeTruthy()
    expect(screen.getByText('9')).toBeTruthy()
    expect(screen.getByText('Kill switch')).toBeTruthy()
  })

  it('renders a hostile tool id as text', async () => {
    const nasty = '<img src=x onerror=alert(1)>'
    stubFetch([{ id: nasty, permission: 'allowed', effectivePermission: 'allowed' }])
    render(<ToolsView state={stateWith([tool(nasty, 'allowed')])} onToast={() => {}} />)
    await waitFor(() => expect(screen.getByText(nasty)).toBeTruthy())
    expect(document.querySelector('img')).toBeNull()
  })
})
