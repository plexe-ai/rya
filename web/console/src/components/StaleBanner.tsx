import { AlertTriangle } from 'lucide-react'
import { since } from '../lib/format'

/**
 * Consecutive failed polls before the console says so in a banner.
 *
 * Two, not one. A single missed tick is a blip — a laptop lid, a redeploy, a
 * proxy hiccup — and a banner on every blip is a banner operators learn to ignore,
 * which would cost more than it buys. Two failures at the 6s poll is ~12 seconds
 * of silence, which is no longer a blip. The `live` pill goes offline on the first
 * failure regardless, so nothing is hidden in the meantime.
 */
export const STALE_AFTER_FAILURES = 2

/**
 * "What you are looking at is old, and here is how old."
 *
 * Audit §5.9: once one poll had succeeded, a runtime that then died forever was
 * masked indefinitely. `usePoll` keeps the last good value on purpose — blanking a
 * dashboard over a blip is worse — but the ONLY notice of the failure was a 2.6s
 * toast fired on the leading edge and never again. An operator who was fetching
 * coffee at the wrong moment, or who opened the tab after the runtime died, saw a
 * complete dashboard of numbers with nothing to suggest they were frozen. Every
 * decision made from that screen is made from data of unknown age.
 *
 * So: unmissable, permanent while it lasts, and specific about the age. It sits
 * OUTSIDE the view's error boundary and above every view, because staleness is a
 * property of the shell's poll rather than of whatever page happens to be open.
 *
 * `now` is passed in rather than read here so this and the top bar's pill quote the
 * same instant — see `useNow`.
 */
export function StaleBanner({
  lastSuccessAt,
  now,
  message,
  onRetry,
}: {
  lastSuccessAt: number
  now: number
  message: string | null
  onRetry: () => void
}) {
  return (
    <div className="anote row" role="alert">
      <AlertTriangle aria-hidden="true" focusable="false" />
      <span>
        Showing data from <b>{since(lastSuccessAt, now)} ago</b> — the runtime has not answered
        since{message ? `: ${message}` : '.'}
      </span>
      <button className="btn sm" onClick={onRetry}>
        Retry
      </button>
    </div>
  )
}
