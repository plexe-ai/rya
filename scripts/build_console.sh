#!/usr/bin/env bash
# Build the React operator console into the Python package tree.
#
# Source:  web/console/                 (Vite + React + TypeScript)
# Output:  src/rya/console/dist/        (gitignored; force-included into the wheel)
#
# Node is a release-time dependency only. `pip install rya-server` needs nothing
# but Python, because this script has already run in CI and `dist/` is inside the
# wheel. See docs/PACKAGING.md.
set -euo pipefail

cd "$(dirname "$0")/.."
here=$PWD

if ! command -v npm >/dev/null 2>&1; then
  echo "error: npm not found. Node 20+ is required to build the console." >&2
  echo "       The wheel still builds without it; /v2 then serves a 503 explainer." >&2
  exit 1
fi

cd web/console

# `npm ci` is reproducible and is what CI should use; it needs a lockfile in sync
# with package.json. Fall back to `npm install` for a first run or after a bump.
if [ -f package-lock.json ]; then
  npm ci --no-fund --no-audit
else
  echo "note: no package-lock.json; using 'npm install' (commit the lockfile)" >&2
  npm install --no-fund --no-audit
fi

npm run test --silent
npm run build

cd "$here"
out=src/rya/console/dist
test -f "$out/index.html" || { echo "error: build produced no $out/index.html" >&2; exit 1; }
echo
echo "console built -> $out"
du -sh "$out"
