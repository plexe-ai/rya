import { describe, expect, it, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { GuardView } from './Guard'
import type { ConsoleState } from '../lib/types'

/** Views always receive a `ConsoleState` with an agent; only its name is read here. */
const state = { agent: { name: 'acme' } } as unknown as ConsoleState

interface Call {
  url: string
  method: string
  body: unknown
}

/**
 * Stub `fetch` rather than the api module: the request URL and method are part of
 * what this view has to get right (agent-prefixed `PUT /agents/{agent}/guard`), and
 * mocking `api` would hide both.
 */
function stubFetch(reply: (call: Call) => { status?: number; body?: unknown }) {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      const call: Call = {
        url: String(url),
        method: (init?.method ?? 'GET').toUpperCase(),
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      }
      calls.push(call)
      const { status = 200, body = {} } = reply(call)
      return Promise.resolve({
        ok: status < 400,
        status,
        json: () => Promise.resolve(body),
      } as Response)
    }),
  )
  return calls
}

const TESTS = {
  total: 6,
  passed: 5,
  attacksBlocked: 4,
  attacksTotal: 5,
  benignFalseBlocks: 1,
  benignTotal: 1,
  accuracy: 83,
}

const POLICY = {
  policy: 'Only talk to the CRM.',
  ssrf: true,
  default: 'deny',
  fail: 'closed',
  rules: [
    { action: 'allow', kind: 'prefix', pattern: 'https://api.crm.com/', methods: ['GET'], note: 'crm' },
    { action: 'allow', kind: 'glob', pattern: 'https://cdn.crm.com/*', methods: [], note: '' },
    { action: 'deny', kind: 'glob', pattern: 'https://webhook.site/*', methods: [], note: 'exfil' },
  ],
}

const loaded = (over: Record<string, unknown> = {}) => ({
  agent: 'acme',
  policy: POLICY,
  tests: TESTS,
  exists: true,
  ...over,
})

afterEach(() => vi.unstubAllGlobals())

