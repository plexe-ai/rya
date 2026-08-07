import { useEffect, useId, useRef } from 'react'
import type { ReactNode } from 'react'
import { TriangleAlert } from 'lucide-react'

/**
 * A confirmation step for an action the console cannot take back.
 *
 * It exists because of §5.15: the tool kill switch wrote privileged, append-only
 * policy state on a single unguarded click, and a mis-click on the wrong row of a
 * polling table refused every call to a live tool with no way to say so afterwards.
 * The general shape is here rather than inlined into `Tools.tsx` because that is not
 * the only destructive control in this console — "Send test event" spends real money
 * against a live agent (§5.21), revoking a member and deleting an API key are both one
 * click — and a confirmation each of those grows for itself is how four dialogs end up
 * with four different a11y stories.
 *
 * Not `window.confirm`. That is a modal the operator cannot style, cannot extend with
 * the fields an audit trail needs, and — in a console the browser may have decided is
 * spamming dialogs — one it can suppress entirely, which would turn a guarded action
 * back into an unguarded one without any visible change.
 *
 * The a11y contract is `AuthModal`'s, deliberately identical so there is one dialog
 * behaviour in this tree: `role="dialog" aria-modal="true"`, labelled by its own
 * heading, focus moved inside on open, Tab trapped within the card, Escape closes.
 * **Escape and Cancel are the same event and neither performs the action** — the point
 * of the dialog is that only pressing the confirm button does anything at all.
 *
 * `children` is for the fields the decision needs (a tier to pick, a reason to type).
 * The caller owns them and their validation, and blocks the confirm button with
 * `confirmDisabled` — this component has no opinion about what makes a decision
 * complete, only about not letting it happen by accident.
 */
export function ConfirmDialog({
  title,
  body,
  confirmLabel,
  cancelLabel = 'Cancel',
  danger = false,
  busy = false,
  confirmDisabled = false,
  onConfirm,
  onCancel,
  children,
}: {
  title: string
  /** One or two sentences on what confirming actually does. Text, never markup. */
  body?: ReactNode
  confirmLabel: string
  cancelLabel?: string
  /** Red confirm button + the warning mark. For destructive, not merely consequential. */
  danger?: boolean
  /** The action is in flight: the confirm button locks, Cancel deliberately does not. */
  busy?: boolean
  confirmDisabled?: boolean
  onConfirm: () => void
  onCancel: () => void
  children?: ReactNode
}) {
  const cardRef = useRef<HTMLDivElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)

  // `useId` rather than a fixed string: two of these could be mounted at once (a
  // dialog raised from a view that is itself inside one), and a duplicated
  // `aria-labelledby` target silently names the wrong dialog to a screen reader.
  const uid = useId()
  const titleId = `cd-title-${uid}`
  const bodyId = `cd-body-${uid}`

  // Focus goes to the first field the operator has to fill in, or to Cancel when
  // there is none. Never to the confirm button: the click that opened this dialog is
  // frequently followed by a stray Enter or Space, and landing focus on the
  // destructive control would turn the confirmation into a keystroke of latency.
  useEffect(() => {
    const card = cardRef.current
    if (!card) return
    const field = card.querySelector<HTMLElement>('input,select,textarea')
    ;(field ?? cancelRef.current)?.focus()
  }, [])

  // Escape closes; Tab is trapped inside the dialog. Same handler AuthModal installs.
  // Disabled controls are filtered out of the ring because the confirm button starts
  // disabled whenever the caller demands a field first — treating it as the last stop
  // would send Tab to an element the browser skips, and the trap would leak.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        onCancel()
        return
      }
      if (e.key !== 'Tab' || !cardRef.current) return
      const f = [...cardRef.current.querySelectorAll<HTMLElement>('input,select,textarea,button')].filter(
        (el) => !el.hasAttribute('disabled'),
      )
      if (!f.length) return
      const first = f[0]!
      const last = f[f.length - 1]!
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onCancel])

  return (
    // `.authwrap` is `display:none` in the stylesheet and the auth modal overrides it
    // inline; matching that keeps this to zero edits of an existing rule.
    <div className="authwrap" style={{ display: 'grid' }}>
      <div
        className="authcard"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={body != null ? bodyId : undefined}
        ref={cardRef}
      >
        {danger && (
          <div className="al">
            <TriangleAlert aria-hidden="true" focusable="false" />
          </div>
        )}
        <h3 id={titleId}>{title}</h3>
        {body != null && <p id={bodyId}>{body}</p>}
        {children != null && <div className="apanel">{children}</div>}
        <div className="dlgrow">
          {/* Cancel stays live while `busy`. A request that hangs must not also trap
              the operator in a dialog they can no longer dismiss; the action they
              started still reports itself through the caller's toast. */}
          <button type="button" className="btn" ref={cancelRef} onClick={onCancel}>
            {cancelLabel}
          </button>
          <button
            type="button"
            className={`btn ${danger ? 'danger' : 'dark'}`}
            onClick={onConfirm}
            disabled={busy || confirmDisabled}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
