import { useState } from 'react'
import {
  Check, CheckCircle2, Circle, Clock, Database, FileText, Layers, Play, Plug, ScanLine,
  Send, ShieldAlert, Sparkles, Webhook, XCircle,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { api } from '../lib/api'
import { filterLabel, filterRuns, runCounts, RUN_FILTERS } from '../lib/runs'
import type { RunFilter } from '../lib/runs'
import { num } from '../lib/format'
import type { ConsoleState, Run, TraceEvent } from '../lib/types'
import { Ago, CopyId, SecRow, StatusBadge, Table, ViewHeader } from '../components/ui'

const TRACE_ICON: Record<string, LucideIcon> = {
  'run.started': Play,
  'event.emit': Webhook,
  'memory.append': Database,
  'memory.set': Database,
  'memory.get': Database,
  'memory.search': Database,
  'tool.call': Plug,
  'model.call': Layers,
  'llm.respond': Sparkles,
  'approval.requested': ShieldAlert,
  'approval.approved': Check,
  'channel.send': Send,
  'job.schedule': Clock,
  log: FileText,
  'run.completed': CheckCircle2,
  'run.failed': XCircle,
}

/**
 * Runs & traces.
 *
 * This is the view the migration exists to fix. In the legacy console the 6s poll
 * re-rendered the whole filter block, so `renderRuns` had to sniff
 * `document.activeElement` and skip the re-render whenever the search box had
 * focus — otherwise typing was clobbered mid-keystroke. Here the filter is React
 * state and the table rows are keyed by run id, so a poll that changes the run
 * list cannot touch the input or the caret. The workaround is simply gone.
 */
export function RunsView({
  state,
  onToast,
}: {
  state: ConsoleState
  onToast: (m: string) => void
}) {
  const [status, setStatus] = useState<RunFilter>('all')
  const [query, setQuery] = useState('')
  const [trace, setTrace] = useState<{ id: string; events: TraceEvent[] } | null>(null)

  const counts = runCounts(state.runs)
  const rows = filterRuns(state.runs, status, query)

  async function openTrace(run: Run) {
    try {
      const t = await api<{ trace: TraceEvent[] }>(`/runs/${encodeURIComponent(run.id)}/trace`)
      setTrace({ id: run.id, events: t.trace })
    } catch (e) {
      onToast(`Trace error — ${e instanceof Error ? e.message : String(e)}`)
    }
  }

  return (
    <>
      <ViewHeader title="Runs &amp; traces">
        Every run is a forensic record — input event, every tool and model call, approvals,
        retries, final status.
      </ViewHeader>

      <div className="filters">
        {RUN_FILTERS.map((f) => (
          <button
            key={f}
            className={`fpill${status === f ? ' on' : ''}`}
            onClick={() => setStatus(f)}
            aria-pressed={status === f}
          >
            {filterLabel(f)}
            {counts[f] ? ` · ${counts[f]}` : ''}
          </button>
        ))}
        <input
          className="fsearch"
          placeholder="Filter by run id or trigger…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Filter runs"
        />
      </div>

      <Table
        rows={rows}
        rowKey={(r) => r.id}
        onRowClick={(r) => void openTrace(r)}
        rowLabel={(r) => `Open trace for ${r.id}`}
        emptyIcon={ScanLine}
        emptyMessage={
          state.runs.length ? 'No runs match this filter.' : 'No runs yet — hit "Send test event".'
        }
        columns={[
          { header: 'Run', cell: (r) => <CopyId id={r.id} onCopied={onToast} /> },
          { header: 'Trigger', cell: (r) => r.trigger },
          { header: 'Status', cell: (r) => <StatusBadge status={r.status} /> },
          { header: 'Tokens', cell: (r) => num(r.tokens) },
          { header: 'When', cell: (r) => <Ago ts={r.createdAt} />, className: 'dim' },
        ]}
      />

      {trace && (
        <>
          <SecRow left={`Trace · ${trace.id}`} right={<span className="mono">{trace.events.length} steps</span>} />
          <div className="window" style={{ padding: '6px 16px' }}>
            <div className="trace">
              {trace.events.map((ev, i) => {
                const Icon = TRACE_ICON[ev.kind] ?? Circle
                const amber = ev.kind.startsWith('approval')
                return (
                  <div className="tev" key={`${ev.ts ?? ''}-${i}`}>
                    <span className={`tdot${amber ? ' amber' : ''}`}>
                      <Icon aria-hidden="true" focusable="false" />
                    </span>
                    <div>
                      <div className="tk">{ev.kind}</div>
                      <div className="tm">{ev.label ?? ''}</div>
                    </div>
                    <span className="tt">{(ev.ts ?? '').slice(11, 19)}</span>
                  </div>
                )
              })}
            </div>
          </div>
        </>
      )}
    </>
  )
}
