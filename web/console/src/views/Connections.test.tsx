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
