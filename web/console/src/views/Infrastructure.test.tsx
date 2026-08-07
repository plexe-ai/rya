import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { InfrastructureView } from './Infrastructure'
import type { ConsoleState } from '../lib/types'

/** Only `infra` is read, so the rest of the aggregate is irrelevant here. */
const stateWith = (infra: unknown) => ({ infra }) as unknown as ConsoleState

const FULL = {
  version: '0.4.2',
  python: '3.12.1',
  platform: 'Linux aarch64',
  pid: 4242,
  uptimeSeconds: 3725,
  environment: 'production',
  store: { backend: 'postgres', host: 'db.internal', dbname: 'rya' },
  auth: { mode: 'multi-tenant · API keys + Postgres RLS', webhookSignature: true, rls: true },
  observability: { traces: true, export: 'Langfuse' },
  planes: { controlPlane: 'FastAPI (this process)', dataPlane: 'in-process worker' },
  endpoints: ['POST /inbound', 'GET /console'],
  realtime: { websocket: '/ws', protocol: 'json frames: event|message|replay|ping' },
}

describe('InfrastructureView', () => {
  it('renders the live process facts', () => {
    render(<InfrastructureView state={stateWith(FULL)} onToast={() => {}} />)
    expect(screen.getByText('Python 3.12.1')).toBeTruthy()
    expect(screen.getByText('Linux aarch64')).toBeTruthy()
    expect(screen.getByText('pid 4242')).toBeTruthy()
    expect(screen.getByText('1h 2m')).toBeTruthy()
    expect(screen.getByText('db.internal/rya')).toBeTruthy()
    expect(screen.getByText('rya 0.4.2')).toBeTruthy()
    expect(screen.getByText('enforced')).toBeTruthy()
    expect(screen.getByText('multi-tenant')).toBeTruthy()
    expect(screen.getByText(/POST \/inbound/)).toBeTruthy()
    expect(screen.getByText('/ws')).toBeTruthy()
  })

  /**
   * The shape varies by deployment mode, which is why the local interface has no
   * required fields. A file-store dev server has no `host`/`dbname`, and a control
   * plane that imports nothing has no manifest to report tracing from.
   */
  it('renders a sparse payload without throwing', () => {
    render(<InfrastructureView state={stateWith({ version: '0.4.2' })} onToast={() => {}} />)
    expect(screen.getByText('rya 0.4.2')).toBeTruthy()
    // Every unknown field degrades to a dash rather than "undefined".
    expect(screen.getAllByText('—').length).toBeGreaterThan(3)
    // No endpoint list means no window at all, not an empty one.
    expect(document.querySelector('.window')).toBeNull()
  })

  it('reports a file store as non-durable dev storage, by location', () => {
    render(
      <InfrastructureView
        state={stateWith({ store: { backend: 'file', location: '.rya/store.json' } })}
        onToast={() => {}}
      />,
    )
    expect(screen.getByText('.rya/store.json')).toBeTruthy()
    expect(screen.getByText('file (dev)')).toBeTruthy()
    // No RLS reported means single-tenant, and it must not read as "enforced".
    expect(screen.getByText('single-tenant')).toBeTruthy()
  })

  /**
   * `observability.traces` is null when this process holds no manifest for the agent
   * — every published bundle since D21. "off" would be a false claim about a
   * deployment that never said, the same bug `agent.handlers: null` used to cause.
   */
  it('distinguishes tracing not declared from tracing off', () => {
    // Scoped to the Observability card: "off" also appears under webhook signing.
    const traceRow = () =>
      [...document.querySelectorAll('.ispc')]
        .find((c) => c.querySelector('.hd .t')?.textContent === 'Observability')
        ?.querySelector('.rw .v')?.textContent

    const { unmount } = render(
      <InfrastructureView
        state={stateWith({ observability: { traces: null, export: 'local trace store' } })}
        onToast={() => {}}
      />,
    )
    expect(traceRow()).toBe('not declared')
    unmount()

    render(
      <InfrastructureView state={stateWith({ observability: { traces: false } })} onToast={() => {}} />,
    )
    expect(traceRow()).toBe('off')
    expect(screen.queryByText('not declared')).toBeNull()
  })

  it('degrades calmly when the payload is missing entirely', () => {
    render(<InfrastructureView state={stateWith(undefined)} onToast={() => {}} />)
    expect(screen.getByText('Infrastructure info unavailable.')).toBeTruthy()
    expect(document.querySelector('.ispc')).toBeNull()
  })

  it('renders values as text, so a hostile field cannot inject markup', () => {
    const nasty = '<img src=x onerror=alert(1)>'
    render(<InfrastructureView state={stateWith({ platform: nasty })} onToast={() => {}} />)
    expect(screen.getByText(nasty)).toBeTruthy()
    expect(document.querySelector('img')).toBeNull()
  })
})
