import { useCallback, useEffect, useRef, useState } from 'react'
import { isUnauthenticated } from './api'
import { useRefreshSignal } from './refresh'

/**
 * The ceiling on backoff. A runtime that has been down for ten minutes is not
 * going to be caught a second sooner by asking twice a minute instead of once,
 * and an operator who wants to know *now* has the Refresh button — which resets
 * the backoff, so the slow tail can never trap a console that is being watched.
 */
const MAX_BACKOFF_MS = 60_000

/** Doublings before the ceiling does the clamping. Keeps `2 ** n` finite. */
const MAX_DOUBLINGS = 6

export interface PollResult<T> {
  data: T | null
  error: string | null
  /** False only until the first settled response; a refresh does not flip it back. */
  loading: boolean
  /** True when the last attempt succeeded. Drives the "live / offline" pill. */
  live: boolean
  /**
   * True when the last failure was a 401 rather than an outage — ANY 401, including
   * one the stored credential does not explain. See `isUnauthenticated`.
   */
  unauthorized: boolean
  /**
   * Epoch ms of the last SUCCESSFUL response, or null if there has never been one.
   *
   * Audit §5.9: `live` alone cannot answer the only question that matters once a
   * poll starts failing — *how old is what I am looking at?* A boolean makes data
   * from four seconds ago and data from forty minutes ago indistinguishable, and
   * the console went on showing the second one under a 6px dot.
   */
  lastSuccessAt: number | null
  /**
   * Consecutive failures since the last success. Drives the backoff, and lets the
   * shell tell a blip (1) from an outage (many) without inferring it from a clock.
   */
  failures: number
  refresh: () => Promise<void>
}

/**
 * Poll a fetcher on an interval, keeping the last good value on failure.
 *
 * Keeping stale data visible is deliberate and matches the legacy console's
 * `showRuntimeDown` rule ("don't clobber a live view"): a transient blip should
 * not blank a dashboard an operator is reading. `live` goes false, and
 * `lastSuccessAt` says how stale, so the state stays honest about it.
 *
 * **The loop schedules itself; it is not a `setInterval`.** That is the whole of
 * the §5.8 fix, and it is structural rather than a stack of guards:
 *
 *  - The next tick is scheduled only once the previous one has SETTLED, so a
 *    backend slower than the interval cannot accumulate overlapping requests.
 *    `/console` opens a fresh psycopg connection per request in multi-tenant mode,
 *    and `setInterval(refresh, 6000)` handed a struggling database ten new
 *    connections a minute per open tab, forever, with every earlier answer thrown
 *    away on arrival.
 *  - Consecutive failures back the interval off exponentially to `MAX_BACKOFF_MS`,
 *    so a dead runtime is not hammered at full rate for as long as a tab is open.
 *  - A hidden tab does not fetch. A console left open on another desktop is not an
 *    operator watching it; `visibilitychange` fetches immediately on return, so the
 *    saving costs nothing in freshness.
 *  - Every attempt carries an `AbortSignal`. Superseded and unmounted requests are
 *    cancelled rather than merely ignored, which is what actually releases the
 *    connection at the other end.
 *
 * The fetcher may ignore the signal — `() => api('/x')` still type-checks, because
 * a shorter parameter list is assignable — but one that forwards it into `api()`
 * gets real cancellation.
 *
 * Also subscribes to the shell's refresh signal (`lib/refresh.ts`), so the Refresh
 * button reaches it without the caller wiring anything up.
 */
