// The pure part of the Runs view: the filter vocabulary, and the numbers on the
// pills.
//
// `filterRuns` and `runCounts` used to live here and are gone. Filtering and
// searching are the server's job now (`GET /agents/{a}/runs?status=&q=`), because
// this module was only ever handed the 30-row preview inside the `/console`
// aggregate — so "no runs match" was a claim about a page, and `runCounts` counting
// that page IS audit finding §5.1. Their semantics were not changed, only moved:
// `status` is still an equality match with 'all' passing everything, and `q` is still
// a case-insensitive substring of the run id or the trigger. What changed is the set
// they run over, which is now all of it.

export const RUN_FILTERS = ['all', 'completed', 'waiting_approval', 'failed'] as const
export type RunFilter = (typeof RUN_FILTERS)[number] | (string & {})

/**
 * The only two fields of `/console`'s `stats` that count runs.
 *
 * Structural rather than `Stats` so this stays callable — and testable — with the
 * two numbers it actually reads instead of a whole aggregate. `Stats` is assignable
 * to it, so the call site in `Runs.tsx` is still type-checked against the server
 * shape.
 */
export interface RunTotals {
  /** Every run of the selected agent, not a page of them (`snapshot.py: len(runs)`). */
  runs?: number
  /** Per-status counts over the same complete set. */
  byStatus?: Record<string, number>
}

/**
 * Counts for the Runs filter pills, taken from the authoritative totals.
 *
 * This replaces `runCounts(state.runs)`, which was audit finding §5.1: `/console`
 * is a dashboard aggregate whose `runs` key is a 30-row PREVIEW (`snapshot.py`,
 * `runs[:30]`), so counting it told the operator there were 30 runs — 4 of them
 * failed — on a workspace with 412 runs and 12 failures, a few pixels away from an
 * Overview tile reading 412 off `stats.runs`. Two numbers, one truth, and the wrong
 * one was the one attached to the control you press to go looking.
 *
 * `stats.byStatus` and `stats.runs` are computed server-side over every run of the
 * selected agent, so a pill built from them cannot disagree with the tile, and cannot
 * shrink when a page happens to be short.
 */
export function pillCounts(stats: RunTotals | null | undefined): Record<string, number> {
  const byStatus = stats?.byStatus ?? {}
  const summed = Object.values(byStatus).reduce((a, b) => a + (b || 0), 0)
  return {
    ...byStatus,
    // `all` is written LAST and from `stats.runs` deliberately: it is the number the
    // Overview tile shows, and a `byStatus` that ever carried an `all` key must not
    // be allowed to overwrite it. The sum is a fallback for an aggregate that omitted
    // `runs` — reading 0 beside populated per-status pills would be worse than
    // approximating the total from the parts.
    all: typeof stats?.runs === 'number' ? stats.runs : summed,
  }
}

/**
 * A signature over the run numbers, used as a fetch dependency.
 *
 * The Runs table owns its own paged request, but it must stay as live as it was when
 * the rows came straight from the shell's 6s poll — a new run has always appeared
 * within six seconds and regressing that would be a silent downgrade. Rather than
 * add a second timer, the table refetches when this string changes, so a request
 * goes out exactly when the numbers moved and not once otherwise.
 *
 * `byStatus` is included, sorted, for a specific case the total alone misses: a run
 * going `running` -> `completed` leaves `stats.runs` untouched, and that is precisely
 * the update an operator watching a run is waiting for. Sorting is what makes the
 * value a function of the counts rather than of the server's key order.
 */
export function runsSignature(stats: RunTotals | null | undefined): string {
  const byStatus = Object.entries(stats?.byStatus ?? {}).sort(([a], [b]) =>
    a < b ? -1 : a > b ? 1 : 0,
  )
  return [stats?.runs ?? 0, ...byStatus.map(([k, v]) => `${k}=${v}`)].join('|')
}

/** "waiting_approval" -> "waiting approval"; 'all' -> 'All'. */
export function filterLabel(f: RunFilter): string {
  return f === 'all' ? 'All' : f.replace(/_/g, ' ')
}
