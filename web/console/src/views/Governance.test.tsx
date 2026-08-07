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
            version: 4,
            active: [
              {
                tool: 'send_email',
                permission: 'disabled',
                ts: '2026-08-05T09:30:00Z',
                reason: 'incident 42',
              },
            ],
            history: [
              {
                ts: '2026-08-05T09:30:00Z',
                tool: 'send_email',
                permission: 'disabled',
                previous: 'allowed',
                reason: 'incident 42',
                actor: 'user:ada',
                version: 4,
              },
              {
                ts: '2026-08-04T08:00:00Z',
                tool: 'send_email',
                permission: 'allowed',
                cleared: true,
                actor: 'workspace:acme',
                version: 3,
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
    // The DOCUMENT version, in the section header. The old table had a `v` column
    // reading a per-override `version` the server has never sent — the fixture used
    // to supply one, which is the only reason `v4` ever appeared on screen.
    expect(screen.getByText(/v4 · immediate/)).toBeTruthy()
    expect(screen.getByText('allowed -> disabled')).toBeTruthy()
    expect(screen.getByText('cleared — back to allowed')).toBeTruthy()
    expect(screen.getAllByText('incident 42').length).toBe(2) // active row + history row
    expect(screen.getByText('egress.blocked')).toBeTruthy()
    // Run ids are truncated the way the legacy audit table truncated them.
    expect(screen.getByText('run_0123456789')).toBeTruthy()
  })

  it('names who changed each kill switch', () => {
    // §12 risk 7, quoted in store.py: "who reviewed this allowlist change" is the
    // feature. Every policy record carries `actor`; no screen showed it.
    render(
      <GovernanceView
        state={stateWith({
          enforcement: ENFORCEMENT,
          policy: POLICY,
          switches: {
            active: [],
            history: [
              { ts: '2026-08-05T09:30:00Z', tool: 'send_email', permission: 'disabled', actor: 'user:ada', version: 2 },
            ],
          },
        })}
        onToast={() => {}}
      />,
    )
    expect(screen.getByText('Who')).toBeTruthy()
    expect(screen.getByText('user:ada')).toBeTruthy()
  })

  it('renders an unattributed change as a dash rather than a blank cell', () => {
    render(
      <GovernanceView
        state={stateWith({
          enforcement: ENFORCEMENT,
          policy: POLICY,
          switches: {
            active: [],
            history: [{ ts: '2026-08-05T09:30:00Z', tool: 'send_email', permission: 'disabled', version: 1 }],
          },
        })}
        onToast={() => {}}
      />,
    )
    expect(screen.getByText('—')).toBeTruthy()
    // The transition still reads, with no `previous` to compare against.
    expect(screen.getByText('declared -> disabled')).toBeTruthy()
  })

  it('counts the tools whose permission is an override, not a declaration', () => {
    // The Gated/Denied tiles are EFFECTIVE permissions now. Saying so matters: a
    // reader has to know whether "Denied 1" is the manifest or an operator.
    render(
      <GovernanceView
        state={stateWith({
          enforcement: ENFORCEMENT,
          policy: { ...POLICY, toolsOverridden: 2 },
        })}
        onToast={() => {}}
      />,
    )
    expect(screen.getByText(/2 overrides ·/)).toBeTruthy()
  })

  it('does not mention overrides when there are none', () => {
    render(<GovernanceView state={stateWith({ enforcement: ENFORCEMENT, policy: POLICY })} onToast={() => {}} />)
    // Not `/override/` — the empty table legitimately reads "No overrides."
    expect(screen.queryByText(/overrides? ·/)).toBeNull()
    expect(screen.getByText('No overrides.')).toBeTruthy()
  })

  it('singularises one override', () => {
    render(
      <GovernanceView
        state={stateWith({ enforcement: ENFORCEMENT, policy: { ...POLICY, toolsOverridden: 1 } })}
        onToast={() => {}}
      />,
    )
    expect(screen.getByText(/1 override ·/)).toBeTruthy()
  })
})

describe('a source that cannot be read', () => {
  /**
   * The §4.5 failure in miniature: this view reported governed states it had not
   * read. An unreachable policy store must not render as "No overrides." — that is
   * the same picture as a healthy deployment with nothing killed, and the opposite
   * fact.
   */
  it('says the policy store is unreadable instead of showing an empty override table', () => {
    render(
      <GovernanceView
        state={stateWith({
          enforcement: ENFORCEMENT,
          policy: POLICY,
          switches: { active: [], history: [], error: 'OperationalError: connection refused' },
        })}
        onToast={() => {}}
      />,
    )
    expect(screen.getByText(/unknown, not absent/)).toBeTruthy()
    expect(screen.getByText('OperationalError: connection refused')).toBeTruthy()
    expect(screen.queryByText('No overrides.')).toBeNull()
  })

  it('warns that an unreadable egress policy is denying everything', () => {
    // `guard._closed` really does deny all traffic in this state, so the tile saying
    // "0 rules" is true and desperately incomplete on its own.
    render(
      <GovernanceView
        state={stateWith({
          enforcement: { ...ENFORCEMENT, egressGuard: true },
          policy: { ...POLICY, egressRules: 0, egressDefault: null, egressSource: 'store', egressError: 'policy store read failed: TimeoutError' },
        })}
        onToast={() => {}}
      />,
    )
    expect(screen.getByText(/every outbound request is being denied/)).toBeTruthy()
    expect(screen.getByText('unreadable — denying everything')).toBeTruthy()
  })
})

describe('where the egress policy came from', () => {
  /**
   * Two views a click apart described the firewall from different sources — Guard
   * read the store, Governance read `rya.guard.yaml`. Naming the source is what lets
   * an operator notice if they ever diverge again.
   */
  it('names the store as the source', () => {
    render(
      <GovernanceView
        state={stateWith({
          enforcement: ENFORCEMENT,
          policy: { ...POLICY, egressSource: 'store', egressVersion: 'a1b2c3' },
        })}
        onToast={() => {}}
      />,
    )
    expect(screen.getByText('default deny · from store')).toBeTruthy()
  })

  it('calls a file source the project file, without the path', () => {
    render(
      <GovernanceView
        state={stateWith({
          enforcement: ENFORCEMENT,
          policy: { ...POLICY, egressSource: 'file:/srv/agents/acme/rya.guard.yaml' },
        })}
        onToast={() => {}}
      />,
    )
    expect(screen.getByText('default deny · from project file')).toBeTruthy()
    // The absolute server path is noise on a dashboard and says where the process runs.
    expect(screen.queryByText(/srv\/agents/)).toBeNull()
  })

  it('distinguishes no policy at all from a policy it could not read', () => {
    render(
      <GovernanceView
        state={stateWith({
          enforcement: { ...ENFORCEMENT, egressGuard: false },
          policy: { ...POLICY, egressRules: 0, egressDefault: null, egressSource: 'none' },
        })}
        onToast={() => {}}
      />,
    )
    expect(screen.getByText('no policy — not enforced')).toBeTruthy()
    expect(screen.queryByText(/denying/)).toBeNull()
  })

})

describe('GovernanceView, degrading', () => {
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
