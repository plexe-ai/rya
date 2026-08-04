import type { Run } from './types'

export const RUN_FILTERS = ['all', 'completed', 'waiting_approval', 'failed'] as const
export type RunFilter = (typeof RUN_FILTERS)[number] | (string & {})

/**
 * Pure filter behind the Runs view, extracted so it is testable without a DOM.
 * In the legacy console this logic lived inline in `renderRunsTable` and was
 * reachable only by driving the real page.
 *
 * Matches the legacy semantics exactly: status is an equality match ('all' passes
 * everything) and the query is a case-insensitive substring of the run id or the
 * trigger.
 */
export function filterRuns(runs: Run[], status: RunFilter, query: string): Run[] {
  const q = query.trim().toLowerCase()
  return runs.filter((r) => {
    if (status !== 'all' && r.status !== status) return false
    if (!q) return true
    return (
      r.id.toLowerCase().includes(q) || String(r.trigger ?? '').toLowerCase().includes(q)
    )
  })
}

/** Per-status counts for the filter pills, plus `all`. */
export function runCounts(runs: Run[]): Record<string, number> {
  const counts: Record<string, number> = { all: runs.length }
  for (const r of runs) counts[r.status] = (counts[r.status] || 0) + 1
  return counts
}

/** "waiting_approval" -> "waiting approval"; 'all' -> 'All'. */
export function filterLabel(f: RunFilter): string {
  return f === 'all' ? 'All' : f.replace(/_/g, ' ')
}
