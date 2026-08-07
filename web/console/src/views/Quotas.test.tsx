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
          json: () => Promise.resolve({ ok: false, error: { message: 'boom' } }),
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

/**
 * `GET /posture` as the server actually sends it, which is the point of these fixtures:
 * `PostureReport.describe()` names its own conditions and there are FOUR of them. This
 * shape is copied from the server rather than invented here — a fixture that lists the
 * conditions the console *expects* would re-create audit §5.7 inside the test suite.
 *
 * `local` is the honest trusted deployment: no isolation, no mediation, no egress
 * policy — and D32 satisfied in its weak form, because `local` does launch the claimer.
 */
const TRUSTED_POSTURE = {
  untrusted: false,
  ok: false,
  unmet: [
    'isolation (D23): the \'local\' driver provides \'none\'',
    'credential mediation (D18): RYA_BROKER is not set, so a tenant process would hold the database credential',
    'network egress (D24): RYA_EGRESS_MODE is \'none\', so nothing at the network layer stops tenant code',
  ],
  conditions: [
    { key: 'isolation', label: 'Isolation (D23)', ok: false, detail: 'driver "local" gives no isolation' },
    { key: 'broker', label: 'Credential mediation (D18)', ok: false, detail: 'broker not mediating' },
    { key: 'egress', label: 'Network egress (D24)', ok: false, detail: 'no egress policy' },
    { key: 'topology', label: 'Broker topology (D32)', ok: true, detail: 'the "local" driver launches the claimer' },
  ],
  isolation: { ok: false, detail: 'driver "local" gives no isolation' },
  broker: { ok: false, detail: 'broker not mediating' },
  egress: { ok: false, detail: 'no egress policy' },
  topology: { ok: true, detail: 'the "local" driver launches the claimer' },
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
    // `ok` is false here, yet the posture tile is not flagged: three of the four
    // conditions read "not in force" and the tile stays calm.
    expect(screen.getAllByText('not in force').length).toBe(3)
    expect(screen.getByText('Posture').closest('.stat')?.querySelector('.v.amber')).toBeNull()
    // The trusted posture is supported, and the page says so.
    expect(screen.getByText(/the trusted posture is supported/)).toBeTruthy()
    // ...and the unmet reasons are NOT raised as an alarm here. Unmet-on-trusted is the
    // DESIGNED state; listing what a deployment would need if it were untrusted would
    // make every self-host look broken, which is the mistake the badge already avoids.
    expect(screen.queryByText(/refuses to start work for an untrusted tenant/)).toBeNull()
    // `unmet` IS sent on this fixture, so this asserts the page discards it here on
    // purpose rather than never having had it.
    expect(TRUSTED_POSTURE.unmet.length).toBe(3)
    expect(screen.queryByText(/driver provides 'none'/)).toBeNull()
  })

  /**
   * Audit §5.7, executable. The gate has four conditions and this page used to declare
   * three of them itself, so when D32 arrived server-side the table went on showing the
   * three it knew — every one of them "in force" under a tile reading INCOMPLETE,
   * because `ok`/`unmet` did count the fourth. The rows are the server's now.
   */
  it('renders every condition the SERVER names, D32 included, in the gate’s order', async () => {
    stubFetch({ '/quotas': QUOTAS, '/usage': METER, '/posture': TRUSTED_POSTURE })
    render(view())

    expect(await screen.findByText('Broker topology (D32)')).toBeTruthy()
    expect(screen.getByText('the "local" driver launches the claimer')).toBeTruthy()

    const table = screen.getByText('Isolation (D23)').closest('table')
    const rows = Array.from(table?.querySelectorAll('tbody tr') ?? []).map(
      (r) => r.querySelector('td')?.textContent,
    )
    expect(rows).toEqual([
      'Isolation (D23)',
      'Credential mediation (D18)',
      'Network egress (D24)',
      'Broker topology (D32)',
    ])
  })

  /**
   * The proof that the drift class is closed, not just this instance of it: a condition
   * added to the gate after this console shipped renders under the server's own label
   * with no change here. A client-side list could not do this by construction.
   */
  it('renders a condition it has never heard of, labelled by the server', async () => {
    stubFetch({
      '/quotas': QUOTAS,
      '/usage': METER,
      '/posture': {
        ...TRUSTED_POSTURE,
        conditions: [
          ...TRUSTED_POSTURE.conditions,
          { key: 'attestation', label: 'Hardware attestation (D41)', ok: false, detail: 'no TPM quote' },
        ],
      },
    })
    render(view())

    expect(await screen.findByText('Hardware attestation (D41)')).toBeTruthy()
    expect(screen.getByText('no TPM quote')).toBeTruthy()
    expect(screen.getAllByText('not in force').length).toBe(4)
  })

  /**
   * The other half of §5.7: `unmet` was fetched and discarded, so the tile said
   * INCOMPLETE and nothing on the page said what was missing. The server writes those
   * sentences for an operator — they are the refusal message the deploy will produce.
   */
  it('says WHY an untrusted posture is incomplete, in the platform’s own words', async () => {
    const unmet = [
      "isolation (D23): the 'docker' driver provides 'container'",
      "broker topology (D32): the 'docker' driver launches a credential-free sandbox, which cannot also be the claimer",
    ]
    stubFetch({
      '/quotas': QUOTAS,
      '/usage': METER,
      '/posture': {
        ...TRUSTED_POSTURE,
        untrusted: true,
        ok: false,
        unmet,
        conditions: [
          { key: 'isolation', label: 'Isolation (D23)', ok: false, detail: "the 'docker' driver provides 'container'" },
          { key: 'broker', label: 'Credential mediation (D18)', ok: true, detail: 'tenant processes are mediated' },
          { key: 'egress', label: 'Network egress (D24)', ok: true, detail: 'the substrate restricts egress' },
          { key: 'topology', label: 'Broker topology (D32)', ok: false, detail: 'launches a credential-free sandbox' },
        ],
      },
    })
    render(view())

    expect(await screen.findByText('INCOMPLETE')).toBeTruthy()
    // Both reasons, verbatim: an operator reading INCOMPLETE has to be able to act.
    for (const reason of unmet) expect(screen.getByText(reason)).toBeTruthy()
    expect(screen.getByText(/refuses to start work for an untrusted tenant/)).toBeTruthy()
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
        conditions: TRUSTED_POSTURE.conditions.map((c) =>
          c.key === 'egress' ? { ...c, ok: true, detail: 'egress allowlist enforced' } : c,
        ),
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
