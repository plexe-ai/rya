import { createContext, useContext } from 'react'

/**
 * The shell's "refresh everything now" signal — a monotonic counter, broadcast.
 *
 * Audit §5.6: the Refresh button in the top bar refetched `/console` and nothing
 * else, so on the nine views that own a fetch — Environments, Versions, Workers,
 * Quotas, Evals, Guard, Knowledge, and since §5.1/§5.2 Runs and Conversations — the
 * most visible control in the console changed nothing at all. The fix is not nine
 * `reload` props threaded up to the shell: that is the version of this bug that
 * comes back the next time somebody adds a view and forgets one.
 *
 * Instead the counter lives in context and `usePoll`/`useLoad` **subscribe to it
 * themselves**. Every loader in the mounted tree therefore honours Refresh by
 * construction, and a view acquires the behaviour by using the hooks it was already
 * using. Nothing in `views/` mentions this file, which is the point.
 *
 * Bumped by the shell for exactly three things, all of them "what is on screen may
 * no longer be true": the Refresh button, a successful sign-in, and sending a test
 * event. It is deliberately NOT bumped by the 6s poll — a signal that fired every
 * six seconds would turn every `useLoad` on the page into a second poller, which is
 * the mistake §5.5 just finished undoing.
 */
export const RefreshSignal = createContext(0)

/**
 * Subscribe to the shell's refresh counter. The value is meaningless on its own; it
 * is used as a `useEffect` dependency, so a change re-runs the fetch.
 */
export const useRefreshSignal = (): number => useContext(RefreshSignal)
