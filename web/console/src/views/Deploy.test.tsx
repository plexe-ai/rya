import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { EnvironmentsView, VersionsView, WorkersView } from './Deploy'
import type { ConsoleState } from '../lib/types'

// Only the fields the deploy views read: the selected agent (which every
// agent-scoped path is built from) and the workspace label for the breadcrumbs.
const state = { agent: { name: 'support-agent' }, viewer: { workspace: 'default' } } as unknown as ConsoleState

const json = (body: unknown) =>
  Promise.resolve(
    new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } }),
  )

/**
 * Route a stubbed `fetch` by path. Anything unrouted answers `{}` rather than
 * failing, so a test only has to describe the endpoints it cares about — the views
 * are written to degrade on a missing optional payload, and this keeps that honest.
 */
function stubRoutes(routes: [test: RegExp, body: unknown][]) {
  const calls: string[] = []
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    calls.push(url)
    for (const [re, body] of routes) if (re.test(url)) return json(body)
    return json({})
  })
  vi.stubGlobal('fetch', fetchMock)
  return { calls, fetchMock }
}

const VERSION = {
  id: 'ver_abc',
  bundleHash: 'deadbeefcafebabe0123',
  manifestVersion: '2.1',
  sdkVersion: '0.9.0',
  state: 'active',
  entrypoint: 'agent.py',
  createdAt: '2026-08-01T10:00:00Z',
  createdBy: 'ada@example.com',
}

