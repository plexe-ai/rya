import { cleanup, configure } from '@testing-library/react'
import { afterEach, beforeEach } from 'vitest'
import { resetRuntimeInfo } from './lib/api'

// Unmount between tests so a leaked component can't hold focus or a timer and
// make the next test pass (or fail) for the wrong reason.
afterEach(cleanup)

/**
 * `/v1/info` is fetched once per page load and cached in a module global
 * (`lib/api.ts: runtimeInfo`), which is exactly what the auth gate reads to decide
 * whether to open (§5.12). A module global outlives a test, so without this the second
 * test in a file would gate on the first test's stubbed runtime — the same class of
 * cross-test leak `cleanup` and `storage.clear()` exist to prevent, arriving through a
 * variable instead of through the DOM.
 */
beforeEach(resetRuntimeInfo)

/**
 * `waitFor`'s deadline, raised from Testing Library's 1000ms default.
 *
 * Nothing here waits on a real clock — every `fetch` is stubbed and resolves on the
 * next microtask — so a `waitFor` that needs more than a second is waiting on the CPU,
 * not on the code. Vitest runs the files in parallel workers; on a loaded machine, and
 * especially on a 2-core CI runner, a settle that takes ~250ms locally can miss 1000ms
 * and the suite goes red for a reason that has nothing to do with the change under
 * test. Cheap headroom against a failure mode that is pure noise.
 *
 * Kept below vitest's 15s `testTimeout` (see vite.config.ts) so a genuinely stuck
 * assertion still fails with Testing Library's DOM dump, which names what it was
 * looking for, rather than with a bare "test timed out".
 */
configure({ asyncUtilTimeout: 4000 })

/**
 * A real in-memory `localStorage`.
 *
 * Node 25 ships its own experimental `localStorage` global, which shadows the one
 * jsdom installs. Launched without `--localstorage-file` it is an inert object
 * whose `setItem` is undefined, so `lib/api.ts` would throw on any token access.
 * Rather than depend on which implementation wins in a given Node/jsdom pairing,
 * install a known-good one and reset it per test.
 */
class MemoryStorage implements Storage {
  #map = new Map<string, string>()

  get length() {
    return this.#map.size
  }
  clear() {
    this.#map.clear()
  }
  getItem(key: string) {
    return this.#map.has(key) ? this.#map.get(key)! : null
  }
  key(i: number) {
    return [...this.#map.keys()][i] ?? null
  }
  removeItem(key: string) {
    this.#map.delete(key)
  }
  setItem(key: string, value: string) {
    this.#map.set(key, String(value))
  }
}

const storage = new MemoryStorage()
for (const target of [globalThis, globalThis.window].filter(Boolean)) {
  Object.defineProperty(target, 'localStorage', {
    value: storage,
    configurable: true,
    writable: true,
  })
}

beforeEach(() => storage.clear())
