import { ArrowRight, Bot, Package } from 'lucide-react'

import type { AgentRef } from '../lib/types'

/**
 * What the console shows when `GET /console` reports `agent: null`.
 *
 * That is a real state, not a failure — the route says so: "a fresh workspace with
 * nothing published yet still has a dashboard". It arrives in exactly two ways, and
 * they are kept apart deliberately:
 *
 *  - nothing published    a new workspace; what it needs is the publish command
 *  - several published    a choice; what it needs is the list, pointed at
 *
 * Collapsing them into one "no data" card is the tempting shortcut and the wrong
 * one: an operator whose workspace serves two agents would be left wondering what
 * they had failed to deploy.
 */
export function AgentChooser({
  roster,
  onSelect,
}: {
  roster: AgentRef[]
  onSelect: (name: string) => void
}) {
  if (!roster.length) {
    return (
      <div className="downcard">
        <div className="dic">
          <Package aria-hidden="true" focusable="false" />
        </div>
        <h3>Nothing published yet</h3>
        <p>
          This workspace serves no agents. Publish one from your agent repo with{' '}
          <span className="mono">rya publish --env prod</span>, and it appears here — the
          control plane learns what exists from published versions, so there is nothing
          else to register.
        </p>
      </div>
    )
  }

  return (
    <div className="downcard">
      <div className="dic">
        <Bot aria-hidden="true" focusable="false" />
      </div>
      <h3>
        This workspace serves {roster.length} agents
      </h3>
      <p>
        Pick one to see its runs, tools and approvals. Each has its own versions and its
        own environment pointer, so they promote and roll back independently.
      </p>
      <div className="dbtns">
        {roster.map((a) => (
          <button key={a.name} className="btn dark sm" onClick={() => onSelect(a.name)}>
            <ArrowRight aria-hidden="true" focusable="false" />
            {a.name}
          </button>
        ))}
      </div>
    </div>
  )
}
