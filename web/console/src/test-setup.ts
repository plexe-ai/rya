import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach } from 'vitest'

// Unmount between tests so a leaked component can't hold focus or a timer and
// make the next test pass (or fail) for the wrong reason.
afterEach(cleanup)

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
