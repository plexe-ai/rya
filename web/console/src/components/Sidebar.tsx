import { Bot, ChevronsUpDown, GitCommitVertical, LogOut } from 'lucide-react'
import { NAV } from '../lib/nav'
import type { CountKey, ViewId } from '../lib/nav'
import { hasAgent } from '../lib/types'
import type { AgentRef, ConsoleResponse } from '../lib/types'

/** The Plexe mark. Inline because it is a brand asset, not an icon-set glyph. */
function PlexeMark() {
  return (
    <svg viewBox="0 0 532 507" aria-hidden="true">
      <path fill="currentColor" d="M247 0h38v88h-38z" />
      <path fill="currentColor" d="M266 0l52 88h-104z" />
      <rect fill="currentColor" x="60" y="118" width="412" height="330" rx="165" />
      <rect fill="currentColor" x="0" y="228" width="70" height="120" rx="30" />
      <rect fill="currentColor" x="462" y="228" width="70" height="120" rx="30" />
      <rect fill="currentColor" x="80" y="438" width="372" height="69" rx="34" />
      <rect fill="var(--bg,#fff)" x="100" y="158" width="332" height="250" rx="125" />
      <rect fill="currentColor" x="172" y="228" width="56" height="110" rx="28" />
      <rect fill="currentColor" x="304" y="228" width="56" height="110" rx="28" />
    </svg>
  )
}

export function Sidebar({
  view,
  onNavigate,
  state,
  roster,
  selected,
  onSelectAgent,
  counts,
  open,
  onWorkspaceClick,
  onSignOut,
}: {
  view: ViewId
  onNavigate: (v: ViewId) => void
  state: ConsoleResponse | null
  roster: AgentRef[]
  selected: string | null
  onSelectAgent: (name: string | null) => void
  counts: Partial<Record<CountKey, { value: number; amber?: boolean }>>
  open: boolean
  onWorkspaceClick: () => void
  onSignOut: () => void
}) {
  const branding = state?.branding
  const viewer = state?.viewer
  // Workspace chrome is agent-INDEPENDENT and must render whether or not one is
  // selected — it is how you tell which tenant you are looking at, which matters
  // most in exactly the case where no agent has been chosen yet.
  const loaded = hasAgent(state) ? state : null
  const wsName =
    branding?.name ??
    (viewer?.workspace && viewer.workspace !== 'default' ? viewer.workspace : 'Default workspace')
  const wsSub =
    branding?.tagline ??
    (loaded ? `${loaded.agent.environment} · ${loaded.runtime.store}` : state ? 'no agent selected' : 'loading…')

  const who = viewer?.user || (viewer?.mode === 'multi-tenant' ? 'workspace key' : 'operator')
  const initials = (viewer?.user?.includes('@') ? viewer.user.slice(0, 2) : loaded?.agent.name?.slice(0, 2) || 'ry')
    .toUpperCase()

  return (
    <aside className={`side${open ? ' open' : ''}`}>
      <button className="ws" aria-label="Workspace" onClick={onWorkspaceClick}>
        <div className="ws-logo">
          {branding?.logo ? <img src={branding.logo} alt="" /> : <GitCommitVertical aria-hidden="true" focusable="false" />}
        </div>
        <div>
          <div className="ws-name">{wsName}</div>
          <div className="ws-sub">{wsSub}</div>
        </div>
        <span className="chev">
          <ChevronsUpDown aria-hidden="true" focusable="false" />
        </span>
      </button>

      {/* A real <select> once the workspace serves more than one agent, plain text
          while it serves one — a control that implies a choice nobody has is worse
          than a label. The leading placeholder appears only while nothing is
          selected, so the control never shows an agent the page is not showing. */}
      <div className="agent-pick">
        <span className="ai">
          <Bot aria-hidden="true" focusable="false" />
        </span>
        <div>
          {roster.length > 1 ? (
            <select
              aria-label="Agent"
              value={selected ?? ''}
              onChange={(e) => onSelectAgent(e.target.value || null)}
            >
              {!selected && <option value="">Choose an agent…</option>}
              {roster.map((a) => (
                <option key={a.name} value={a.name}>
                  {a.name}
                </option>
              ))}
            </select>
          ) : (
            <div className="nm">{loaded?.agent.name ?? (state ? 'No agents yet' : '—')}</div>
          )}
          <div className="en">
            {loaded
              ? `v${loaded.agent.version} · ${loaded.agent.environment}`
              : state
                ? roster.length
                  ? 'select one above'
                  : 'nothing published'
                : 'loading…'}
          </div>
        </div>
        <span className="dot" />
      </div>

      {NAV.map((group) => (
        <div key={group.title}>
          <div className="grp">{group.title}</div>
          <nav className="nav">
            {group.items.map((item) => {
              const c = item.count ? counts[item.count] : undefined
              const Icon = item.icon
              return (
                <button
                  key={item.id}
                  className={view === item.id ? 'on' : undefined}
                  onClick={() => onNavigate(item.id)}
                  aria-current={view === item.id ? 'page' : undefined}
                >
                  <Icon aria-hidden="true" focusable="false" />
                  {item.label}
                  {c != null && (
                    <span className={`ct${c.amber ? ' amber' : ''}`}>{c.value || ''}</span>
                  )}
                </button>
              )
            })}
          </nav>
        </div>
      ))}

      <div className="pwr">
        Powered by <PlexeMark />
        <b>Plexe</b> · Rya runtime
      </div>

      <div className="side-foot">
        <div className="ava">{initials}</div>
        <div>
          <div className="me">{who}</div>
          <div className="em">{viewer?.mode ?? ''}</div>
        </div>
        <button
          className="btn sm"
          style={{ marginLeft: 'auto', padding: '5px 8px' }}
          aria-label="Sign out / change token"
          title="Sign out"
          onClick={onSignOut}
        >
          <LogOut aria-hidden="true" focusable="false" />
        </button>
      </div>
    </aside>
  )
}
