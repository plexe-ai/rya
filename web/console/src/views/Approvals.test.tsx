import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ApprovalsView } from './Approvals'
import type { Approval, ConsoleState } from '../lib/types'

/**
 * The only irreversible action in the console, and it had no test.
 *
 * Approving executes a real external side effect and resumes a real run. What the
 * operator can SEE before pressing the button is therefore the whole product feature,
 * not presentation — so most of what is asserted here is about rendering, and the
 * sharpest assertions are negative: the amount must not be missing, the agent must not
 * be misattributed, the icon must not claim the action is something it is not.
 */

const approval = (over: Partial<Approval> = {}): Approval => ({
  id: 'apr_1',
  title: 'Refund order #4417',
  runId: 'run_88',
  body: 'Customer reports a duplicate charge. Policy allows refunds under 30 days.',
  action: { tool: 'payments.refund', input: { amount: 500000, currency: 'GBP', orderId: '4417' } },
  agent: 'support-agent',
  ...over,
})

const stateWith = (approvals: Approval[]) => ({ approvals }) as unknown as ConsoleState

function renderView(approvals: Approval[], agent: string | null = 'support-agent') {
  const onToast = vi.fn()
  const onResolved = vi.fn()
  render(
    <ApprovalsView
      state={stateWith(approvals)}
      agent={agent}
      onToast={onToast}
      onResolved={onResolved}
    />,
  )
  return { onToast, onResolved }
}

