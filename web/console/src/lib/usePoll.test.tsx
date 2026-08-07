import type { ReactNode } from 'react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, renderHook, screen, fireEvent } from '@testing-library/react'
import { RefreshSignal } from './refresh'
import { useLoad, usePoll } from './usePoll'

/**
 * `usePoll` / `useLoad` — audit §5.6 and §5.8.
 *
 * These two hooks are the console's whole relationship with the network: every view
 * fetches through one of them. §5.8 is about what that relationship did to a backend
 * that was struggling — `setInterval(refresh, 6000)` with no in-flight guard, no
 * abort, no backoff and no visibility check, against a `/console` that opens a fresh
 * psycopg connection per request in multi-tenant mode. §5.6 is about what it did NOT
 * do: reach the nine views that own their own fetch, which is most of the console.
 *
 * Everything here drives the hook directly rather than through a view. The behaviour
 * under test is timing and cancellation; asserting it through rendered markup would
 * pin it to whichever view was chosen as the vehicle.
 */

const INTERVAL = 1000

/** A promise whose settlement this test controls. */
function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  // An unhandled rejection here is noise: the hook attaches its catch a tick later.
  promise.catch(() => {})
  return { promise, resolve, reject }
}

/** Let every pending microtask run, inside act, without moving the clock. */
const settle = () => act(async () => { await Promise.resolve() })

/** Move the fake clock, flushing microtasks as timers fire. */
const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms) })

