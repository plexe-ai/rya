import { Bot, Menu, RefreshCw, Zap } from 'lucide-react'
import { since } from '../lib/format'
import { hasAgent } from '../lib/types'
import type { ConsoleResponse } from '../lib/types'

export function TopBar({
  state,
  live,
  loading,
  lastSuccessAt,
  now,
  onRefresh,
  onSendEvent,
  onToggleNav,
}: {
  state: ConsoleResponse | null
  live: boolean
  loading: boolean
  /** Epoch ms of the last good poll, or null if there has never been one. */
  lastSuccessAt: number | null
  /** The shell's shared clock, so this and the stale banner quote one instant. */
  now: number
  onRefresh: () => void
  onSendEvent: () => void
  onToggleNav: () => void
}) {
  // Every badge here describes the SELECTED AGENT, so with none selected there is
  // nothing truthful to show and the row is simply empty. `Send test event` is
  // disabled for the same reason: it posts to an agent-scoped route.
  const loaded = hasAgent(state) ? state : null
  const badges = loaded
    ? [
        `env: ${loaded.agent.environment}`,
        `runtime: ${loaded.agent.runtime}`,
        `store: ${loaded.runtime.store}`,
        `llm: ${loaded.runtime.llmProvider}`,
      ]
    : []

  const workspace =
    state?.viewer?.workspace && state.viewer.workspace !== 'default' ? state.viewer.workspace : null

  return (
    <div className="top">
      <button className="hamburger" aria-label="Open navigation" onClick={onToggleNav}>
        <Menu aria-hidden="true" focusable="false" />
      </button>
      <div className="big-ic">
        <Bot aria-hidden="true" focusable="false" />
      </div>
      <div>
        <h1>
          {(state?.branding && workspace) ||
            loaded?.agent.name ||
            (state ? 'No agent selected' : '—')}
        </h1>
        <div className="meta">
          {loaded && (
            <>
              <span className="badge run">
                <span className="d" />
                {loaded.agent.status}
              </span>
              {badges.map((b) => (
                <span className="badge mono" key={b}>
                  {b}
                </span>
              ))}
            </>
          )}
        </div>
      </div>
      <div className="actions">
        {/* The word is in its own element and the age is in another, deliberately:
            "offline" is the state, the age is a qualifier on it, and gluing them
            into one text node would make the pill unreadable to anything looking
            for the state — including the tests that pin it.

            The age is the §5.9 half. `offline` on its own is true of a runtime that
            blinked two seconds ago and of one that died before this tab was opened,
            and the operator's next move is different for each. */}
        <span className={`live${live ? '' : ' off'}`} role="status" aria-live="polite">
          <span className="d" />
          <span>{loading ? 'connecting…' : live ? 'live' : 'offline'}</span>
          {!live && !loading && lastSuccessAt != null && (
            <span className="dim">· {since(lastSuccessAt, now)} old</span>
          )}
        </span>
        <button className="btn sm" onClick={onRefresh}>
          <RefreshCw aria-hidden="true" focusable="false" />
          Refresh
        </button>
        <button
          className="btn dark sm"
          onClick={onSendEvent}
          disabled={!loaded}
          title={loaded ? undefined : 'Select an agent first — an event is addressed to one'}
        >
          <Zap aria-hidden="true" focusable="false" />
          Send test event
        </button>
      </div>
    </div>
  )
}
