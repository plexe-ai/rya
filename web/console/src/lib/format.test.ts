import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ago, absolute, fmtUptime, num, stamp, statusClass, usd } from './format'

describe('ago', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-01T12:00:00Z'))
  })
  afterEach(() => vi.useRealTimers())

  it('renders an em dash for a missing timestamp', () => {
    expect(ago(undefined)).toBe('—')
    expect(ago(null)).toBe('—')
    expect(ago('')).toBe('—')
  })

  it('buckets by magnitude', () => {
    expect(ago('2026-08-01T11:59:30Z')).toBe('just now')
    expect(ago('2026-08-01T11:45:00Z')).toBe('15m ago')
    expect(ago('2026-08-01T09:00:00Z')).toBe('3h ago')
    expect(ago('2026-07-29T12:00:00Z')).toBe('3d ago')
  })

  it('clamps a future timestamp to "just now" rather than going negative', () => {
    expect(ago('2026-08-01T12:05:00Z')).toBe('just now')
  })

  it('passes an unparseable value straight through', () => {
    expect(ago('not a date')).toBe('not a date')
  })
})

describe('absolute / stamp', () => {
  it('makes an ISO timestamp readable', () => {
    expect(absolute('2026-08-01T12:00:00Z')).toBe('2026-08-01 12:00:00 UTC')
    expect(stamp('2026-08-01T12:00:00Z')).toBe('2026-08-01 12:00')
  })
  it('returns empty for nothing', () => {
    expect(absolute(null)).toBe('')
    expect(stamp(undefined)).toBe('')
  })
})

describe('fmtUptime', () => {
  it('drops units that are zero from the left', () => {
    expect(fmtUptime(0)).toBe('0s')
    expect(fmtUptime(45)).toBe('45s')
    expect(fmtUptime(125)).toBe('2m 5s')
    expect(fmtUptime(3725)).toBe('1h 2m')
  })
  it('treats undefined as zero', () => {
    expect(fmtUptime(undefined)).toBe('0s')
  })
})

describe('usd', () => {
  it('keeps four decimals below a dollar and two above', () => {
    expect(usd(0.0032)).toBe('$0.0032')
    expect(usd(12.5)).toBe('$12.50')
  })
  it('returns null when there is no cost to show, so callers can fall back', () => {
    expect(usd(null)).toBeNull()
    expect(usd(undefined)).toBeNull()
  })
  it('renders a real zero rather than treating it as absent', () => {
    expect(usd(0)).toBe('$0.0000')
  })
})

describe('num', () => {
  it('groups thousands and treats nullish as zero', () => {
    expect(num(1234567)).toBe((1234567).toLocaleString())
    expect(num(undefined)).toBe('0')
    expect(num(null)).toBe('0')
  })
})

describe('statusClass', () => {
  it('maps known statuses onto badge modifiers', () => {
    expect(statusClass('completed')).toBe('ok')
    expect(statusClass('waiting_approval')).toBe('wait')
    expect(statusClass('failed')).toBe('fail')
    expect(statusClass('rejected')).toBe('fail')
  })
  it('returns empty for an unknown or missing status instead of throwing', () => {
    expect(statusClass('something_new')).toBe('')
    expect(statusClass(undefined)).toBe('')
  })
})
