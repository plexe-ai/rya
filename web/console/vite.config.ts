// `vitest/config` re-exports Vite's defineConfig widened with the `test` key, so
// one config file drives both the build and the test run.
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'

// The build output lands in the Python package tree, NOT here: `src/rya/console/dist`
// is what `pyproject.toml` force-includes into the wheel and what `api/app.py`
// mounts. This mirrors Airflow (`src/airflow/ui/dist`) and Chainlit
// (`chainlit/frontend/dist`) — frontend source stays out of the wheel, only
// `dist/` goes in. It is gitignored; maintainer CI builds it at release time so
// `pip install rya` never needs Node.
const OUT = fileURLToPath(new URL('../../src/rya/console/dist', import.meta.url))

export default defineConfig({
  plugins: [react()],
  // The console is served at the ROOT now — the legacy single-file SPA that held `/`
  // during the migration is deleted, so there is one console and one address. Assets
  // resolve to `/assets/*`, which `api/app.py` mounts; `/v2` 308-redirects here for
  // bookmarks. Change this and that mount together.
  base: '/',
  build: {
    outDir: OUT,
    emptyOutDir: true,
    // Off deliberately: this output ships inside the Python wheel, and the map for
    // this bundle is ~1MB — four times the bundle itself. `npm run dev` serves
    // full sourcemaps, so the debugging story is already covered where it matters.
    sourcemap: false,
  },
  server: {
    port: 5273,
    // `npm run dev` talks to a local `rya serve`. Same paths as production, so
    // no environment-specific API base is needed in app code.
    proxy: Object.fromEntries(
      ['/console', '/runs', '/approvals', '/queue', '/gate', '/v1', '/agents', '/versions', '/workers', '/quotas', '/usage', '/evals', '/events']
        .map((p) => [p, { target: 'http://127.0.0.1:8000', changeOrigin: true }]),
    ),
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.test.{ts,tsx}'],
    setupFiles: ['src/test-setup.ts'],
  },
})
