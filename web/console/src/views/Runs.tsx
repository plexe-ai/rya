import { useEffect, useRef, useState } from 'react'
import {
  Check, CheckCircle2, Circle, Clock, Database, FileText, Layers, Play, Plug, ScanLine,
  Send, ShieldAlert, Sparkles, Webhook, XCircle,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { api } from '../lib/api'
import { ag } from '../lib/agent'
import { filterLabel, pillCounts, runsSignature, RUN_FILTERS } from '../lib/runs'
import type { RunFilter } from '../lib/runs'
import { num } from '../lib/format'
import type { ConsoleState, RunStatus, TraceEvent } from '../lib/types'
import { Ago, CopyId, Empty, SecRow, StatusBadge, Table, ViewHeader } from '../components/ui'

// ---- shapes -----------------------------------------------------------------
// Declared here rather than in lib/types.ts: this endpoint has exactly one
// consumer, and per-view response shapes are kept next to it (the same call
// `Conversations.tsx` makes for `/sessions`).

/**
 * One row of `GET /agents/{agent}/runs?summary=1`.
 *
 * `summary=1` is not optional politeness — without it the route answers with full
 * run DOCUMENTS, every trace event included, which for a 50-row page is megabytes
 * of evidence to render five columns. The table reads a subset of these fields;
 * the rest are declared because they are the contract, and because the next column
 * anyone adds here should not need a server change to know what it can have.
 */
export interface RunRow {
  id: string
  status: RunStatus
  /**
   * Nullable, unlike `types.ts: Run`, because the server's projection is
   * `run.get("trigger")` and a run document is not obliged to carry one. Declaring
   * it `string` here would be the §4.9 mistake in miniature: a type that promises
   * more than the route sends, believed by `tsc` and by the next person to add a
   * column.
   */
  trigger?: string | null
  createdAt?: string | null
  pendingApproval?: string | null
  error?: string | null
  traceLength?: number
  tokens?: number
  costUsd?: number | null
}

/**
 * `GET /agents/{agent}/runs?summary=1&limit=&offset=[&status=][&q=]`.
 *
 * `count` is the size of the FILTERED set, not the length of `runs` — it is the N in
 * "showing 50 of 412", and the only thing that can tell us whether another page
 * exists. Reading it off the page length instead is how a truncated list comes to
 * look complete, which is the whole of §5.1.
 */
export interface RunsPage {
  runs?: RunRow[]
  count?: number
  limit?: number
  offset?: number
}

/** Rows per page. One "Load more" adds another 50. */
const PAGE = 50

/**
 * How long the search box waits before asking the server.
 *
 * Long enough that a typed run id is one request rather than one per keystroke,
 * short enough not to feel like a submit. Filtering is server-side now, so every
 * keystroke would otherwise be a query over the whole run table.
 */
const QUERY_DEBOUNCE_MS = 250

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
 *
 * **Audit §5.1.** The port carried over one thing it should not have: it treated
 * `state.runs` as the run list. It is not — it is a 30-row PREVIEW inside a
 * dashboard aggregate (`snapshot.py`, `runs[:30]`), and filtering, searching and
 * counting inside it produced a view that was confidently wrong in three ways at
 * once. The pills disagreed with the Overview tile a few pixels above them; there
 * was no pagination and no "showing 30 of N", so a 412-run workspace looked like a
 * 30-run one; and searching for the id of run number 31 answered "No runs match" —
 * the console telling an operator holding a real run id that it does not exist.
 *
 * So the table owns its own request now: `GET /agents/{a}/runs?summary=1&limit&…`
 * with `status` and `q` as server-side parameters, over every run rather than over
 * a page. The pills read `state.stats` (`byStatus` and `runs`), which the server
 * computes over the whole set, so they cannot drift from the tile.
 *
 * Three properties of that arrangement are load-bearing:
 *
 *  1. **Paging is `offset=0&limit=pages*PAGE`, refetched whole.** Not
 *     accumulate-and-merge: the list is sorted newest-first and new runs arrive
 *     while the operator reads, so appending page 2 to a page 1 that has since
 *     shifted duplicates rows and hides others, and no merge key fixes that
 *     because the boundary itself moved. Refetching the window is a few more bytes
 *     for a window that is always internally consistent.
 *  2. **It stays as live as the poll was.** Rows used to update within 6s for free
 *     because they came from the shell. The fetch depends on `runsSignature(stats)`,
 *     so it refetches exactly when the run numbers move — no second timer, and a
 *     status change counts, not just a new run.
 *  3. **Loading and failure never render as "no data".** An empty table means one
 *     of three things and says which: nothing has run yet, the filter is too
 *     narrow, or the request failed. Collapsing the third into either of the first
 *     two is the outage-vs-idle confusion this console exists to prevent (§5.4,
 *     §5.10), and it is the specific reason "No runs match this filter" must never
 *     be shown for a fetch that did not answer.
 */
export function RunsView({
  state,
  onToast,
}: {
  state: ConsoleState
  onToast: (m: string) => void
}) {
  const [status, setStatus] = useState<RunFilter>('all')
  /** What is in the box, keystroke by keystroke. React-controlled — see above. */
  const [query, setQuery] = useState('')
  /** What has been asked of the server. Trails `query` by `QUERY_DEBOUNCE_MS`. */
  const [q, setQ] = useState('')
  /** Pages loaded, so the window is always `pages * PAGE` rows from offset 0. */
  const [pages, setPages] = useState(1)
  /**
   * The loaded window and the size of the set it came from, kept as one value so a
   * response cannot leave rows from one request beside the count from another —
   * which is exactly the mismatch that would make "showing 50 of 412" a lie.
   */
  const [page, setPage] = useState<{ rows: RunRow[]; count: number; limit: number } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  /**
   * Bumped by Retry, and a fetch dependency for that alone.
   *
   * Without it a failed window has no way back. The shell's Refresh button reloads
   * `/console`, and if the run numbers come back unchanged the signature does not
   * move and nothing here refetches — so a transient failure would sit on screen
   * until the operator happened to navigate away and back. §5.9 is the same defect
   * one level up: "stale data ... no age indicator, no retry".
   */
  const [retries, setRetries] = useState(0)
  const [trace, setTrace] = useState<{ id: string; events: TraceEvent[] } | null>(null)

  const counts = pillCounts(state.stats)
  const signature = runsSignature(state.stats)

  // Agent-scoped via `ag()`. The unprefixed `/runs` still answers, through the
  // deprecated Rule 6 fallback that resolves the reserved `_` alias — and that
  // alias refuses with `E_AGENT_AMBIGUOUS` the moment a workspace serves a second
  // agent, so the prefix is not a nicety (console/AGENTS.md, `lib/agent.ts`).
  // How wide a window is being asked for. `pages` is never sent as such — the
  // request is always `offset=0` over a growing `limit`, which is what keeps the
  // window internally consistent while the list re-sorts underneath it.
  const wanted = pages * PAGE
  const params = new URLSearchParams({ summary: '1', limit: String(wanted), offset: '0' })
  // Omitted rather than sent empty: `status=` and `q=` are absent-means-everything,
  // and an empty-string filter is a different request to make the server reason about.
  if (status !== 'all') params.set('status', status)
  if (q) params.set('q', q)
  const path = ag(state.agent.name, `/runs?${params.toString()}`)

  // Monotonic request token. Page 2 is requested while page 1 may still be in
  // flight, and the two differ only in `limit`, so a slow page 1 landing last would
  // silently shrink the window back — and take the "Load more" button with it, which
  // reads as the button having done nothing. Same guard, same reason, as the
  // transcript fetch in `Conversations.tsx`.
  const seq = useRef(0)

  useEffect(() => {
    const mine = ++seq.current
    setLoading(true)
    void (async () => {
      try {
        const p = await api<RunsPage>(path)
        if (mine !== seq.current) return
        const rows = p.runs ?? []
        setPage({
          rows,
          count: typeof p.count === 'number' ? p.count : rows.length,
          // The limit the server actually applied — it clamps to 1..500, and the
          // echo is how we learn we asked for more than it will give. Defaulting to
          // what we asked for means a server that omits the echo is taken at its
          // word rather than treated as having refused.
          limit: typeof p.limit === 'number' ? p.limit : wanted,
        })
        setError(null)
      } catch (e) {
        if (mine !== seq.current) return
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        if (mine === seq.current) setLoading(false)
      }
    })()
    // `path` folds agent, status, query and page size into one dependency (`wanted`
    // is already inside it, and is declared because it is read); `signature` is the
    // liveness one and `retries` the manual one. Nothing else may go in here — `state`
    // is a brand-new object on every 6s poll, so depending on it would refetch forever.
  }, [path, wanted, signature, retries])

  // Debounce the query into `q`. Guarded on there being a change so that mounting,
  // and every re-render the poll causes, does not arm a timer that resolves to the
  // value it already had.
  useEffect(() => {
    const next = query.trim()
    if (next === q) return
    const t = setTimeout(() => {
      setQ(next)
      // A different query is a different result set, so the window starts over.
      // Carrying `pages` across would ask for 150 rows of something the operator
      // has not seen the first of.
      setPages(1)
    }, QUERY_DEBOUNCE_MS)
    return () => clearTimeout(t)
  }, [query, q])

  async function openTrace(run: RunRow) {
    try {
      // Workspace-scoped, NOT agent-prefixed: a run id already identifies its
      // agent, and the server serves `/runs/{id}/trace` unprefixed only.
      const t = await api<{ trace: TraceEvent[] }>(`/runs/${encodeURIComponent(run.id)}/trace`)
      setTrace({ id: run.id, events: t.trace })
    } catch (e) {
      onToast(`Trace error — ${e instanceof Error ? e.message : String(e)}`)
    }
  }

  const rows = page?.rows ?? []
  const total = page?.count ?? 0
  const missing = rows.length < total
  /**
   * True once the server has refused to widen the window any further — it clamps
   * `limit` to 500 (`_page_limit`), so a workspace with more runs than that cannot
   * be reached by growing one request, however many times the button is pressed.
   *
   * Detected from the echoed `limit` rather than by hard-coding 500 here: a copy of
   * the server's ceiling in the client is a constant that goes stale silently, and
   * the failure it produces is a button that looks live and does nothing.
   */
  const atCeiling = page !== null && page.limit < wanted
  const more = missing && !atCeiling

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
            onClick={() => {
              setStatus(f)
              // Same reason as the query: a new filter is a new result set.
              setPages(1)
            }}
            aria-pressed={status === f}
          >
            {filterLabel(f)}
            {/* From `stats`, over every run — never from the loaded rows. A pill
                that counted the page said "failed · 2" next to a tile saying 412
                total, and sent the operator looking for 12 failures in a list of
                30 (§5.1). Zero still renders bare rather than as "· 0". */}
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

      {error ? (
        // Never the table's empty state. "No runs match this filter" for a request
        // that failed is the console inventing an answer, and it is indistinguishable
        // from the true one — so a failure gets its own words, and an unauthenticated
        // console says so rather than being reported as an outage (`Queue.tsx`).
        <Empty icon={ScanLine}>
          {error === 'unauthorized' ? (
            // No Retry here: the remedy is the Connect dialog, which the shell's own
            // poll raises for a credential failure. A button that re-sends the same
            // rejected credential is theatre.
            'Connect to load runs.'
          ) : (
            <>
              {`Runs unavailable — ${error}`}{' '}
              <button className="btn sm" onClick={() => setRetries((n) => n + 1)}>
                Retry
              </button>
            </>
          )}
        </Empty>
      ) : (
        <Table
          rows={rows}
          rowKey={(r) => r.id}
          onRowClick={(r) => void openTrace(r)}
          rowLabel={(r) => `Open trace for ${r.id}`}
          emptyIcon={ScanLine}
          emptyMessage={
            loading
              ? 'Loading runs…'
              : // The discriminator is the authoritative total, not the loaded row
                // count: on a fresh install `stats.runs` is 0 whatever filter is
                // set, and "No runs match this filter" would blame the filter for
                // an agent that has simply never run. This condition used to read
                // `state.runs.length`, i.e. "is the 30-row preview non-empty".
                counts.all === 0
                ? 'No runs yet — hit "Send test event".'
                : 'No runs match this filter.'
          }
          columns={[
            { header: 'Run', cell: (r) => <CopyId id={r.id} onCopied={onToast} /> },
            { header: 'Trigger', cell: (r) => r.trigger || '—' },
            { header: 'Status', cell: (r) => <StatusBadge status={r.status} /> },
            { header: 'Tokens', cell: (r) => num(r.tokens) },
            { header: 'When', cell: (r) => <Ago ts={r.createdAt} />, className: 'dim' },
          ]}
        />
      )}

      {!error && rows.length > 0 && (
        // The honesty line the view did not have. Without it a window is
        // indistinguishable from a complete list, and the operator has no way to
        // know that the run they are looking for is simply further down.
        <div className="filters" style={{ marginTop: 12 }}>
          <span className="dim mono">{`showing ${num(rows.length)} of ${num(total)}`}</span>
          {/* Offered only while rows are genuinely missing: a control that refetches
              the window already on screen is one that appears to do nothing. */}
          {more ? (
            <button
              className="btn sm"
              style={{ marginLeft: 'auto' }}
              onClick={() => setPages((p) => p + 1)}
              // A second click while page N+1 is in flight would ask for N+2 and
              // discard the request already paid for.
              disabled={loading}
            >
              {loading ? 'Loading…' : 'Load more'}
            </button>
          ) : (
            missing && (
              // Rows are still missing and the button is gone, which needs saying:
              // silently dropping the affordance at the ceiling would read as "that
              // is all of them", and this view's entire fault was answering
              // confidently about runs it had not been given.
              <span className="dim" style={{ marginLeft: 'auto' }}>
                as wide as one request goes — filter by status or search to reach the rest
              </span>
            )
          )}
        </div>
      )}

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
