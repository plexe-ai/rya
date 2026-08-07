import { Bot, ChevronsUpDown, GitCommitVertical, LogOut } from 'lucide-react'
import { NAV } from '../lib/nav'
import type { NavCounts, ViewId } from '../lib/nav'
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
  showing,
  switchFailed,
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
  /**
   * The operator's CHOICE — the agent every request in this console is addressed to.
   * This is what drives the `<select>`, and it has to: a control bound to anything
   * else fights the person using it (§5.14).
   */
  selected: string | null
  /**
   * The server's ECHO — the agent whose data is on the page right now. Differs from
   * `selected` for exactly as long as a switch is unresolved: one round trip when
   * things go well, and indefinitely when they do not.
   */
  showing: string | null
  /** Is the runtime failing? Separates "switching" from "stuck on the old agent". */
  switchFailed: boolean
  onSelectAgent: (name: string | null) => void
  counts: NavCounts
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

  // The choice and the echo disagree — the page is not showing the agent the console
  // is addressed to. Both must be present for this to mean anything: a `selected` with
  // no `showing` yet is a first load, not a disagreement.
  const mismatch = selected != null && showing != null && selected !== showing

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
          than a label. The leading placeholder appears only while nothing is selected.

          `value` is the operator's choice, never the server's echo. It used to be the
          echo, which made this control lag the click by a round trip and stick on the
          wrong name if that request failed (§5.14); the `.en` line below is where the
          gap between choice and data is now stated, instead of being hidden by
          reverting the control. */}
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
          {/* This line describes the DATA, so when the data belongs to a different
              agent than the one named above it, describing the data is the wrong
              thing to do — `v3 · prod` under the name `billing-agent` is a sentence
              about `support-agent` with the wrong subject.

              Two ways for that to happen and they want different words. A switch in
              flight is ordinary and resolves itself in a round trip. A switch that
              cannot complete is the §5.14 failure: the console keeps serving the old
              agent's runs, approvals and secrets while the selector, `ag()` and
              localStorage have all moved on, and previously the only clue was that
              the selector had silently snapped back. */}
          <div className="en" title={mismatch ? `The console is still showing ${showing}.` : undefined}>
            {mismatch ? (
              <span style={switchFailed ? { color: 'var(--amber)' } : undefined}>
                {switchFailed ? `still showing ${showing}` : `loading ${selected}…`}
              </span>
            ) : loaded ? (
              `v${loaded.agent.version} · ${loaded.agent.environment}`
            ) : state ? (
              roster.length ? (
                'select one above'
              ) : (
                'nothing published'
              )
            ) : (
              'loading…'
            )}
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
                  {/* Three renderings for three states — see `NavCount` (§5.10).
                      This was `{c.value || ''}`, which drew a real zero and a count
                      that could not be read identically, as nothing at all. Amber
                      is never applied to `—`: highlighting a number we do not have
                      is the same lie in a louder colour. */}
                  {c != null &&
                    (c.value == null ? (
                      <span className="ct unk" title="Not available — this count could not be read.">
                        —
                      </span>
                    ) : (
                      <span className={`ct${c.amber ? ' amber' : ''}`}>{c.value}</span>
                    ))}
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
