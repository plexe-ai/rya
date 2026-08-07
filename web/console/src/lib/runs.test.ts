import { describe, expect, it } from 'vitest'
import { filterLabel, pillCounts, runsSignature } from './runs'
import type { Stats } from './types'

/**
 * `stats` as `/console` sends it for a workspace with 412 runs — the shape audit
 * §5.1 is about. Only `runs` and `byStatus` matter here; the rest is present so the
 * assertion is made against the real aggregate type rather than a convenient
 * subset of it.
 */
const STATS: Stats = {
  runs: 412,
  byStatus: { completed: 380, failed: 12, waiting_approval: 15, running: 5 },
  approvalsPending: 15,
  inputTokens: 900_000,
  outputTokens: 210_000,
  jobsPending: 0,
}

describe('pillCounts', () => {
  it('counts every run, not the page the console happens to hold', () => {
    // The numbers a `runCounts(state.runs)` over the 30-row preview could never
    // produce: `/console` caps `runs` at 30, so 412 and 380 are unreachable from it.
    expect(pillCounts(STATS)).toEqual({
      all: 412,
      completed: 380,
      failed: 12,
      waiting_approval: 15,
      running: 5,
    })
  })

  it("takes `all` from stats.runs, so it matches the Overview tile exactly", () => {
    // The tile renders `num(st.runs)` (`Overview.tsx`). Summing `byStatus` instead
    // would drift the moment the server counts a status this console does not know
    // about — and a pill disagreeing with a tile one screen up is the visible half
    // of §5.1.
    expect(pillCounts({ runs: 412, byStatus: { completed: 400 } }).all).toBe(412)
  })

  it('falls back to the sum of the parts when the aggregate omits the total', () => {
    // Reading 0 under `All` beside populated per-status pills would be worse than
    // approximating: it would look like a workspace with no runs.
    expect(pillCounts({ byStatus: { completed: 3, failed: 1 } }).all).toBe(4)
  })

  it('does not let a byStatus key named all displace the total', () => {
    expect(pillCounts({ runs: 412, byStatus: { all: 30, failed: 12 } })).toEqual({
      all: 412,
      failed: 12,
    })
  })

  it('reports a fresh install as zero rather than throwing', () => {
    // The fresh-install arm of the empty state switches on `all === 0`, so this has
    // to be a number for a workspace that has never run anything, and for the
    // moment before the first poll has landed at all.
    expect(pillCounts({ runs: 0, byStatus: {} })).toEqual({ all: 0 })
    expect(pillCounts(undefined).all).toBe(0)
    expect(pillCounts(null).all).toBe(0)
  })
})

describe('runsSignature', () => {
  it('moves when a run is added', () => {
    const before = runsSignature({ runs: 412, byStatus: { completed: 380 } })
    const after = runsSignature({ runs: 413, byStatus: { completed: 381 } })
    expect(after).not.toBe(before)
  })

  it('moves when a run only CHANGES status, leaving the total alone', () => {
    // The reason `byStatus` is in the signature at all: `running` -> `completed` is
    // the update an operator watching a run is waiting for, and the total does not
    // notice it. A signature over `runs` alone would leave the table frozen on the
    // stale status until something else happened.
    const before = runsSignature({ runs: 412, byStatus: { running: 1, completed: 411 } })
    const after = runsSignature({ runs: 412, byStatus: { running: 0, completed: 412 } })
    expect(after).not.toBe(before)
  })

  it('is stable across the server reordering the byStatus keys', () => {
    // Object key order is insertion order, and nothing promises the server builds
    // that dict the same way twice. An unstable signature would refetch the whole
    // window on every poll — quietly turning the 6s dashboard poll into a 6s query
    // over the run table, which is the load §5.8 warns about.
    const a = runsSignature({ runs: 3, byStatus: { failed: 1, completed: 2 } })
    const b = runsSignature({ runs: 3, byStatus: { completed: 2, failed: 1 } })
    expect(a).toBe(b)
  })

  it('is stable when nothing moved', () => {
    expect(runsSignature(STATS)).toBe(runsSignature({ ...STATS }))
  })

  it('answers for an absent or empty stats block', () => {
    expect(runsSignature(undefined)).toBe(runsSignature(null))
    expect(runsSignature({})).toBe(runsSignature({ runs: 0, byStatus: {} }))
  })
})

describe('filterLabel', () => {
  it('capitalises all and de-snakes the rest', () => {
    expect(filterLabel('all')).toBe('All')
    expect(filterLabel('waiting_approval')).toBe('waiting approval')
    expect(filterLabel('failed')).toBe('failed')
  })
})
