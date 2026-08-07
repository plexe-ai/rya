import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ConnectionsView } from './Connections'
import type { ConsoleState } from '../lib/types'

// No `fetch` stub here on purpose: ConnectionsView is a pure function of the
// `/console` aggregate the shell already polls. If this file ever needs a fetch
// mock, the view has grown a second source of truth for the same rows.

const stateWith = (connections: unknown[], tools: unknown[] = []) =>
  ({ agent: { name: 'support-agent' }, connections, tools } as unknown as ConsoleState)

const conn = (over: Record<string, unknown> = {}) => ({
  id: 'cx_1',
  provider: 'github',
  scopes: ['repo:read'],
  owner: null,
  status: 'active',
  secretSet: true,
  encrypted: true,
  ...over,
})

describe('ConnectionsView', () => {
  it('renders a connection with its scopes and status', () => {
    render(<ConnectionsView state={stateWith([conn()])} onToast={() => {}} />)
    expect(screen.getByText('github')).toBeTruthy()
    expect(screen.getByText('repo:read')).toBeTruthy()
    expect(screen.getByText('active')).toBeTruthy()
    expect(screen.getByText('active').className).toBe('stbadge ok')
    expect(screen.getByText('encrypted')).toBeTruthy()
  })

  it('reads an ownerless connection as shared rather than as missing data', () => {
    render(<ConnectionsView state={stateWith([conn({ owner: null })])} onToast={() => {}} />)
    expect(screen.getByText('shared')).toBeTruthy()
  })

  it('names the owner of a per-user credential', () => {
    render(<ConnectionsView state={stateWith([conn({ owner: 'ada@acme.io' })])} onToast={() => {}} />)
    expect(screen.getByText('ada@acme.io')).toBeTruthy()
    expect(screen.queryByText('shared')).toBeNull()
  })

  it('distinguishes an unencrypted secret from a missing one', () => {
    const { unmount } = render(
      <ConnectionsView state={stateWith([conn({ encrypted: false })])} onToast={() => {}} />,
    )
    expect(screen.getByText(/unencrypted/)).toBeTruthy()
    unmount()

    render(<ConnectionsView state={stateWith([conn({ secretSet: false })])} onToast={() => {}} />)
    expect(screen.getByText('none')).toBeTruthy()
    expect(screen.queryByText(/unencrypted/)).toBeNull()
  })

  it('marks a revoked connection', () => {
    render(<ConnectionsView state={stateWith([conn({ status: 'revoked' })])} onToast={() => {}} />)
    expect(screen.getByText('revoked')).toBeTruthy()
    expect(screen.queryByText('active')).toBeNull()
    // Red for the one status that really does mean revoked. This tone assertion holds
    // against the pre-§5.17 code too — it is here to stop the local status map losing
    // an entry, not to demonstrate the bug.
    expect(screen.getByText('revoked').className).toBe('stbadge fail')
  })

  /**
   * §5.17. `status` is optional on this view's `Connection` for the same reason every
   * field past `{id, provider, scopes}` is: the rows come straight off the store
   * record and `_public_connection` passes through whatever it finds. The old cell was
   * a two-arm ternary on `=== 'active'`, so an absent status landed in the else arm
   * and a connection whose secret is still set, that nobody has revoked, was reported
   * as revoked in red — sending an operator to re-issue a credential that is fine.
   */
  it('does not report a connection with no status as revoked', () => {
    render(<ConnectionsView state={stateWith([conn({ status: undefined })])} onToast={() => {}} />)
    expect(screen.queryByText('revoked')).toBeNull()
    expect(screen.queryByText('active')).toBeNull()
    // Amber, not green: `get_connection` resolves a credential only
    // `WHERE status = 'active'`, so this one will not be injected into a tool call at
    // runtime. Something is wrong; the console just does not know what.
    expect(screen.getByText('unknown').className).toBe('stbadge wait')
    // And it says WHY, which is this console's standing rule for an unknown (§5.4,
    // §5.10). The consequence is the useful half: an operator who knows the credential
    // will not be injected has something to act on, where a bare "unknown" only tells
    // them the page has stopped answering.
    expect(screen.getByText('unknown').getAttribute('title')).toMatch(/not be resolved into tool calls/i)
  })

  it('treats an explicit null status the same as an absent one', () => {
    render(<ConnectionsView state={stateWith([conn({ status: null })])} onToast={() => {}} />)
    expect(screen.getByText('unknown')).toBeTruthy()
    expect(screen.queryByText('revoked')).toBeNull()
  })

  /**
   * The other half of §5.17: every value that was not the literal `'active'` came out
   * as the specific word "revoked", so the console both invented a claim the server
   * never made and threw away the word the server did send.
   */
  it('prints a status it does not recognise verbatim, and leaves it neutral', () => {
    render(<ConnectionsView state={stateWith([conn({ status: 'expired' })])} onToast={() => {}} />)
    expect(screen.getByText('expired')).toBeTruthy()
    expect(screen.queryByText('revoked')).toBeNull()
    // Neutral grey — `StatusBadge`'s rule for an open vocabulary (`statusClass`
    // returns '' for anything it has not been taught). A tone here would be a verdict
    // on a word whose meaning this console does not know.
    expect(screen.getByText('expired').className).toBe('stbadge')
    // Neutral is not the same as unexplained: the tooltip quotes the word back and
    // names the consequence, so a status this console has never been taught still
    // tells an operator whether the credential is live.
    const t = screen.getByText('expired').getAttribute('title') ?? ''
    expect(t).toMatch(/'expired'/)
    expect(t).toMatch(/does not recognise/i)
  })

  it('does not clutter a status it does understand with a tooltip', () => {
    // The corollary, and the reason the `title` is conditional: `active` and `revoked`
    // mean what they say, and a hover explaining a word the operator already read is
    // noise that trains people to ignore the ones that matter.
    render(<ConnectionsView state={stateWith([conn({ status: 'active' })])} onToast={() => {}} />)
    expect(screen.getByText('active').getAttribute('title')).toBeNull()
  })

  it('shows an em dash for a connection that narrows no scopes', () => {
    render(<ConnectionsView state={stateWith([conn({ scopes: [] })])} onToast={() => {}} />)
    expect(screen.getByText('—')).toBeTruthy()
  })

  it('lists two connections to the same provider without collapsing them', () => {
    // The keying case: keyed on `provider` these two rows would collide.
    render(
      <ConnectionsView
        state={stateWith([
          conn({ id: 'cx_1', owner: null }),
          conn({ id: 'cx_2', owner: 'ada@acme.io' }),
        ])}
        onToast={() => {}}
      />,
    )
    expect(screen.getAllByText('github').length).toBe(2)
    expect(screen.getByText('shared')).toBeTruthy()
    expect(screen.getByText('ada@acme.io')).toBeTruthy()
  })

  it('reads no connections as an ordinary state, with the command that fixes it', () => {
    render(<ConnectionsView state={stateWith([])} onToast={() => {}} />)
    expect(screen.getByText(/No connections/)).toBeTruthy()
    expect(screen.getByText(/rya connect/)).toBeTruthy()
  })

  it('treats an absent connections field the same as an empty one', () => {
    // `snapshot.py` fills it, but the type marks it optional and a fresh install
    // must not render a crash.
    render(
      <ConnectionsView
        state={{ agent: { name: 'a' }, tools: [] } as unknown as ConsoleState}
        onToast={() => {}}
      />,
    )
    expect(screen.getByText(/No connections/)).toBeTruthy()
  })

  it('lists the scoped tools and the scopes each one requires', () => {
    render(
      <ConnectionsView
        state={stateWith([conn()], [
          { id: 'gh.issue.create', permission: 'allowed', provider: 'github', scopes: ['repo:write'] },
          { id: 'time.now', permission: 'allowed' },
        ])}
        onToast={() => {}}
      />,
    )
    expect(screen.getByText('Scoped tools')).toBeTruthy()
    expect(screen.getByText('gh.issue.create')).toBeTruthy()
    expect(screen.getByText('repo:write')).toBeTruthy()
    // A tool with no provider takes part in no intersection, so it is not listed.
    expect(screen.queryByText('time.now')).toBeNull()
  })

  it('omits the scoped-tools section entirely when no tool names a provider', () => {
    render(
      <ConnectionsView
        state={stateWith([conn()], [{ id: 'time.now', permission: 'allowed' }])}
        onToast={() => {}}
      />,
    )
    // An empty "Scoped tools" table would read as a missing binding.
    expect(screen.queryByText('Scoped tools')).toBeNull()
  })

  it('renders a hostile provider name as text', () => {
    const nasty = '<img src=x onerror=alert(1)>'
    render(<ConnectionsView state={stateWith([conn({ provider: nasty })])} onToast={() => {}} />)
    expect(screen.getByText(nasty)).toBeTruthy()
    expect(document.querySelector('img')).toBeNull()
  })
})