function stubFetch(res: () => Response) {
  const fn = vi.fn((_i: RequestInfo | URL, _init?: RequestInit) => Promise.resolve(res()))
  vi.stubGlobal('fetch', fn)
  return fn
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

afterEach(() => vi.unstubAllGlobals())

describe('what the operator can see before approving', () => {
  it('renders the action arguments, which is the thing being consented to', () => {
    renderView([approval()])
    // The audit's example: a £500,000 refund that rendered as a title and a mail icon.
    // `500000` must be on screen, because approving without it is a signature on an
    // unread document.
    const args = screen.getByLabelText('Arguments for payments.refund')
    expect(args.textContent).toContain('500000')
    expect(args.textContent).toContain('GBP')
    expect(args.textContent).toContain('4417')
  })

  it('renders the body the server has always shipped', () => {
    renderView([approval()])
    expect(screen.getByText(/duplicate charge/)).toBeTruthy()
  })

  it('names the real tool rather than assuming an email', () => {
    renderView([approval()])
    expect(screen.getByText('payments.refund')).toBeTruthy()
    // The old view hardcoded a Mail icon for every approval in the product. A generic
    // gate icon can be uninformative; a mail icon on a refund is wrong.
    expect(document.querySelector('.approw .aic svg')).toBeTruthy()
  })

  it('says so explicitly when an action takes no arguments', () => {
    // Silence would read as "nothing will happen", which is a different claim from
    // "this action has no parameters".
    renderView([approval({ action: { tool: 'batch.flush', input: {} } })])
    expect(screen.getByText('This action takes no arguments.')).toBeTruthy()
  })

  it('survives an action with no `action` object at all', () => {
    renderView([approval({ action: null, body: undefined })])
    expect(screen.getByText('Refund order #4417')).toBeTruthy()
    expect(screen.getByText('action')).toBeTruthy()
  })

  it('truncates a huge payload instead of rendering megabytes', () => {
    const big = { blob: 'x'.repeat(9000) }
    renderView([approval({ action: { tool: 't', input: big } })])
    const args = screen.getByLabelText('Arguments for t')
    expect(args.textContent).toContain('truncated')
    expect((args.textContent ?? '').length).toBeLessThan(4200)
  })

  it('reports an unserialisable payload rather than rendering nothing', () => {
    // "We could not show you this" and "there was nothing to show" are different
    // facts, and only one of them is a reason not to approve.
    const circular: Record<string, unknown> = { name: 'loop' }
    circular.self = circular
    renderView([approval({ action: { tool: 't', input: circular } })])
    expect(screen.getByLabelText('Arguments for t').textContent).toContain(
      'could not be displayed',
    )
  })
})

describe('the workspace inbox, rendered under one agent', () => {
  it('marks the approvals that belong to a different agent', () => {
    renderView([approval(), approval({ id: 'apr_2', agent: 'billing-agent' })], 'support-agent')
    // Still listed — hiding a pending human gate because another agent is selected is
    // how a run waits forever.
    expect(screen.getByText('billing-agent')).toBeTruthy()
    expect(document.querySelector('.apf')?.textContent).toBe('billing-agent')
    expect(screen.getByText(/1 of these is for another agent/)).toBeTruthy()
  })

  it('says nothing about other agents when every row is the selected one', () => {
    renderView([approval(), approval({ id: 'apr_2' })], 'support-agent')
    expect(screen.queryByText(/for another agent/)).toBeNull()
    expect(document.querySelector('.apf')).toBeNull()
  })

  it('pluralises the warning', () => {
    renderView(
      [
        approval({ id: 'a', agent: 'billing-agent' }),
        approval({ id: 'b', agent: 'ops-agent' }),
      ],
      'support-agent',
    )
    expect(screen.getByText(/2 of these are for another agent/)).toBeTruthy()
  })

  it('marks nothing when the server sent no agent, rather than guessing', () => {
    renderView([approval({ agent: null })], 'support-agent')
    expect(screen.queryByText(/for another agent/)).toBeNull()
  })

  it('shows the empty state when there is nothing pending', () => {
    renderView([])
    expect(screen.getByText('No pending approvals.')).toBeTruthy()
  })
})

describe('resolving', () => {
  it('posts to the approve route and reports the resulting run status', async () => {
    const fn = stubFetch(() => json({ runStatus: 'resuming' }))
    const { onToast, onResolved } = renderView([approval()])

    fireEvent.click(screen.getByRole('button', { name: /^Approve$/ }))

    await waitFor(() => expect(onToast).toHaveBeenCalledWith('Approved → run resuming'))
    expect(String(fn.mock.calls[0]![0])).toBe('/approvals/apr_1/approve')
    expect(fn.mock.calls[0]![1]?.method).toBe('POST')
    expect(onResolved).toHaveBeenCalled()
  })

  it('posts to the reject route', async () => {
    const fn = stubFetch(() => json({ runStatus: 'rejected' }))
    const { onToast } = renderView([approval()])

    fireEvent.click(screen.getByRole('button', { name: /^Reject$/ }))

    await waitFor(() => expect(onToast).toHaveBeenCalledWith('Rejected → run rejected'))
    expect(String(fn.mock.calls[0]![0])).toBe('/approvals/apr_1/reject')
  })

  it('encodes the id, so an id with a slash cannot forge a path', async () => {
    const fn = stubFetch(() => json({ runStatus: 'resuming' }))
    renderView([approval({ id: 'apr/../evil' })])
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/ }))
    await waitFor(() => expect(fn).toHaveBeenCalled())
    expect(String(fn.mock.calls[0]![0])).toBe('/approvals/apr%2F..%2Fevil/approve')
  })

  it('disables only the row being resolved, not the whole list', async () => {
    let release: (r: Response) => void = () => {}
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise<Response>((res) => (release = res))),
    )
    renderView([approval(), approval({ id: 'apr_2', title: 'Second' })])

    const approves = screen.getAllByRole('button', { name: /^Approve$/ })
    fireEvent.click(approves[0]!)

    // The other row stays actionable: an operator with a queue of approvals must not
    // be blocked on one slow resume.
    await waitFor(() => expect(approves[0]).toHaveProperty('disabled', true))
    expect(approves[1]).toHaveProperty('disabled', false)

    // Settle inside act(), or React warns about a state update escaping the test.
    await act(async () => {
      release(json({ runStatus: 'resuming' }))
    })
  })

  it('re-enables the buttons after a failure, and shows the server message', async () => {
    // Bank mode (§4.3/§4.2) lands here: E_APPROVER_IDENTITY_REQUIRED with a hint. The
    // operator needs the buttons back and needs to know why it refused.
    stubFetch(() =>
      json(
        {
          ok: false,
          error: {
            code: 'E_APPROVER_IDENTITY_REQUIRED',
            message: 'This deployment requires a user identity to resolve approvals.',
            hint: 'POST /v1/token with your session, then send X-Rya-User-Token.',
          },
        },
        401,
      ),
    )
    const { onToast, onResolved } = renderView([approval()])

    const approve = screen.getByRole('button', { name: /^Approve$/ })
    fireEvent.click(approve)

    await waitFor(() =>
      expect(onToast).toHaveBeenCalledWith(
        expect.stringContaining('requires a user identity'),
      ),
    )
    expect(onToast).toHaveBeenCalledWith(expect.stringContaining('X-Rya-User-Token'))
    expect(approve).toHaveProperty('disabled', false)
    // Nothing was resolved, so the list must not be told to refresh as if it had been.
    expect(onResolved).not.toHaveBeenCalled()
  })
})
