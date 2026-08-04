import type { ReactNode } from 'react'
import { Inbox } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { ago, absolute, statusClass } from '../lib/format'

// The shared primitives, matching the legacy console's `tile`/`table`/`empty`/
// `spec` helpers class-for-class so `styles.css` needs no changes. The difference
// is that these take data and return elements, so nothing is string-concatenated
// and nothing needs `esc()`.

export function Empty({ children, icon: Icon = Inbox }: { children: ReactNode; icon?: LucideIcon }) {
  return (
    <div className="empty">
      <Icon aria-hidden="true" focusable="false" />
      {children}
    </div>
  )
}

export function Tile({
  icon: Icon,
  label,
  value,
  sub,
  amber,
}: {
  icon: LucideIcon
  label: string
  value: ReactNode
  sub?: ReactNode
  amber?: boolean
}) {
  return (
    <div className="stat">
      <div className="k">
        <Icon aria-hidden="true" focusable="false" />
        {label}
      </div>
      <div className={`v${amber ? ' amber' : ''}`}>{value}</div>
      {sub != null && <div className="vs">{sub}</div>}
    </div>
  )
}

export function SecRow({ left, right }: { left: ReactNode; right?: ReactNode }) {
  return (
    <div className="secrow">
      <span className="l">{left}</span>
      {right != null && <span className="r">{right}</span>}
    </div>
  )
}

export function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`stbadge ${statusClass(status)}`.trim()}>
      <span className="d" />
      {status}
    </span>
  )
}

/** Relative time with the absolute value in a tooltip. */
export function Ago({ ts }: { ts?: string | null }) {
  if (!ts) return <>—</>
  return <span title={absolute(ts)}>{ago(ts)}</span>
}

export function Mono({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={`mono${className ? ' ' + className : ''}`}>{children}</span>
}

/**
 * Click-to-copy identifier. `stopPropagation` keeps a copy from also opening the
 * row's detail view, which is the behaviour the legacy `cpid()` had.
 */
export function CopyId({ id, onCopied }: { id: string; onCopied?: (msg: string) => void }) {
  return (
    <span
      className="mono"
      title="Click to copy"
      style={{ cursor: 'copy' }}
      onClick={(e) => {
        e.stopPropagation()
        void navigator.clipboard?.writeText(id).then(() => onCopied?.(`Copied ${id}`))
      }}
    >
      {id}
    </span>
  )
}

export interface Column<T> {
  header: ReactNode
  /** Cell renderer. Returning a string is fine — React escapes it. */
  cell: (row: T) => ReactNode
  className?: string
}

/**
 * Table with an empty state. `rowKey` is required rather than falling back to the
 * array index: a keyed row is what lets React preserve DOM (and focus, and
 * selection) across the 6s poll instead of rebuilding the tbody. That is the
 * specific bug class `console/AGENTS.md` warns about for the legacy renderer.
 */
export function Table<T>({
  columns,
  rows,
  rowKey,
  onRowClick,
  rowLabel,
  emptyMessage = 'Nothing here yet.',
  emptyIcon,
}: {
  columns: Column<T>[]
  rows: T[]
  rowKey: (row: T) => string
  onRowClick?: (row: T) => void
  rowLabel?: (row: T) => string
  emptyMessage?: ReactNode
  emptyIcon?: LucideIcon
}) {
  if (!rows.length) return <Empty icon={emptyIcon}>{emptyMessage}</Empty>
  return (
    <table className="tbl">
      <thead>
        <tr>
          {columns.map((c, i) => (
            <th key={i}>{c.header}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr
            key={rowKey(row)}
            className={onRowClick ? 'clickable' : undefined}
            tabIndex={onRowClick ? 0 : undefined}
            role={onRowClick ? 'button' : undefined}
            aria-label={onRowClick && rowLabel ? rowLabel(row) : undefined}
            onClick={onRowClick ? () => onRowClick(row) : undefined}
            // Rows are not real buttons, so Enter/Space have to be wired up by
            // hand to keep them keyboard-reachable.
            onKeyDown={
              onRowClick
                ? (e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      onRowClick(row)
                    }
                  }
                : undefined
            }
          >
            {columns.map((c, i) => (
              <td key={i} className={c.className}>
                {c.cell(row)}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export function Toast({ message }: { message: string | null }) {
  return (
    <div className={`toast${message ? ' show' : ''}`} role="status" aria-live="polite">
      {message}
    </div>
  )
}

export function Window({ name, children }: { name: string; children: ReactNode }) {
  return (
    <div className="window">
      <div className="win-bar">
        <div className="win-dots">
          <i />
          <i />
          <i />
        </div>
        <span className="win-name">{name}</span>
      </div>
      <pre>{children}</pre>
    </div>
  )
}

export function ViewHeader({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <>
      <div className="h2">{title}</div>
      {children != null && <div className="sub">{children}</div>}
    </>
  )
}
