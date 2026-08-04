// Formatting helpers ported from the legacy SPA. These returned HTML strings
// there; here they return plain data and the components own the markup, which is
// what removes the escaping burden entirely (React escapes text children).

/** "3m ago" / "2h ago". Returns the raw value if it isn't a parseable date. */
export function ago(ts?: string | null): string {
  if (!ts) return '—'
  const t = Date.parse(ts)
  if (Number.isNaN(t)) return ts
  const s = Math.max(0, (Date.now() - t) / 1000)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

/** Absolute timestamp for the `title` tooltip beside a relative one. */
export function absolute(ts?: string | null): string {
  if (!ts) return ''
  return ts.replace('T', ' ').replace('Z', ' UTC')
}

/** "2026-08-01 14:03" — the compact form used in audit tables. */
export function stamp(ts?: string | null): string {
  return (ts || '').replace('T', ' ').slice(0, 16)
}

export function fmtUptime(seconds?: number): string {
  const s = seconds || 0
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = Math.floor(s % 60)
  if (h) return `${h}h ${m}m`
  if (m) return `${m}m ${sec}s`
  return `${sec}s`
}

export function num(n?: number | null): string {
  return (n ?? 0).toLocaleString()
}

/**
 * Cost is sub-cent for a single run and dollars in aggregate, so the precision
 * has to follow the magnitude or the interesting digits vanish.
 */
export function usd(v?: number | null): string | null {
  if (v == null) return null
  return `$${Number(v).toFixed(v < 1 ? 4 : 2)}`
}

/** Maps a run/job status onto the `.stbadge` modifier class. */
const STATUS_CLASS: Record<string, string> = {
  completed: 'ok',
  running: 'ok',
  waiting_approval: 'wait',
  pending: 'wait',
  failed: 'fail',
  rejected: 'fail',
  cancelled: 'fail',
}
export const statusClass = (s?: string): string => (s && STATUS_CLASS[s]) || ''

/** Short permission labels for the `.pb` pill (`allowed` -> `allowed`, etc). */
export const PERM_CLASS: Record<string, string> = {
  allowed: 'allowed',
  read_only: 'read',
  approval_required: 'appr',
  disabled: 'dis',
}