export function usePoll<T>(
  fetcher: (signal?: AbortSignal) => Promise<T>,
  intervalMs: number,
  opts: { enabled?: boolean; onError?: (e: Error) => void; tick?: number } = {},
): PollResult<T> {
  const { enabled = true, onError } = opts
  // The context is the normal path. `opts.tick` exists for the one caller that
  // PUBLISHES the signal — the shell — because `useContext` reads the nearest
  // provider ABOVE the component, and App renders its own. Without this it would
  // have to bump the tick *and* call `refresh()`, which is two `/console` requests
  // per press of a button whose neighbouring finding (§5.8) is about request volume.
  const ctxTick = useRefreshSignal()
  const tick = opts.tick ?? ctxTick

  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [live, setLive] = useState(false)
  const [unauthorized, setUnauthorized] = useState(false)
  const [lastSuccessAt, setLastSuccessAt] = useState<number | null>(null)
  const [failures, setFailures] = useState(0)

  // Held in refs so changing them never restarts the loop.
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher
  const onErrorRef = useRef(onError)
  onErrorRef.current = onError

  // Guards against a slow in-flight response landing after unmount, and against
  // an earlier request resolving after a later one (out-of-order overwrite).
  const mounted = useRef(true)
  const seq = useRef(0)
  // The controller for the request currently out, or null. It doubles as the
  // in-flight flag, so "is one out?" and "cancel it" cannot get out of step.
  const inFlight = useRef<AbortController | null>(null)
  // Mirrors `failures` for callbacks that closed over an older render.
  const failuresRef = useRef(0)
  /**
   * Doublings for the SCHEDULER, deliberately not the same number as `failures`.
   *
   * They diverge on one action, and it matters: an operator pressing Refresh should
   * make the console poll promptly again (reset the backoff) but must not make it
   * *claim the runtime is healthier than it is* (reset the failure count). Sharing
   * one counter meant a failed Retry dropped the count back to 1 and dismissed the
   * staleness banner the operator had just pressed Retry on — the console answering
   * "it's fine now" to a click that had proved the opposite.
   */
  const backoffRef = useRef(0)

  /**
   * One attempt. `force` distinguishes the two callers, and they want opposite
   * things when a request is already out: a scheduled tick SKIPS — that is the
   * in-flight guard — while an operator pressing Refresh SUPERSEDES. Dropping
   * their click because the console is mid-request would make the button look
   * broken in exactly the slow-backend case they pressed it for.
   */
  const attempt = useCallback(async (force: boolean) => {
    if (inFlight.current) {
      if (!force) return
      inFlight.current.abort()
    }
    const ctl = new AbortController()
    inFlight.current = ctl
    const mine = ++seq.current
    try {
      const d = await fetcherRef.current(ctl.signal)
      if (!mounted.current || mine !== seq.current) return
      setData(d)
      setError(null)
      setLive(true)
      setUnauthorized(false)
      setLastSuccessAt(Date.now())
      failuresRef.current = 0
      backoffRef.current = 0
      setFailures(0)
    } catch (e) {
      // We cancelled this one. That is not an outage, and counting it as one would
      // mean every Refresh over a slow request toasted "Lost connection".
      if (ctl.signal.aborted) return
      if (!mounted.current || mine !== seq.current) return
      const err = e instanceof Error ? e : new Error(String(e))
      setLive(false)
      setUnauthorized(isUnauthenticated(err))
      setError(err.message)
      failuresRef.current += 1
      backoffRef.current += 1
      setFailures(failuresRef.current)
      onErrorRef.current?.(err)
    } finally {
      if (inFlight.current === ctl) inFlight.current = null
      if (mounted.current && mine === seq.current) setLoading(false)
    }
  }, [])

  // Set by the scheduling effect: cancel the pending tick and re-time it from now.
  const restartRef = useRef<() => void>(() => {})

  /** An operator-initiated read: supersedes whatever is out, and clears the backoff. */
  const refresh = useCallback(async () => {
    backoffRef.current = 0
    await attempt(true)
    // Re-time the loop as well as resetting the counter. Without this a refresh
    // during a long backoff bought one fresh read and then went quiet again until
    // the *old* 60-second timer fired — the console would sit there looking like
    // the Refresh button had stopped working, which is the §5.6 complaint arriving
    // by a different route.
    restartRef.current()
  }, [attempt])

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      inFlight.current?.abort()
    }
  }, [])

  useEffect(() => {
    if (!enabled) return
    // A change of `tick` re-runs this effect, which is how the shell's Refresh
    // reaches the poll: one immediate attempt, with the timer's phase reset to match.
    backoffRef.current = 0

    let stopped = false
    let timer: number | undefined

    const schedule = () => {
      if (stopped) return
      const wait = Math.min(
        intervalMs * 2 ** Math.min(backoffRef.current, MAX_DOUBLINGS),
        MAX_BACKOFF_MS,
      )
      timer = window.setTimeout(run, wait)
    }
    const run = async () => {
      if (stopped) return
      // Skip the REQUEST while hidden, not the loop: keeping the timer alive means
      // there is no resume path to get wrong, and `onVisible` covers the latency.
      if (!document.hidden) await attempt(false)
      schedule()
    }
    const onVisible = () => {
      if (!document.hidden) void attempt(true)
    }

    restartRef.current = () => {
      window.clearTimeout(timer)
      schedule()
    }
    void attempt(true).then(schedule)
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      stopped = true
      restartRef.current = () => {}
      window.clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [enabled, intervalMs, attempt, tick])

  return { data, error, loading, live, unauthorized, lastSuccessAt, failures, refresh }
}

/**
 * A clock: re-renders the caller every `everyMs` for as long as `enabled`.
 *
 * The staleness readouts (§5.9) are the only things in the console that must change
 * with no new data — "4m old" has to become "5m old" while the runtime stays
 * silent, and the poll itself cannot drive that, because backoff means it may not
 * run again for a minute. Two readouts share ONE clock, passed down as `now`, so
 * the pill and the banner can never quote different ages for the same instant.
 *
 * Disabled by default in the only sense that matters: while everything is live,
 * `enabled` is false and there is no timer at all.
 */
export function useNow(everyMs: number, enabled: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!enabled) return
    setNow(Date.now())
    const id = window.setInterval(() => setNow(Date.now()), everyMs)
    return () => window.clearInterval(id)
  }, [everyMs, enabled])
  return now
}

/**
 * One-shot loader for views that load on entry rather than on the shell's poll
 * (guard, evals, quotas and the deploy views — see console/AGENTS.md: deployment
 * topology moves on a promote, not per second).
 *
 * "On entry" plus the shell's refresh signal, since §5.6: a view that only ever
 * loads once is a view the Refresh button cannot reach, and a Refresh that does
 * nothing on four of the deploy pages is worse than no button there at all.
 *
 * The sequence guard and the abort are not decoration. While a reload happened only
 * on an agent or id change, two overlapping loads were nearly unreachable; a button
 * an operator can press twice makes them ordinary, and an older response landing
 * last would put the previous agent's deployment under the current one's name.
 */
export function useLoad<T>(fetcher: (signal?: AbortSignal) => Promise<T>, deps: unknown[] = []) {
  const tick = useRefreshSignal()

  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const mounted = useRef(true)
  const seq = useRef(0)
  const inFlight = useRef<AbortController | null>(null)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      inFlight.current?.abort()
    }
  }, [])

  const reload = useCallback(async () => {
    inFlight.current?.abort()
    const ctl = new AbortController()
    inFlight.current = ctl
    const mine = ++seq.current
    setLoading(true)
    try {
      const d = await fetcherRef.current(ctl.signal)
      if (!mounted.current || mine !== seq.current) return
      setData(d)
      setError(null)
    } catch (e) {
      if (ctl.signal.aborted) return
      if (!mounted.current || mine !== seq.current) return
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (inFlight.current === ctl) inFlight.current = null
      if (mounted.current && mine === seq.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick])

  return { data, error, loading, reload }
}
