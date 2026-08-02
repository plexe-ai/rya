import { useState } from 'react'
import { Check, Mail } from 'lucide-react'
import { api } from '../lib/api'
import type { Approval, ConsoleState } from '../lib/types'
import { Empty, ViewHeader } from '../components/ui'

export function ApprovalsView({
  state,
  onToast,
  onResolved,
}: {
  state: ConsoleState
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

  return (
    <>
      <ViewHeader title="Approvals">
        External actions pause here. Approving resumes the real run — durably, from any worker.
      </ViewHeader>

      {state.approvals.length === 0 ? (
        <Empty>No pending approvals.</Empty>
      ) : (
        state.approvals.map((a) => {
          const tool = a.action?.tool ?? 'action'
          const to = typeof a.action?.input?.to === 'string' ? a.action.input.to : ''
          const busy = !!pending[a.id]
          return (
            <div className="approw" key={a.id}>
              <span className="aic">
                <Mail aria-hidden="true" focusable="false" />
              </span>
              <div>
                <div className="nm">{a.title}</div>
                <div className="ds">
                  <span className="mono">{tool}</span>
                  {to && ` → ${to}`} · {a.runId}
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
                <button className="btn sm" onClick={() => void resolve(a, 'reject')} disabled={busy}>
                  Reject
                </button>
              </div>
            </div>
          )
        })
      )}
    </>
  )
}