beforeEach(() => {
  vi.useFakeTimers()
})
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('usePoll — the in-flight guard (§5.8)', () => {
  it('does not start a second request while one is still out', async () => {
    // The whole finding, at its simplest: a backend slower than the poll interval.
    const pending = deferred<string>()
    const fetcher = vi.fn(() => pending.promise)
    renderHook(() => usePoll(fetcher, INTERVAL))
    await settle()
    expect(fetcher).toHaveBeenCalledTimes(1)

    // Five intervals pass with the first request still unanswered. Under
    // `setInterval` this was five more requests — and five more connections.
    await advance(INTERVAL * 5)
    expect(fetcher).toHaveBeenCalledTimes(1)

    // It answers; the loop resumes from there rather than firing the backlog.
    await act(async () => {
      pending.resolve('ok')
    })
    expect(fetcher).toHaveBeenCalledTimes(1)
    await advance(INTERVAL)
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('schedules the next tick from the SETTLEMENT, not from the wall clock', async () => {
    // A 900ms backend on a 1000ms poll should be ~1 request per 1900ms, not 1 per
    // 1000ms with a permanent overlap. This is what makes the load self-limiting.
    let n = 0
    const fetcher = vi.fn(
      () =>
        new Promise<number>((res) => {
          setTimeout(() => res(++n), 900)
        }),
    )
    renderHook(() => usePoll(fetcher, INTERVAL))
    await advance(900) // first response
    expect(fetcher).toHaveBeenCalledTimes(1)
    await advance(999) // the interval has not elapsed since settlement
    expect(fetcher).toHaveBeenCalledTimes(1)
    await advance(2)
    expect(fetcher).toHaveBeenCalledTimes(2)
  })
})

describe('usePoll — backoff (§5.8)', () => {
  it('backs off exponentially while the runtime is down', async () => {
    const fetcher = vi.fn(() => Promise.reject(new Error('connection refused')))
    const { result } = renderHook(() => usePoll(fetcher, INTERVAL))
    await settle()
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(result.current.failures).toBe(1)

    // One failure -> wait 2x. At 1x nothing has happened yet.
    await advance(INTERVAL)
    expect(fetcher).toHaveBeenCalledTimes(1)
    await advance(INTERVAL)
    expect(fetcher).toHaveBeenCalledTimes(2)

    // Two failures -> 4x.
    await advance(INTERVAL * 3)
    expect(fetcher).toHaveBeenCalledTimes(2)
    await advance(INTERVAL)
    expect(fetcher).toHaveBeenCalledTimes(3)
    expect(result.current.failures).toBe(3)
  })

  it('returns to the full rate the moment a request succeeds', async () => {
    // Backoff that outlives the outage is its own bug: an operator watching a
    // runtime come back would wait a minute to be told so.
    let fail = true
    const fetcher = vi.fn(() => (fail ? Promise.reject(new Error('down')) : Promise.resolve('up')))
    const { result } = renderHook(() => usePoll(fetcher, INTERVAL))
    await settle()
    await advance(INTERVAL * 2) // 2nd attempt, fails
    await advance(INTERVAL * 4) // 3rd attempt, fails
    expect(fetcher).toHaveBeenCalledTimes(3)

    fail = false
    await advance(INTERVAL * 8)
    expect(fetcher).toHaveBeenCalledTimes(4)
    expect(result.current.failures).toBe(0)
    expect(result.current.live).toBe(true)

    await advance(INTERVAL)
    expect(fetcher).toHaveBeenCalledTimes(5)
  })

  it('an operator’s refresh clears the backoff immediately', async () => {
    const fetcher = vi.fn(() => Promise.reject(new Error('down')))
    const { result } = renderHook(() => usePoll(fetcher, INTERVAL))
    await settle()
    await advance(INTERVAL * 2)
    await advance(INTERVAL * 4)
    expect(fetcher).toHaveBeenCalledTimes(3) // now waiting 8x

    await act(async () => {
      await result.current.refresh()
    })
    expect(fetcher).toHaveBeenCalledTimes(4)
    // And the loop is back at 2x from one failure, not 16x.
    await advance(INTERVAL * 2)
    expect(fetcher).toHaveBeenCalledTimes(5)
  })
})

describe('usePoll — a hidden tab (§5.8)', () => {
  const define = (hidden: boolean) => {
    Object.defineProperty(document, 'hidden', { value: hidden, configurable: true })
    Object.defineProperty(document, 'visibilityState', {
      value: hidden ? 'hidden' : 'visible',
      configurable: true,
    })
  }
  const setHidden = (hidden: boolean) => {
    define(hidden)
    document.dispatchEvent(new Event('visibilitychange'))
  }
  // Restore the property WITHOUT firing the event: a dispatch here would land after
  // the test body and set state on a hook nobody is awaiting — an act warning that
  // says nothing about the code under test.
  afterEach(() => define(false))

  it('stops fetching while nobody is looking, and catches up on return', async () => {
    const fetcher = vi.fn(() => Promise.resolve('ok'))
    renderHook(() => usePoll(fetcher, INTERVAL))
    await settle()
    expect(fetcher).toHaveBeenCalledTimes(1)

    await act(async () => setHidden(true))
    await advance(INTERVAL * 10)
    // Ten ticks skipped. A console parked on another desktop is not an operator.
    expect(fetcher).toHaveBeenCalledTimes(1)

    // Immediately on return — not "within six seconds of return", which is how long
    // an operator would spend reading numbers that had not been checked in an hour.
    await act(async () => setHidden(false))
    await settle()
    expect(fetcher).toHaveBeenCalledTimes(2)
  })
})

describe('usePoll — cancellation (§5.8)', () => {
  it('hands the fetcher an abort signal and aborts it on unmount', async () => {
    const seen: AbortSignal[] = []
    const pending = deferred<string>()
    const { unmount } = renderHook(() =>
      usePoll((signal) => {
        if (signal) seen.push(signal)
        return pending.promise
      }, INTERVAL),
    )
    await settle()
    expect(seen).toHaveLength(1)
    expect(seen[0]!.aborted).toBe(false)

    unmount()
    // Ignoring a late response leaves the request running to completion at the other
    // end; aborting is what actually releases the connection.
    expect(seen[0]!.aborted).toBe(true)
  })

  it('a refresh supersedes the request in flight rather than queueing behind it', async () => {
    const pending = [deferred<string>(), deferred<string>()]
    let i = 0
    const seen: AbortSignal[] = []
    const { result } = renderHook(() =>
      usePoll((signal) => {
        if (signal) seen.push(signal)
        return pending[i++]!.promise
      }, INTERVAL),
    )
    await settle()

    // A scheduled tick would skip here. A click must not: the operator pressed
    // Refresh precisely because the request already out is taking too long.
    void result.current.refresh()
    await settle()
    expect(seen).toHaveLength(2)
    expect(seen[0]!.aborted).toBe(true)

    // And the superseded request's answer, arriving late, does not overwrite.
    await act(async () => {
      pending[1]!.resolve('second')
      pending[0]!.resolve('first')
    })
    expect(result.current.data).toBe('second')
  })

  it('does not report an aborted request as an outage', async () => {
    // An AbortError surfacing as "Lost connection to the runtime" would mean every
    // Refresh over a slow request accused a healthy backend of being down.
    const pending = deferred<string>()
    const onError = vi.fn()
    const { result } = renderHook(() =>
      usePoll(
        (signal) =>
          new Promise<string>((res, rej) => {
            signal?.addEventListener('abort', () => rej(new Error('AbortError')))
            void pending.promise.then(res)
          }),
        INTERVAL,
        { onError },
      ),
    )
    await settle()
    void result.current.refresh()
    await settle()
    expect(onError).not.toHaveBeenCalled()
    expect(result.current.error).toBeNull()
  })
})

describe('usePoll — staleness (§5.9)', () => {
  it('records when the data on screen was last true', async () => {
    let fail = false
    const fetcher = vi.fn(() => (fail ? Promise.reject(new Error('down')) : Promise.resolve('ok')))
    const { result } = renderHook(() => usePoll(fetcher, INTERVAL))
    await settle()
    const at = result.current.lastSuccessAt
    expect(at).toBeGreaterThan(0)

    fail = true
    await advance(INTERVAL)
    await advance(INTERVAL * 2)
    expect(result.current.live).toBe(false)
    expect(result.current.failures).toBe(2)
    // Unchanged: it timestamps the DATA, not the last attempt. A stamp that moved
    // on every failed poll would report freshly-failed as freshly-loaded.
    expect(result.current.lastSuccessAt).toBe(at)
    expect(result.current.data).toBe('ok')
  })

  it('is null before anything has ever succeeded', async () => {
    const { result } = renderHook(() =>
      usePoll(() => Promise.reject(new Error('down')), INTERVAL),
    )
    await settle()
    // Distinct from "old": there is no data, so there is no age. The shell paints
    // its runtime-down card for this, not a staleness banner.
    expect(result.current.lastSuccessAt).toBeNull()
    expect(result.current.data).toBeNull()
  })
})

/** A provider with a button that bumps the signal, standing in for the top bar. */
function WithRefresh({ children }: { children: ReactNode }) {
  const [tick, setTick] = useState(0)
  return (
    <RefreshSignal.Provider value={tick}>
      <button onClick={() => setTick((t) => t + 1)}>Refresh</button>
      {children}
    </RefreshSignal.Provider>
  )
}

describe('the refresh signal (§5.6)', () => {
  it('useLoad refetches when the shell broadcasts', async () => {
    // THE finding. `useLoad` is what Environments, Versions, Workers, Quotas, Evals,
    // Guard and Knowledge use, and before this the Refresh button did nothing at all
    // on any of them.
    const fetcher = vi.fn(() => Promise.resolve('data'))
    function Probe() {
      const { data } = useLoad(fetcher, [])
      return <div>{data}</div>
    }
    render(
      <WithRefresh>
        <Probe />
      </WithRefresh>,
    )
    await settle()
    expect(fetcher).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await settle()
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('usePoll refetches when the shell broadcasts, without doubling up', async () => {
    const fetcher = vi.fn(() => Promise.resolve('data'))
    function Probe() {
      usePoll(fetcher, INTERVAL)
      return null
    }
    render(
      <WithRefresh>
        <Probe />
      </WithRefresh>,
    )
    await settle()
    expect(fetcher).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await settle()
    // Exactly one. A broadcast that also left the old timer running would double the
    // request rate with every press.
    expect(fetcher).toHaveBeenCalledTimes(2)
    await advance(INTERVAL)
    expect(fetcher).toHaveBeenCalledTimes(3)
  })

  it('does not fire on mount beyond the load the view already does', async () => {
    const fetcher = vi.fn(() => Promise.resolve('data'))
    function Probe() {
      useLoad(fetcher, ['dep'])
      return null
    }
    render(
      <WithRefresh>
        <Probe />
      </WithRefresh>,
    )
    await settle()
    expect(fetcher).toHaveBeenCalledTimes(1)
  })
})

describe('useLoad — ordering (§5.6)', () => {
  it('ignores a response overtaken by a newer one', async () => {
    // Reachable only now that a button can start a second load. An older answer
    // landing last would show the previous agent's deployment under this one's name.
    const pending = [deferred<string>(), deferred<string>()]
    let i = 0
    function Probe() {
      const { data } = useLoad(() => pending[i++]!.promise, [])
      return <div>{data ?? 'none'}</div>
    }
    render(
      <WithRefresh>
        <Probe />
      </WithRefresh>,
    )
    await settle()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await settle()

    await act(async () => {
      pending[1]!.resolve('newer')
      pending[0]!.resolve('older')
    })
    expect(screen.getByText('newer')).toBeTruthy()
  })
})
