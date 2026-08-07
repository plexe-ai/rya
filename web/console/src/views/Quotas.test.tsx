import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QuotasView } from './Quotas'
import type { ConsoleState } from '../lib/types'

/**
 * Stub the network at `fetch`, not at `lib/api`, so the real request path runs: this
 * page's three endpoints are all workspace/deployment-scoped and a test that mocked
 * `api()` could not tell a prefixed path from an unprefixed one.
 */
function stubFetch(routes: Record<string, unknown | Error>) {
  const calls: string[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      const path = String(url)
      calls.push(path)
      const body = routes[path]
      if (body === undefined || body instanceof Error) {
        return Promise.resolve({
          ok: false,
          status: 500,
          json: () => Promise.resolve({ detail: { message: 'boom' } }),
        } as unknown as Response)
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) } as unknown as Response)
    }),
  )
  return calls
}

/** QuotasView reads nothing off the aggregate; the props are the shell's contract. */
const state = {} as unknown as ConsoleState
const view = () => <QuotasView state={state} onToast={() => {}} />

const QUOTAS = {
  quota: {
    enforced: true,
    source: 'policy',
    maxConcurrentRuns: 4,
    maxRunsPerDay: 100,
    maxQueueDepth: null,
    maxTokensPerDay: 1_000_000,
    maxCostUsdPerDay: null,
    maxWorkers: 3,
  },
  usage: {
    concurrentRuns: 4,
    runsToday: 12,
    queueDepth: 0,
    tokensToday: 4321,
    costUsdToday: 0.5,
    workers: 1,
  },
  admission: [{ limit: 'maxConcurrentRuns', label: 'Concurrent runs', current: 4, max: 4 }],
}

const METER = { usage: { calls: 9, inputTokens: 1000, outputTokens: 200, costUsd: 0.1234 } }

const TRUSTED_POSTURE = {
  untrusted: false,
  ok: false,
  isolation: { ok: false, detail: 'driver "local" gives no isolation' },
  broker: { ok: false, detail: 'broker not mediating' },
  egress: { ok: false, detail: 'no egress policy' },
  driver: { driver: 'local', isolation: 'none' },
  probe: null,
  credentials: { clean: true, violations: [] },
}

afterEach(() => vi.unstubAllGlobals())

