import { Bot, Menu, RefreshCw, Zap } from 'lucide-react'
import type { ConsoleState } from '../lib/types'

export function TopBar({
  state,
  live,
  loading,
  onRefresh,
  onSendEvent,
  onToggleNav,
}: {
  state: ConsoleState | null
  live: boolean
  loading: boolean
  onRefresh: () => void
  onSendEvent: () => void
  onToggleNav: () => void
}) {
  const badges = state
    ? [
        `env: ${state.agent.environment}`,
        `runtime: ${state.agent.runtime}`,
        `store: ${state.runtime.store}`,
        `llm: ${state.runtime.llmProvider}`,
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
        <h1>{(state?.branding && workspace) || state?.agent.name || '—'}</h1>
        <div className="meta">
          {state && (
            <>
              <span className="badge run">
                <span className="d" />
                {state.agent.status}
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
        <span className={`live${live ? '' : ' off'}`} role="status" aria-live="polite">
          <span className="d" />
          {loading ? 'connecting…' : live ? 'live' : 'offline'}
        </span>
        <button className="btn sm" onClick={onRefresh}>
          <RefreshCw aria-hidden="true" focusable="false" />
          Refresh
        </button>
        <button className="btn dark sm" onClick={onSendEvent}>
          <Zap aria-hidden="true" focusable="false" />
          Send test event
        </button>
      </div>
    </div>
  )
}
