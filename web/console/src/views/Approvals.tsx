import { useState } from 'react'
import { Check, ShieldQuestion } from 'lucide-react'
import { api } from '../lib/api'
import type { Approval, ConsoleState } from '../lib/types'
import { Empty, ViewHeader } from '../components/ui'

/**
 * How much of an action's arguments to render before truncating.
 *
 * A cap is needed — an action can carry an arbitrary payload and this view must not
 * become a megabyte of `<pre>` — but it is generous on purpose. The whole point of
 * this screen is that the operator sees what they are consenting to, so the failure
 * mode to avoid is hiding the one field that mattered.
 */
const MAX_ARGS_CHARS = 4000

function formatArgs(input: Record<string, unknown>): string {
  let text: string
  try {
    text = JSON.stringify(input, null, 2)
  } catch {
    // Circular or otherwise unserialisable. Say so rather than rendering nothing —
    // "we could not show you this" and "there was nothing to show" are different
    // facts, and only one of them is a reason not to approve.
    return '// arguments could not be displayed'
  }
  if (text.length <= MAX_ARGS_CHARS) return text
  return `${text.slice(0, MAX_ARGS_CHARS)}\n… truncated — inspect the run trace for the rest`
}

/**
 * The human-in-the-loop gate.
 *
 * Two things this view used to get wrong, both about what the operator can see:
 *
 *  1. **It rendered the workspace inbox under one agent's name.** `state.approvals` is
 *     `list_approvals("pending")`, deliberately workspace-wide, while every other key
 *     in the snapshot is scoped to the selected agent. The list is still workspace-wide
 *     — hiding a pending gate because a different agent is selected is how a run waits
 *     forever — but it now says so, and marks the rows that belong elsewhere.
 *  2. **The operator approved blind.** The server has always shipped `body` and the
 *     full `action`; this rendered neither. It cherry-picked `action.input.to` and drew
 *     a mail icon next to every row, so a pending `payments.refund` of £500,000 looked
 *     like an outgoing email. Approving is the only irreversible action in the console.
 */
export function ApprovalsView({
  state,
  agent,
  onToast,
  onResolved,
}: {
  state: ConsoleState
  /** The selected agent, so rows belonging to a different one can be marked. */
  agent: string | null
  onToast: (m: string) => void
  onResolved: () => Promise<void> | void
}) {
  // Per-approval, not a single flag: resolving one row must not disable the rest.
  const [pending, setPending] = useState<Record<string, boolean>>({})

  async function resolve(a: Approval, action: 'approve' | 'reject') {
    setPending((p) => ({ ...p, [a.id]: true }))
    try {
      const r = await api<{ runStatus: string }>(
        `/approvals/${encodeURIComponent(a.id)}/${action}`,
        { method: 'POST' },
      )
      onToast(`${action === 'approve' ? 'Approved' : 'Rejected'} → run ${r.runStatus}`)
      await onResolved()
    } catch (e) {
      onToast(`Error — ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      // Clear on both paths: on success the row disappears with the refreshed
      // state, and on failure the operator needs the buttons back.
      setPending((p) => {
        const { [a.id]: _drop, ...rest } = p
        return rest
      })
    }
  }

  const foreign = state.approvals.filter((a) => a.agent && agent && a.agent !== agent).length

  return (
    <>
      <ViewHeader title="Approvals">
        Every action awaiting a human in this workspace — not only the selected agent's.
        Approving resumes the real run, durably, from any worker.
      </ViewHeader>

      {state.approvals.length === 0 ? (
        <Empty>No pending approvals.</Empty>
      ) : (
        <>
          {foreign > 0 && (
            <div className="anote" role="status">
              {foreign} of these {foreign === 1 ? 'is' : 'are'} for another agent. Approving one
              resumes <em>that</em> agent's run.
            </div>
          )}
          {state.approvals.map((a) => {
            const tool = a.action?.tool ?? 'action'
            const input = a.action?.input
            const hasArgs = !!input && typeof input === 'object' && Object.keys(input).length > 0
            const elsewhere = !!a.agent && !!agent && a.agent !== agent
            const busy = !!pending[a.id]
            return (
              <div className="approw" key={a.id}>
                <div className="apphead">
                  {/* One neutral gate icon. This used to be a hardcoded envelope,
                      which asserted the action was an email for every approval in
                      the product. A wrong icon is worse than a generic one here. */}
                  <span className="aic">
                    <ShieldQuestion aria-hidden="true" focusable="false" />
                  </span>
                  <div>
                    <div className="nm">{a.title}</div>
                    <div className="ds">
                      <span className="mono">{tool}</span> · {a.runId}
                      {a.agent && (
                        <>
                          {' · '}
                          <span className={elsewhere ? 'apf' : undefined}>{a.agent}</span>
                        </>
                      )}
                    </div>
                  </div>
                  <div className="ar">
                    <button
                      className="btn approve sm"
                      onClick={() => void resolve(a, 'approve')}
                      disabled={busy}
                    >
                      <Check aria-hidden="true" focusable="false" />
                      Approve
                    </button>
                    <button
                      className="btn sm"
                      onClick={() => void resolve(a, 'reject')}
                      disabled={busy}
                    >
                      Reject
                    </button>
                  </div>
                </div>

                {a.body && <div className="appbody">{a.body}</div>}

                {hasArgs ? (
                  <pre className="appargs" aria-label={`Arguments for ${tool}`}>
                    {formatArgs(input)}
                  </pre>
                ) : (
                  // Silence here would read as "nothing will happen". An action with
                  // no arguments is a real thing; not knowing is not.
                  <div className="appnoargs">This action takes no arguments.</div>
                )}
              </div>
            )
          })}
        </>
      )}
    </>
  )
}