describe('EnvironmentsView', () => {
  it('lists one row per pointer, enriched from describe_environment', async () => {
    stubRoutes([
      [/\/agents\/support-agent\/environments$/, { environments: [{ name: 'prod', currentVersionId: 'ver_abc' }] }],
      [
        /\/agents\/support-agent\/environments\/prod$/,
        {
          name: 'prod',
          currentVersion: VERSION,
          actor: 'ada@example.com',
          updatedAt: '2026-08-02T09:00:00Z',
          historyDepth: 1,
          pinnedRuns: { ver_old: 2 },
        },
      ],
      [/\/agents\/support-agent\/gate$/, { gates: [{ environment: 'prod', enforced: true, source: 'policy' }] }],
      [/\/agents\/support-agent\/versions$/, { versions: [VERSION, { id: 'ver_old' }] }],
    ])

    render(<EnvironmentsView state={state} onToast={() => {}} />)

    expect(await screen.findByText('prod')).toBeTruthy()
    // The pointer record carries no bundle hash: this cell can only come from the
    // second, per-row `describe_environment` request.
    expect(screen.getByText('deadbeefcafe')).toBeTruthy()
    expect(screen.getByText('gated')).toBeTruthy()
    expect(screen.getByText('1 pinned')).toBeTruthy()
    expect(screen.getByText('ada@example.com')).toBeTruthy()
  })

  it('treats no environments as an ordinary fresh-install state', async () => {
    stubRoutes([[/\/agents\/support-agent\/environments$/, { environments: [] }]])
    render(<EnvironmentsView state={state} onToast={() => {}} />)
    expect(await screen.findByText(/No environments yet/)).toBeTruthy()
  })

  it('drills into an environment: gate verdict, retained versions and history', async () => {
    stubRoutes([
      [/\/agents\/support-agent\/environments$/, { environments: [{ name: 'prod', currentVersionId: 'ver_abc' }] }],
      [
        /\/agents\/support-agent\/environments\/prod\/history$/,
        {
          history: [
            { versionId: 'ver_abc', bundleHash: 'deadbeefcafebabe', current: true, at: '2026-08-02T09:00:00Z', actor: 'ada@example.com' },
            { versionId: 'ver_old', bundleHash: 'feedfacefeedface', current: false, at: '2026-07-30T09:00:00Z' },
          ],
        },
      ],
      [
        /\/agents\/support-agent\/environments\/prod$/,
        {
          name: 'prod',
          currentVersion: VERSION,
          actor: 'ada@example.com',
          updatedAt: '2026-08-02T09:00:00Z',
          historyDepth: 1,
          pinnedRuns: { ver_old: 3 },
        },
      ],
      [/\/gate\/check\?env=prod/, { allowed: false, checks: [{ check: 'evals', ok: false, detail: 'score 0.4 < 0.8', fix: 'rya eval' }] }],
      [/\/gate\?env=prod/, { gates: [{ environment: 'prod', enforced: true, source: 'policy', requireEvals: true, minEvalScore: 0.8 }] }],
      [/\/agents\/support-agent\/gate$/, { gates: [{ environment: 'prod', enforced: true, source: 'policy' }] }],
      [/\/versions\/ver_old$/, { id: 'ver_old', bundleHash: 'feedfacefeedface', state: 'retired' }],
      [/\/versions\/ver_abc\/runs/, { runs: [{ id: 'run_1', status: 'completed', trigger: 'message.received', tokens: 12 }], count: 1, pinnedCount: 0 }],
    ])

    render(<EnvironmentsView state={state} onToast={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open environment prod' }))

    expect(await screen.findByText('Environment · prod')).toBeTruthy()
    // The gate's live verdict, and the unmet requirement it would refuse on.
    expect(screen.getByText('1 unmet')).toBeTruthy()
    expect(screen.getByText('score 0.4 < 0.8')).toBeTruthy()
    // §9's drain step: an older version held open because runs are pinned to it.
    expect(screen.getByText('Retained versions')).toBeTruthy()
    expect(screen.getAllByText('ver_old').length).toBeGreaterThan(0)
    expect(screen.getByText('retired')).toBeTruthy()
    expect(screen.getByText('blocked')).toBeTruthy()
    // ...and the promote/rollback history, newest first.
    expect(screen.getByText('current')).toBeTruthy()
    expect(screen.getByText('replaced')).toBeTruthy()
    expect(screen.getByText('run_1')).toBeTruthy()
  })

  it('says "fully drained" rather than showing an empty panel when nothing is pinned', async () => {
    stubRoutes([
      [/\/agents\/support-agent\/environments$/, { environments: [{ name: 'dev', currentVersionId: 'ver_abc' }] }],
      [/\/agents\/support-agent\/environments\/dev\/history$/, { history: [] }],
      [/\/agents\/support-agent\/environments\/dev$/, { name: 'dev', currentVersion: VERSION, pinnedRuns: {} }],
      [/\/versions\/ver_abc\/runs/, { runs: [], count: 0, pinnedCount: 0 }],
    ])

    render(<EnvironmentsView state={state} onToast={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open environment dev' }))

    expect(await screen.findByText(/Fully drained/)).toBeTruthy()
    expect(screen.getByText(/No promotions recorded yet/)).toBeTruthy()
    expect(screen.getByText(/No runs on the current version yet/)).toBeTruthy()
  })

  it('asks the AGENT-PREFIXED environment routes, never the bare ones', async () => {
    const { calls } = stubRoutes([
      [/\/agents\/support-agent\/environments$/, { environments: [{ name: 'prod' }] }],
    ])
    render(<EnvironmentsView state={state} onToast={() => {}} />)
    await screen.findByText('prod')

    // An unprefixed `/environments` resolves the reserved `_` alias server-side and
    // 400s E_AGENT_AMBIGUOUS the moment the workspace serves a second agent.
    expect(calls.some((u) => u.endsWith('/agents/support-agent/environments'))).toBe(true)
    expect(calls.some((u) => /(^|\/)environments$/.test(u) && !u.includes('/agents/'))).toBe(false)
  })
})

describe('VersionsView', () => {
  it('lists the ledger and marks which environments point at each version', async () => {
    stubRoutes([
      [/\/agents\/support-agent\/versions$/, { versions: [VERSION, { id: 'ver_old', state: 'retired' }] }],
      [
        /\/agents\/support-agent\/environments$/,
        { environments: [{ name: 'prod', currentVersionId: 'ver_abc' }, { name: 'dev', currentVersionId: 'ver_abc' }] },
      ],
    ])

    render(<VersionsView state={state} onToast={() => {}} />)

    expect(await screen.findByText('ver_abc')).toBeTruthy()
    expect(screen.getByText('ver_old')).toBeTruthy()
    expect(screen.getByText('prod')).toBeTruthy()
    expect(screen.getByText('dev')).toBeTruthy()
    expect(screen.getByText('retired')).toBeTruthy()
  })

  it('treats no versions as an ordinary fresh-install state', async () => {
    stubRoutes([[/\/agents\/support-agent\/versions$/, { versions: [] }]])
    render(<VersionsView state={state} onToast={() => {}} />)
    expect(await screen.findByText(/No versions recorded/)).toBeTruthy()
  })

  it('drills into a version: identity, attestations, runs and workers', async () => {
    stubRoutes([
      [/\/agents\/support-agent\/versions$/, { versions: [VERSION] }],
      [/\/agents\/support-agent\/environments$/, { environments: [{ name: 'prod', currentVersionId: 'ver_abc' }] }],
      [/\/versions\/ver_abc\/attestations$/, { attestations: [{ id: 'att_1', kind: 'evals', ok: true, passed: 3, total: 3, score: 1, createdAt: '2026-08-01T10:05:00Z' }] }],
      [/\/versions\/ver_abc\/runs/, { runs: [{ id: 'run_9', status: 'failed', trigger: 'cron', tokens: 5, environment: 'prod' }], count: 1, pinnedCount: 0 }],
      [/\/workers\?version_id=ver_abc/, { workers: [{ id: 'wrk_1', status: 'alive', versionId: 'ver_abc', bundleHash: 'deadbeefcafebabe', handlers: ['event'], host: 'box', pid: 7, coldStartMs: 240, lastHeartbeatAt: new Date().toISOString() }] }],
      [/\/versions\/ver_abc$/, VERSION],
    ])

    render(<VersionsView state={state} onToast={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open version ver_abc' }))

    expect(await screen.findByText('Version · ver_abc')).toBeTruthy()
    expect(screen.getByText('agent.py')).toBeTruthy()
    expect(screen.getByText('evals')).toBeTruthy()
    expect(screen.getByText('3/3 passed · score 1')).toBeTruthy()
    expect(screen.getByText('run_9')).toBeTruthy()
    expect(screen.getByText('wrk_1')).toBeTruthy()
  })

  it('renders a hostile version id as text, never as markup', async () => {
    const nasty = '<img src=x onerror=alert(1)>'
    stubRoutes([[/\/agents\/support-agent\/versions$/, { versions: [{ id: nasty }] }]])
    render(<VersionsView state={state} onToast={() => {}} />)
    expect(await screen.findByText(nasty)).toBeTruthy()
    expect(document.querySelector('img')).toBeNull()
  })
})

describe('WorkersView', () => {
  /**
   * The one behaviour this view must not lose.
   *
   * `/workers` defaults to `status=alive`. Asking for the default would drop a
   * crashed worker from the list, and an empty list already means something else
   * entirely — scale-to-zero, the DESIGNED idle state under §6. So a crash would
   * render identically to an idle key. `?status=` (empty) means every status.
   */
  it('requests every status, not the alive default', async () => {
    const { calls } = stubRoutes([[/\/workers/, { workers: [] }]])
    render(<WorkersView state={state} onToast={() => {}} />)
    await screen.findByText(/No workers running/)

    const workerCalls = calls.filter((u) => u.includes('/workers'))
    expect(workerCalls.length).toBe(1)
    expect(workerCalls[0]).toContain('status=')
    expect(workerCalls[0]).not.toContain('status=alive')
  })

  it('reads an empty list as idle, never as an outage', async () => {
    stubRoutes([[/\/workers/, { workers: [] }]])
    render(<WorkersView state={state} onToast={() => {}} />)
    expect(await screen.findByText(/idle steady state/)).toBeTruthy()
    expect(screen.getByText('scaled to zero')).toBeTruthy()
  })

  it('keeps a lost worker in the table and gives it its own tile', async () => {
    stubRoutes([
      [
        /\/workers/,
        {
          workers: [
            { id: 'wrk_live', status: 'alive', versionId: 'ver_abc', handlers: ['event'], coldStartMs: 310, lastHeartbeatAt: new Date().toISOString(), stats: { claimed: 4, completed: 3 } },
            // SIGKILLed: the server derives `status` from heartbeat age, so it comes
            // back `lost` rather than disappearing.
            { id: 'wrk_gone', status: 'lost', versionId: 'ver_abc', lastHeartbeatAt: '2026-08-01T00:00:00Z' },
          ],
        },
      ],
    ])

    render(<WorkersView state={state} onToast={() => {}} />)

    expect(await screen.findByText('wrk_live')).toBeTruthy()
    // The whole point: the crashed process is still a row.
    expect(screen.getByText('wrk_gone')).toBeTruthy()
    expect(screen.getByText('lost')).toBeTruthy()
    expect(screen.getByText('Lost')).toBeTruthy()
    expect(screen.getByText('stopped heartbeating; the supervisor replaces these')).toBeTruthy()
    // One live process, so no cold-start tile is shown in its place.
    expect(screen.queryByText('Slowest cold start')).toBeNull()
    expect(screen.getByText('claiming from the queue')).toBeTruthy()
    expect(screen.getByText('4 claimed this uptime')).toBeTruthy()
  })

  it('surfaces a 401 as "connect a key", not as a broken deployment', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(new Response('{}', { status: 401 }))),
    )
    render(<WorkersView state={state} onToast={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Connect with a workspace key to load Workers/)).toBeTruthy())
  })
})
