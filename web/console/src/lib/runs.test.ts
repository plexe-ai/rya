import { describe, expect, it } from 'vitest'
import { filterLabel, filterRuns, runCounts } from './runs'
import type { Run } from './types'

const run = (over: Partial<Run> & { id: string }): Run => ({
  status: 'completed',
  trigger: 'message.received',
  ...over,
})

const RUNS: Run[] = [
  run({ id: 'run_aaa1', status: 'completed', trigger: 'message.received' }),
  run({ id: 'run_bbb2', status: 'failed', trigger: 'cron.nightly' }),
  run({ id: 'run_ccc3', status: 'waiting_approval', trigger: 'webhook' }),
  run({ id: 'run_ddd4', status: 'completed', trigger: 'CRON.weekly' }),
]

describe('filterRuns', () => {
  it("'all' with an empty query passes everything through", () => {
    expect(filterRuns(RUNS, 'all', '')).toHaveLength(4)
  })

  it('filters by exact status', () => {
    expect(filterRuns(RUNS, 'completed', '').map((r) => r.id)).toEqual(['run_aaa1', 'run_ddd4'])
    expect(filterRuns(RUNS, 'waiting_approval', '').map((r) => r.id)).toEqual(['run_ccc3'])
  })

  it('matches a query against the run id', () => {
    expect(filterRuns(RUNS, 'all', 'bbb').map((r) => r.id)).toEqual(['run_bbb2'])
  })

  it('matches a query against the trigger, case-insensitively', () => {
    // 'cron.nightly' and 'CRON.weekly' differ in case; both must match.
    expect(filterRuns(RUNS, 'all', 'cron').map((r) => r.id)).toEqual(['run_bbb2', 'run_ddd4'])
  })

  it('applies status AND query together', () => {
    expect(filterRuns(RUNS, 'completed', 'cron').map((r) => r.id)).toEqual(['run_ddd4'])
  })

  it('ignores surrounding whitespace in the query', () => {
    expect(filterRuns(RUNS, 'all', '  bbb  ').map((r) => r.id)).toEqual(['run_bbb2'])
  })

  it('returns nothing when a query matches nothing', () => {
    expect(filterRuns(RUNS, 'all', 'nope')).toEqual([])
  })

  it('tolerates a missing trigger without throwing', () => {
    const runs = [run({ id: 'run_x', trigger: undefined as unknown as string })]
    expect(filterRuns(runs, 'all', 'anything')).toEqual([])
    expect(filterRuns(runs, 'all', 'run_x')).toHaveLength(1)
  })
})

describe('runCounts', () => {
  it('counts per status and totals under all', () => {
    expect(runCounts(RUNS)).toEqual({
      all: 4,
      completed: 2,
      failed: 1,
      waiting_approval: 1,
    })
  })

  it('reports zero total for an empty list', () => {
    expect(runCounts([])).toEqual({ all: 0 })
  })
})

describe('filterLabel', () => {
  it('capitalises all and de-snakes the rest', () => {
    expect(filterLabel('all')).toBe('All')
    expect(filterLabel('waiting_approval')).toBe('waiting approval')
    expect(filterLabel('failed')).toBe('failed')
  })
})