describe('QuotasView', () => {
  it('renders this workspace’s ceilings, the meter totals and the launch gate', async () => {
    const calls = stubFetch({ '/quotas': QUOTAS, '/usage': METER, '/posture': TRUSTED_POSTURE })
    render(view())

    expect(await screen.findByText('Concurrent runs')).toBeTruthy()
    // At its ceiling, so this row must read as a breach and the tile must count it.
    expect(screen.getByText('at limit')).toBeTruthy()
    // "Runs today" appears twice on purpose: once as a tile, once as a ceiling row.
    expect(screen.getAllByText('Runs today').length).toBe(2)
    // Meter totals come from the durable meter, not from run traces.
    expect(screen.getByText('$0.1234')).toBeTruthy()
    // ...and the launch gate is on the same page, last.
    expect(screen.getByText('Isolation (D23)')).toBeTruthy()
    expect(screen.getByText('Credential mediation (D18)')).toBeTruthy()
    expect(screen.getByText('Network egress (D24)')).toBeTruthy()

    // Every one of the three is workspace/deployment-scoped: no agent prefix anywhere.
    expect(calls).toEqual(['/quotas', '/usage', '/posture'])
    expect(calls.some((c) => c.includes('/agents/'))).toBe(false)
  })

  it('reads an unset ceiling as unlimited, never as a breach', async () => {
    stubFetch({ '/quotas': QUOTAS, '/usage': METER, '/posture': TRUSTED_POSTURE })
    render(view())
    await screen.findByText('Concurrent runs')

    // maxQueueDepth and maxCostUsdPerDay are unset while both are being consumed.
    // Unset means unlimited, so there is no verdict to render for those two rows.
    expect(screen.getAllByText('unlimited').length).toBe(2)
    // Exactly one row is genuinely at its ceiling (concurrent runs).
    expect(screen.getAllByText('at limit').length).toBe(1)
  })

  it('degrades calmly when nothing is set and nothing has been metered', async () => {
    stubFetch({ '/quotas': { quota: {}, usage: {}, admission: [] }, '/posture': TRUSTED_POSTURE })
    render(view())

    // `/usage` is absent from the routes above, so it fails: an empty meter is an
    // ordinary state and must not take the ceilings down with it.
    expect(await screen.findByText('No metered usage yet.')).toBeTruthy()
    expect(screen.getByText('Concurrent runs')).toBeTruthy()
    // The Quota tile reads "unlimited" and so does every one of the six rows.
    expect(screen.getAllByText('unlimited').length).toBe(7)
    expect(screen.queryByText('at limit')).toBeNull()
  })

  /**
   * The api omits `org` until a reconciler has written a verdict, because "no rollup"
   * and "an all-clear rollup" are different states. Rendering an all-clear for an
   * absent block would erase the distinction, so the block must be ABSENT.
   */
  it('omits the organization block entirely when the api sends no org', async () => {
    stubFetch({ '/quotas': QUOTAS, '/usage': METER, '/posture': TRUSTED_POSTURE })
    render(view())
    await screen.findByText('Concurrent runs')

    expect(screen.queryByText('Organization budget')).toBeNull()
    expect(screen.queryByText('within budget')).toBeNull()
    expect(screen.queryByText('This organization has no budget set.')).toBeNull()
  })

  it('says in words when the ORG is the boundary that refused, not this workspace', async () => {
    stubFetch({
      '/quotas': {
        ...QUOTAS,
        org: {
          orgId: 'org_1',
          exhausted: true,
          budget: { maxCostUsdPerDay: 10, maxTokensPerDay: null, maxCostUsdPerMonth: null },
          usage: { costUsdToday: 12, tokensToday: 5, costUsdMonth: 12 },
          workspaces: ['ws_a', 'ws_b'],
          computedAt: '2026-08-05T10:00:00Z',
        },
      },
      '/usage': METER,
      '/posture': TRUSTED_POSTURE,
    })
    render(view())

    expect(await screen.findByText('Organization budget')).toBeTruthy()
    // Only the ceilings the org actually set appear; the two nulls do not.
    expect(screen.getByText('Org USD today')).toBeTruthy()
    expect(screen.queryByText('Org tokens today')).toBeNull()
    expect(screen.getByText('over budget')).toBeTruthy()
    expect(screen.getByText(/refusing new work because its/)).toBeTruthy()
  })

  it('shows an all-clear rollup as within budget when there IS a verdict', async () => {
    stubFetch({
      '/quotas': {
        ...QUOTAS,
        org: {
          orgId: 'org_1',
          exhausted: false,
          budget: { maxCostUsdPerDay: 10 },
          usage: { costUsdToday: 1 },
          workspaces: ['ws_a'],
          computedAt: '2026-08-05T10:00:00Z',
        },
      },
      '/usage': METER,
      '/posture': TRUSTED_POSTURE,
    })
    render(view())

    expect(await screen.findByText('Organization budget')).toBeTruthy()
    expect(screen.getByText('within budget')).toBeTruthy()
    expect(screen.queryByText(/refusing new work because its/)).toBeNull()
  })

  /**
   * The badge follows `untrusted`, not `ok`. A trusted deployment with none of the three
   * conditions met is CORRECT, and a red mark there would train an operator to ignore it
   * on the one deployment where it means something.
   */
  it('leaves the posture badge calm on a trusted deployment with ok:false', async () => {
    stubFetch({ '/quotas': QUOTAS, '/usage': METER, '/posture': TRUSTED_POSTURE })
    render(view())

    expect(await screen.findByText('trusted')).toBeTruthy()
    expect(screen.getByText('hostile-tenant isolation not claimed')).toBeTruthy()
    // `ok` is false here, yet the posture tile is not flagged: the three conditions
    // read "not in force" and the tile stays calm.
    expect(screen.getAllByText('not in force').length).toBe(3)
    expect(screen.getByText('Posture').closest('.stat')?.querySelector('.v.amber')).toBeNull()
    // The trusted posture is supported, and the page says so.
    expect(screen.getByText(/the trusted posture is supported/)).toBeTruthy()
  })

  it('flags an UNTRUSTED deployment whose conditions are not all in force', async () => {
    stubFetch({
      '/quotas': QUOTAS,
      '/usage': METER,
      '/posture': {
        ...TRUSTED_POSTURE,
        untrusted: true,
        ok: false,
        egress: { ok: true, detail: 'egress allowlist enforced' },
        probe: { verified: false },
        credentials: { clean: false, violations: [{ group: 'aws' }] },
      },
    })
    render(view())

    expect(await screen.findByText('INCOMPLETE')).toBeTruthy()
    expect(screen.getByText('Posture').closest('.stat')?.querySelector('.v.amber')).toBeTruthy()
    expect(screen.getByText('untrusted tenancy declared')).toBeTruthy()
    expect(screen.getByText('RYA_UNTRUSTED_TENANTS=1')).toBeTruthy()
    // A refuted probe is louder than an unverified one.
    expect(screen.getByText(/REFUTED/)).toBeTruthy()
    // Credential KINDS, never values.
    expect(screen.getByText('holds credentials')).toBeTruthy()
    expect(screen.getByText('aws')).toBeTruthy()
    // ...and the "trusted posture is supported" note belongs only to trusted mode.
    expect(screen.queryByText(/the trusted posture is supported/)).toBeNull()
  })

  it('keeps the page up when only the posture route is unavailable', async () => {
    stubFetch({ '/quotas': QUOTAS, '/usage': METER })
    render(view())

    expect(await screen.findByText('Concurrent runs')).toBeTruthy()
    expect(screen.queryByText('Tenant posture')).toBeNull()
  })

  it('asks for a key rather than reporting an outage when unauthenticated', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve({ ok: false, status: 401, json: () => Promise.resolve({}) } as unknown as Response),
      ),
    )
    render(view())
    expect(await screen.findByText(/Connect with a workspace key/)).toBeTruthy()
  })
})
