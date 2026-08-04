import { useCallback, useEffect, useRef, useState } from 'react'
import { UnauthorizedError } from './api'

export interface PollResult<T> {
  data: T | null
  error: string | null
  /** False only until the first settled response; a refresh does not flip it back. */
  loading: boolean
  /** True when the last attempt succeeded. Drives the "live / offline" pill. */
  live: boolean
  /** True when the last failure was a 401 rather than an outage. */
  unauthorized: boolean
  refresh: () => Promise<void>
}

/**
 * Poll a fetcher on an interval, keeping the last good value on failure.
 *
 * Keeping stale data visible is deliberate and matches the legacy console's
 * `showRuntimeDown` rule ("don't clobber a live view"): a transient blip should
 * not blank a dashboard an operator is reading. `live` goes false so the state is
 * still honest about being stale.
 */
export function usePoll<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
  opts: { enabled?: boolean; onError?: (e: Error) => void } = {},
): PollResult<T> {
  const { enabled = true, onError } = opts

  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [live, setLive] = useState(false)
  const [unauthorized, setUnauthorized] = useState(false)

  // Held in refs so changing them never restarts the interval.
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher
  const onErrorRef = useRef(onError)
  onErrorRef.current = onError

  // Guards against a slow in-flight response landing after unmount, and against
  // an earlier request resolving after a later one (out-of-order overwrite).
  const mounted = useRef(true)
  const seq = useRef(0)

  const refresh = useCallback(async () => {
    const mine = ++seq.current
    try {
      const d = await fetcherRef.current()
      if (!mounted.current || mine !== seq.current) return
      setData(d)
      setError(null)
      setLive(true)
      setUnauthorized(false)
    } catch (e) {
      if (!mounted.current || mine !== seq.current) return
      const err = e instanceof Error ? e : new Error(String(e))
      setLive(false)
      setUnauthorized(err instanceof UnauthorizedError)
      setError(err.message)
      onErrorRef.current?.(err)
    } finally {
      if (mounted.current && mine === seq.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  useEffect(() => {
    if (!enabled) return
    void refresh()
    const id = setInterval(() => void refresh(), intervalMs)
    return () => clearInterval(id)
  }, [enabled, intervalMs, refresh])

  return { data, error, loading, live, unauthorized, refresh }
}

/**
 * One-shot loader for views that load on entry rather than on the shell's poll
 * (guard, evals, queue, and the deploy views — see console/AGENTS.md: deployment
 * topology moves on a promote, not per second).
 */
export function useLoad<T>(fetcher: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      const d = await fetcherRef.current()
      setData(d)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return { data, error, loading, reload }
}
