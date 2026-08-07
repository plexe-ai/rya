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
          new Response(JSON.stringify({ ok: false, error: { message: 'policy writes unsupported' } }), {
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

/**
 * Reach the confirmation the kill switch now sits behind (§5.15).
 *
 * Every policy write in this view goes through it, so the helper is what a test has
 * to say out loud: the row button opens a decision, it does not take one.
 */
async function openSwitch(label: RegExp) {
  await waitFor(() => expect(screen.getByRole('button', { name: label })).toBeTruthy())
  fireEvent.click(screen.getByRole('button', { name: label }))
  return screen.getByRole('dialog')
}

const typeReason = (text: string) =>
  fireEvent.change(screen.getByLabelText(/^Reason/), { target: { value: text } })

const pickTier = (tier: string) =>
  fireEvent.change(screen.getByLabelText('New permission'), { target: { value: tier } })

const puts = (calls: Call[]) => calls.filter((c) => c.method === 'PUT')

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
        override: {
          permission: 'disabled',
          ts: '2026-08-05T09:00:00Z',
          reason: 'vendor incident INC-4412 — refunds paused',
        },
      },
    ])
    render(<ToolsView state={stateWith([tool('email.send', 'allowed')])} onToast={() => {}} />)

    await waitFor(() => expect(screen.getByText('override')).toBeTruthy())
    // Both tiers are on screen: what was declared, and what is in force. (The tier
    // legend at the foot of the view names every tier too, hence getAllByText.)
    expect(screen.getAllByText('allowed').length).toBeGreaterThan(1)
    expect(screen.getAllByText('disabled').length).toBeGreaterThan(1)
    // ...and the override says WHY, not just that it exists.
    expect(screen.getByTitle(/INC-4412/)).toBeTruthy()
  })

  /**
   * §5.15, the whole of it in one test: pressing the switch DECIDES nothing.
   *
   * The old column wrote privileged, append-only policy state on the first click, in a
   * table a 6s poll keeps repainting — a mis-click refused every call to a live tool
   * and there was no step at which the operator could have noticed which row they were
   * on. The request must not exist until a human has confirmed it.
   */
  it('opens a confirmation and sends NO request until it is confirmed', async () => {
    const calls = stubFetch([
      { id: 'email.send', permission: 'allowed', effectivePermission: 'allowed' },
    ])
    render(<ToolsView state={stateWith([tool('email.send', 'allowed')])} onToast={() => {}} />)

    const dialog = await openSwitch(/Disable/)
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(puts(calls).length).toBe(0)
    // ...and still nothing after the microtasks a write would have queued.
    await waitFor(() => expect(screen.getByLabelText(/^Reason/)).toBeTruthy())
    expect(puts(calls).length).toBe(0)
  })

  it('writes nothing when the confirmation is cancelled', async () => {
    const calls = stubFetch([
      { id: 'email.send', permission: 'allowed', effectivePermission: 'allowed' },
    ])
    render(<ToolsView state={stateWith([tool('email.send', 'allowed')])} onToast={() => {}} />)

    await openSwitch(/Disable/)
    typeReason('changed my mind')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(screen.queryByRole('dialog')).toBeNull()
    expect(puts(calls).length).toBe(0)
  })

  it('writes nothing when the confirmation is dismissed with Escape', async () => {
    const calls = stubFetch([
      { id: 'email.send', permission: 'allowed', effectivePermission: 'allowed' },
    ])
    render(<ToolsView state={stateWith([tool('email.send', 'allowed')])} onToast={() => {}} />)

    await openSwitch(/Disable/)
    fireEvent.keyDown(document, { key: 'Escape' })

    expect(screen.queryByRole('dialog')).toBeNull()
    expect(puts(calls).length).toBe(0)
  })

  /**
   * The reason on the wire is the OPERATOR'S, not a constant.
   *
   * `_set_tool_permission` stores it verbatim in a versioned, attributed, append-only
   * policy record, and `GET /tools/log` reads that record back. The console used to
   * fabricate `'console kill switch'` there, which says nothing the log's own actor and
   * timestamp did not already say — and destroyed the only field that could have
   * distinguished an incident response from a mis-click.
   */
  it('PUTs the reason the operator typed, not a hardcoded one', async () => {
    const calls = stubFetch([
      { id: 'email.send', permission: 'allowed', effectivePermission: 'allowed' },
    ])
    const toasts: string[] = []
    render(<ToolsView state={stateWith([tool('email.send', 'allowed')])} onToast={(m) => toasts.push(m)} />)

    await openSwitch(/Disable/)
    typeReason('  vendor incident INC-4412 — refunds paused until the postmortem  ')
    fireEvent.click(screen.getByRole('button', { name: 'Write override' }))

    await waitFor(() => expect(toasts.length).toBe(1))
    const put = puts(calls)[0]!
    expect(put.url).toBe(`/agents/${AGENT}/tools/email.send/permission`)
    expect(JSON.parse(put.body!)).toEqual({
      permission: 'disabled',
      // Trimmed: leading whitespace in an audit record is noise that never washes out.
      reason: 'vendor incident INC-4412 — refunds paused until the postmortem',
    })
    expect(put.body).not.toContain('console kill switch')
    expect(toasts[0]).toContain('Disabled email.send')
    expect(toasts[0]).toContain('v7')
    expect(toasts[0]).toContain('effective immediately')
    // Refresh-after-write: the table re-reads the server's view rather than
    // patching one cell from the response.
    await waitFor(() => expect(calls.filter((c) => c.url.endsWith('/tools')).length).toBe(2))
    // The decision is made, so the dialog goes.
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  /**
   * The tier that was documented and unreachable. The legend at the foot of this view
   * explains that `approval_required` PAUSES the run for a human — the thing an
   * operator actually wants mid-incident — while the column offered a hardcoded
   * `disabled` and a clear, so the console described a capability it did not offer.
   */
  it('can put a tool behind approval, the tier the column could not reach', async () => {
    const calls = stubFetch([
      { id: 'billing.refund', permission: 'allowed', effectivePermission: 'allowed' },
    ])
    const toasts: string[] = []
    render(<ToolsView state={stateWith([tool('billing.refund', 'allowed')])} onToast={(m) => toasts.push(m)} />)

    await openSwitch(/Disable/)
    pickTier('approval_required')
    typeReason('every refund reviewed by hand while we audit the vendor')
    fireEvent.click(screen.getByRole('button', { name: 'Write override' }))

    await waitFor(() => expect(toasts.length).toBe(1))
    expect(JSON.parse(puts(calls)[0]!.body!)).toEqual({
      permission: 'approval_required',
      reason: 'every refund reviewed by hand while we audit the vendor',
    })
    expect(toasts[0]).toContain('billing.refund → approval_required')
  })

  it('offers exactly the four tiers the legend documents', async () => {
    stubFetch([{ id: 'email.send', permission: 'allowed', effectivePermission: 'allowed' }])
    render(<ToolsView state={stateWith([tool('email.send', 'allowed')])} onToast={() => {}} />)

    await openSwitch(/Disable/)
    const sel = screen.getByLabelText('New permission') as HTMLSelectElement
    expect([...sel.options].map((o) => o.value)).toEqual([
      'allowed',
      'read_only',
      'approval_required',
      'disabled',
    ])
    // `disabled` is preselected: this column is the kill switch, and killing the tool
    // is what an operator reaching for it usually means. The rest are one keystroke
    // away rather than absent.
    expect(sel.value).toBe('disabled')
  })

  it('will not write an override until a reason is actually typed', async () => {
    const calls = stubFetch([
      { id: 'email.send', permission: 'allowed', effectivePermission: 'allowed' },
    ])
    render(<ToolsView state={stateWith([tool('email.send', 'allowed')])} onToast={() => {}} />)

    await openSwitch(/Disable/)
    const confirm = () => screen.getByRole('button', { name: 'Write override' }) as HTMLButtonElement
    expect(confirm().disabled).toBe(true)
    fireEvent.click(confirm())
    expect(puts(calls).length).toBe(0)

    // Whitespace is not a reason — an audit record of `'   '` is worse than the
    // constant it replaced, because it looks deliberate.
    typeReason('   ')
    expect(confirm().disabled).toBe(true)

    typeReason('paused pending the postmortem')
    expect(confirm().disabled).toBe(false)
  })

  it('restores by CLEARING the override rather than writing the manifest value back', async () => {
    const calls = stubFetch([
      { id: 'email.send', permission: 'allowed', effectivePermission: 'disabled', override: { permission: 'disabled' } },
    ])
    const toasts: string[] = []
    render(<ToolsView state={stateWith([tool('email.send', 'allowed')])} onToast={(m) => toasts.push(m)} />)

    await openSwitch(/Restore/)
    // Confirmed, because re-enabling a tool somebody deliberately killed is itself
    // consequential — quite possibly mid-incident. But no reason is asked for: the
    // server drops the record rather than annotating it, so a reason typed here would
    // go nowhere.
    expect(screen.queryByLabelText(/^Reason/)).toBeNull()
    expect(puts(calls).length).toBe(0)

    fireEvent.click(screen.getByRole('button', { name: 'Drop override' }))
    await waitFor(() => expect(toasts.length).toBe(1))
    expect(JSON.parse(puts(calls)[0]!.body!)).toEqual({ clear: true })
    expect(toasts[0]).toContain('Restored email.send')
  })

  it('encodes a tool id with a slash into the path', async () => {
    const calls = stubFetch([{ id: 'a/b', permission: 'allowed', effectivePermission: 'allowed' }])
    render(<ToolsView state={stateWith([tool('a/b', 'allowed')])} onToast={() => {}} />)

    await openSwitch(/Disable/)
    typeReason('encoding check')
    fireEvent.click(screen.getByRole('button', { name: 'Write override' }))
    await waitFor(() => expect(puts(calls).length).toBe(1))
    expect(puts(calls)[0]!.url).toBe(`/agents/${AGENT}/tools/a%2Fb/permission`)
  })

  it('surfaces a failed write as a toast, and keeps the typed reason to retry with', async () => {
    stubFetch([{ id: 'email.send', permission: 'allowed', effectivePermission: 'allowed' }], { failPut: true })
    const toasts: string[] = []
    render(<ToolsView state={stateWith([tool('email.send', 'allowed')])} onToast={(m) => toasts.push(m)} />)

    await openSwitch(/Disable/)
    typeReason('vendor incident INC-4412')
    fireEvent.click(screen.getByRole('button', { name: 'Write override' }))

    await waitFor(() => expect(toasts[0]).toMatch(/^Error —/))
    // The dialog survives the failure: discarding a sentence the operator has just
    // composed, because the server 501'd, teaches them to type '.' next time.
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect((screen.getByLabelText(/^Reason/) as HTMLTextAreaElement).value).toBe('vendor incident INC-4412')
    await waitFor(() =>
      expect((screen.getByRole('button', { name: 'Write override' }) as HTMLButtonElement).disabled).toBe(
        false,
      ),
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
