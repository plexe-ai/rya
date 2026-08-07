import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Sidebar } from './Sidebar'
import type { NavCounts } from '../lib/nav'
import type { ConsoleResponse } from '../lib/types'

/**
 * Sidebar counts — audit §5.10.
 *
 * The badge was `{c.value || ''}`, so a workspace with **0 workers** and a workspace
 * whose workers endpoint was **down** drew the same thing: nothing. That is the
 * outage-versus-idle confusion `app.py`'s workers route has a comment forbidding,
 * reproduced one panel to the left — and it is the worse half of the pair, because
 * scale-to-zero is the DESIGNED state for an idle key, so an operator has every
 * reason to read the blank as "fine" and stop looking.
 *
 * Three states, three renderings. Both directions are pinned: a fabricated blank for
 * a real zero is the bug, and a fabricated `—` for a real zero would be the same bug
 * with the sign flipped.
 */

const STATE = {
  agent: {
    name: 'support-agent',
    version: '1',
    environment: 'dev',
    status: 'ready',
    runtime: 'python',
    handlers: null,
  },
  agents: [{ name: 'support-agent' }],
  runtime: { store: 'file', llmProvider: 'anthropic', multiTenant: false },
  viewer: { workspace: 'default', mode: 'single-tenant', user: 'operator' },
} as unknown as ConsoleResponse

function mount(counts: NavCounts) {
  return render(
    <Sidebar
      view="overview"
      onNavigate={vi.fn()}
      state={STATE}
      roster={[{ name: 'support-agent' }]}
      selected="support-agent"
      showing="support-agent"
      switchFailed={false}
      onSelectAgent={vi.fn()}
      counts={counts}
      open={false}
      onWorkspaceClick={vi.fn()}
      onSignOut={vi.fn()}
    />,
  )
}

/** The count badge on a nav row, or null when the row has none. */
const badge = (label: string) =>
  screen.getByRole('button', { name: new RegExp(label) }).querySelector('.ct')

describe('the sidebar count badge (§5.10)', () => {
  it('draws a real zero as 0, not as a blank', () => {
    mount({ workers: { value: 0 } })
    expect(badge('Workers')?.textContent).toBe('0')
  })

  it('draws a count that could not be read as —, and says so on hover', () => {
    mount({ workers: { value: null } })
    const b = badge('Workers')
    expect(b?.textContent).toBe('—')
    // The tooltip is the whole difference between "there are none" and "we do not
    // know" for an operator who is deciding whether to go and look at Docker.
    expect(b?.getAttribute('title')).toMatch(/[Nn]ot available/)
  })

  it('tells the two apart on the same render', () => {
    // The finding in one assertion: these two rows described identical pixels.
    mount({ workers: { value: 0 }, versions: { value: null } })
    expect(badge('Workers')?.textContent).toBe('0')
    expect(badge('Versions')?.textContent).toBe('—')
  })

  it('draws no badge at all for a count the shell did not supply', () => {
    // The third state, and the reason `—` could not simply become the default: a
    // console that has not finished its first load must not flash `—` across a
    // healthy sidebar. Absent means "not attempted", and it stays blank.
    mount({})
    expect(badge('Workers')).toBeNull()
    expect(badge('Tools')).toBeNull()
  })

  it('never renders a badge on a nav row that has no count', () => {
    mount({ workers: { value: 3 } })
    // Overview and Runs carry no `count` key at all — a number there would be
    // inventing one.
    expect(badge('Overview')).toBeNull()
    expect(badge('Runs & traces')).toBeNull()
  })

  it('applies amber to a real number but never to an unknown', () => {
    mount({ approvals: { value: 2, amber: true } })
    expect(badge('Approvals')?.className).toContain('amber')

    // Amber on `—` would be a fabricated alarm — the same lie the fabricated zero
    // was, in a louder colour.
    mount({ approvals: { value: null, amber: true } })
    const bs = screen.getAllByRole('button', { name: /Approvals/ })
    const b = bs[bs.length - 1]!.querySelector('.ct')
    expect(b?.textContent).toBe('—')
    expect(b?.className).not.toContain('amber')
  })
})