describe('GuardView', () => {
  it('loads the policy on entry, agent-prefixed, and renders it', async () => {
    const calls = stubFetch(() => ({ body: loaded() }))
    render(<GuardView state={state} onToast={() => {}} />)

    expect(await screen.findByDisplayValue('https://api.crm.com/')).toBeTruthy()
    expect(calls).toEqual([{ url: '/agents/acme/guard', method: 'GET', body: undefined }])

    // Test-suite tiles.
    expect(screen.getByText('5/6')).toBeTruthy()
    expect(screen.getByText('83%')).toBeTruthy()
    // Policy prose, judge failure mode, default and the SSRF switch.
    expect((screen.getByLabelText('Security policy') as HTMLTextAreaElement).value).toBe(
      'Only talk to the CRM.',
    )
    expect((screen.getByLabelText('Judge failure mode') as HTMLSelectElement).value).toBe('closed')
    expect((screen.getByLabelText('Default policy') as HTMLSelectElement).value).toBe('deny')
    // Grouped allow/deny, deny beats allow.
    expect(screen.getByDisplayValue('https://webhook.site/*')).toBeTruthy()
    expect(screen.getByDisplayValue('GET')).toBeTruthy()
  })

  it('saves nothing until asked: the button is inert on a clean draft', async () => {
    stubFetch(() => ({ body: loaded() }))
    render(<GuardView state={state} onToast={() => {}} />)
    await screen.findByDisplayValue('https://api.crm.com/')

    const save = screen.getByRole('button', { name: /save policy/i }) as HTMLButtonElement
    expect(save.disabled).toBe(true)
    expect(screen.getByText('saved')).toBeTruthy()
  })

  /** The core write path: an edited pattern must reach the server, spelled `pattern`. */
  it('sends the edited pattern in a PUT to the agent-prefixed guard route', async () => {
    const calls = stubFetch((c) =>
      c.method === 'PUT' ? { body: { ok: true, tests: { passed: 6, total: 6 } } } : { body: loaded() },
    )
    const toast = vi.fn()
    render(<GuardView state={state} onToast={toast} />)

    const pattern = (await screen.findByLabelText('allow rule 1 pattern')) as HTMLInputElement
    fireEvent.change(pattern, { target: { value: 'https://api.crm.com/v2/' } })
    fireEvent.change(screen.getByLabelText('allow rule 1 methods'), { target: { value: 'GET, POST' } })
    expect(screen.getByText('unsaved changes')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /save policy/i }))

    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    const put = calls.find((c) => c.method === 'PUT')!
    expect(put.url).toBe('/agents/acme/guard')

    const body = put.body as { policy: { rules: Record<string, unknown>[]; default: string } }
    expect(body.policy.rules[0]).toEqual({
      action: 'allow',
      kind: 'prefix',
      pattern: 'https://api.crm.com/v2/',
      methods: ['GET', 'POST'],
      note: 'crm',
    })
    // The field is `pattern`, never `url`: `guard.py: _matcher` reads `pattern`, so a
    // rule carrying `url` compiles to startswith("") and allows everything under a
    // default of deny (the bug in examples/crizac/rya.guard.yaml).
    expect(Object.keys(body.policy.rules[0]!)).not.toContain('url')
    expect(body.policy.default).toBe('deny')

    await waitFor(() => expect(toast).toHaveBeenCalledWith('Policy saved · 6/6 tests pass'))
    // Refresh after the write: the server re-scores the suite and stamps a version.
    expect(calls.filter((c) => c.method === 'GET').length).toBe(2)
  })

  it('drops a rule with no pattern instead of saving a match-everything rule', async () => {
    const calls = stubFetch((c) => (c.method === 'PUT' ? { body: { ok: true, tests: TESTS } } : { body: loaded() }))
    render(<GuardView state={state} onToast={() => {}} />)
    await screen.findByLabelText('allow rule 1 pattern')

    fireEvent.click(screen.getByRole('button', { name: /add allow/i }))
    // A new, still-empty row exists in the draft...
    expect(screen.getByLabelText('allow rule 3 pattern')).toBeTruthy()
    // ...but it cannot be saved on its own, because it would change nothing.
    expect((screen.getByRole('button', { name: /save policy/i }) as HTMLButtonElement).disabled).toBe(true)

    fireEvent.change(screen.getByLabelText('allow rule 3 pattern'), { target: { value: 'https://ok.dev/' } })
    fireEvent.change(screen.getByLabelText('allow rule 1 pattern'), { target: { value: '   ' } })
    fireEvent.click(screen.getByRole('button', { name: /save policy/i }))

    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    const rules = (calls.find((c) => c.method === 'PUT')!.body as { policy: { rules: { pattern: string }[] } })
      .policy.rules
    // The blank-patterned rule is gone; a new rule appends to the document (the
    // groups are a display concern — the server splits allow from deny itself).
    expect(rules.map((r) => r.pattern)).toEqual([
      'https://cdn.crm.com/*',
      'https://webhook.site/*',
      'https://ok.dev/',
    ])
  })

  /**
   * The regression this migration exists to delete.
   *
   * The legacy console re-rendered the guard from an HTML string and worked around
   * the clobbering with a `document.activeElement` sniff. Here the rows are React
   * state and the fetch is a `useLoad`, so a parent poll arrives as new props and
   * cannot touch an unsaved draft, its focus or its caret.
   */
  it('keeps an unsaved draft, focus and caret across a parent re-render', async () => {
    stubFetch(() => ({ body: loaded() }))
    const { rerender } = render(<GuardView state={state} onToast={() => {}} />)

    const input = (await screen.findByLabelText('allow rule 1 pattern')) as HTMLInputElement
    input.focus()
    fireEvent.change(input, { target: { value: 'https://api.crm.com/v9/' } })
    input.setSelectionRange(4, 4)

    // A 6s poll lands with a fresh `/console` payload (new object, same agent).
    rerender(<GuardView state={{ ...state, runs: [] } as unknown as ConsoleState} onToast={() => {}} />)

    const after = screen.getByLabelText('allow rule 1 pattern') as HTMLInputElement
    expect(after.value).toBe('https://api.crm.com/v9/')
    expect(document.activeElement).toBe(after)
    expect(after.selectionStart).toBe(4)
    expect(screen.getByText('unsaved changes')).toBeTruthy()
  })

  it('keys rows by identity, so removing one does not shift the inputs below it', async () => {
    stubFetch(() => ({ body: loaded() }))
    render(<GuardView state={state} onToast={() => {}} />)
    await screen.findByDisplayValue('https://api.crm.com/')

    fireEvent.click(screen.getByRole('button', { name: 'Remove allow rule 1' }))

    // The surviving rule keeps ITS pattern, not the removed row's.
    expect((screen.getByLabelText('allow rule 1 pattern') as HTMLInputElement).value).toBe(
      'https://cdn.crm.com/*',
    )
    expect(screen.queryByDisplayValue('https://api.crm.com/')).toBeNull()
  })

  it('reads a missing policy as no egress policy configured, not as an error', async () => {
    stubFetch(() => ({ body: { agent: 'acme', policy: {}, tests: {}, exists: false } }))
    render(<GuardView state={state} onToast={() => {}} />)

    expect(await screen.findByText(/No egress policy configured/)).toBeTruthy()
    // ...and the editor is still there, seeded with the server's own defaults, so
    // this is where a first policy gets written.
    expect((screen.getByLabelText('Default policy') as HTMLSelectElement).value).toBe('deny')
    expect((screen.getByLabelText('Judge failure mode') as HTMLSelectElement).value).toBe('closed')
    expect(screen.getByText(/No allow rules/)).toBeTruthy()
    expect(screen.getByText('No deny rules.')).toBeTruthy()
  })

  it('says a present-but-unreadable policy is failing closed', async () => {
    stubFetch(() => ({ body: loaded({ error: 'policy store read failed: OSError' }) }))
    render(<GuardView state={state} onToast={() => {}} />)
    expect(await screen.findByText(/failing closed — policy store read failed/)).toBeTruthy()
  })

  it('asks for a token on a 401 instead of claiming an outage', async () => {
    stubFetch(() => ({ status: 401, body: {} }))
    render(<GuardView state={state} onToast={() => {}} />)
    expect(
      await screen.findByText('Connect with an operator token to load the guard policy.'),
    ).toBeTruthy()
  })

  it('surfaces a failed save and keeps the draft', async () => {
    stubFetch((c) =>
      c.method === 'PUT'
        ? { status: 403, body: { detail: { message: 'read-only key' } } }
        : { body: loaded() },
    )
    const toast = vi.fn()
    render(<GuardView state={state} onToast={toast} />)

    fireEvent.change(await screen.findByLabelText('deny rule 1 pattern'), {
      target: { value: 'https://evil.test/*' },
    })
    fireEvent.click(screen.getByRole('button', { name: /save policy/i }))

    await waitFor(() => expect(toast).toHaveBeenCalledWith('Save failed — read-only key'))
    expect((screen.getByLabelText('deny rule 1 pattern') as HTMLInputElement).value).toBe(
      'https://evil.test/*',
    )
    expect(screen.getByText('unsaved changes')).toBeTruthy()
  })
})
