import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { GovernanceView } from './Governance'
import type { ConsoleState, Governance } from '../lib/types'

/**
 * `fetch` is stubbed with a spy that fails the test if it is ever called: this view is
 * a pure function of the `/console` aggregate the shell already polls, and giving it a
 * fetch of its own would put a second source of truth on the page.
 */
const fetchSpy = vi.fn(() => Promise.reject(new Error('GovernanceView must not fetch')))
vi.stubGlobal('fetch', fetchSpy)

afterEach(() => fetchSpy.mockClear())

const ENFORCEMENT: Governance['enforcement'] = {
  egressGuard: true,
  groundingGate: true,
  approverIdentity: true,
  perUserIdentity: false,
  multiTenantRls: true,
  secretsSealed: true,
}

const POLICY: Governance['policy'] = {
  hash: 'sha256:abc123',
  toolsGated: 2,
  toolsDenied: 1,
  pinnedArgTools: 3,
  egressRules: 4,
  egressDefault: 'deny',
}

const stateWith = (governance?: Governance) => ({ governance }) as unknown as ConsoleState

describe('GovernanceView', () => {
  it('renders the six enforcement gates, marking the ones that are off', () => {
    render(<GovernanceView state={stateWith({ enforcement: ENFORCEMENT, policy: POLICY })} onToast={() => {}} />)

    expect(screen.getByText('Egress guard')).toBeTruthy()
    expect(screen.getByText('Per-user identity')).toBeTruthy()
    // Five enforced, one off — and the wording is the gate's state, not a score.
    expect(screen.getAllByText('enforced').length).toBe(5)
    expect(screen.getAllByText('off').length).toBe(1)
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('renders the policy summary keyed off the policy hash', () => {
    render(<GovernanceView state={stateWith({ enforcement: ENFORCEMENT, policy: POLICY })} onToast={() => {}} />)

    expect(screen.getByText('sha256:abc123')).toBeTruthy()
    expect(screen.getByText('Gated')).toBeTruthy()
    expect(screen.getByText('default deny')).toBeTruthy()
  })

  it('says "not configured" when there is no egress default', () => {
    render(
      <GovernanceView
        state={stateWith({ enforcement: ENFORCEMENT, policy: { ...POLICY, egressDefault: null } })}
        onToast={() => {}}
      />,
    )
    expect(screen.getByText('not configured')).toBeTruthy()
  })

  it('treats no overrides and no violations as ordinary states', () => {
    render(<GovernanceView state={stateWith({ enforcement: ENFORCEMENT, policy: POLICY })} onToast={() => {}} />)

    expect(screen.getByText('No overrides.')).toBeTruthy()
    expect(screen.getByText('None recorded.')).toBeTruthy()
    // Nothing has been reverted, so there is no append-only history to show.
    expect(screen.queryByText('History')).toBeNull()
  })

  it('lists active kill switches, their history and the runtime’s violations', () => {
    render(
      <GovernanceView
        state={stateWith({
          enforcement: ENFORCEMENT,
          policy: POLICY,
          switches: {
            active: [{ tool: 'send_email', permission: 'disabled', ts: '2026-08-05T09:30:00Z', version: 4 }],
            history: [
              {
                ts: '2026-08-05T09:30:00Z',
                tool: 'send_email',
                permission: 'disabled',
                previous: 'allowed',
                reason: 'incident 42',
              },
              {
                ts: '2026-08-04T08:00:00Z',
                tool: 'send_email',
                permission: 'allowed',
                cleared: true,
                reason: 'restored after drill',
              },
            ],
          },
          violations: [
            { ts: '2026-08-05T10:00:00Z', kind: 'egress.blocked', runId: 'run_0123456789abcdef', detail: 'evil.test' },
          ],
        })}
        onToast={() => {}}
      />,
    )

    expect(screen.getAllByText('send_email').length).toBeGreaterThan(0)
    expect(screen.getByText('v4')).toBeTruthy()
    expect(screen.getByText('allowed -> disabled')).toBeTruthy()
    expect(screen.getByText('restored allowed')).toBeTruthy()
    expect(screen.getByText('incident 42')).toBeTruthy()
    expect(screen.getByText('egress.blocked')).toBeTruthy()
    // Run ids are truncated the way the legacy audit table truncated them.
    expect(screen.getByText('run_0123456789')).toBeTruthy()
  })

  it('says "Unavailable." rather than rendering an all-clear when governance is absent', () => {
    render(<GovernanceView state={stateWith(undefined)} onToast={() => {}} />)

    expect(screen.getByText('Unavailable.')).toBeTruthy()
    // No gate may read as "off" here: nothing was reported, which is not the same as
    // nothing being enforced.
    expect(screen.queryByText('off')).toBeNull()
    expect(screen.queryByText('Enforcement')).toBeNull()
  })

  it('renders violation detail as text, so a hostile value cannot inject markup', () => {
    const nasty = '<img src=x onerror=alert(1)>'
    render(
      <GovernanceView
        state={stateWith({
          enforcement: ENFORCEMENT,
          policy: POLICY,
          violations: [{ ts: '2026-08-05T10:00:00Z', kind: 'egress.blocked', runId: 'run_1', detail: nasty }],
        })}
        onToast={() => {}}
      />,
    )
    expect(screen.getByText(nasty)).toBeTruthy()
    expect(document.querySelector('img')).toBeNull()
  })
})
