# Rya runtime image — the single-worker server (control plane + data plane).
# OSS self-host: `docker compose up` (see docker-compose.yml).

# ---------------------------------------------------------------------------
# Stage 1 — the operator console.
#
# `web/console/` (React + Vite) compiles to `src/rya/console/dist/`, which is
# gitignored and force-included into the wheel via `artifacts` in pyproject.toml —
# the Airflow pattern, see docs/PACKAGING.md. Node is a BUILD-time dependency only:
# nothing below this stage has it, and `pip install rya-server` never needs it.
#
# Before this stage existed the image had no console at all. A clean clone produced
# a container serving the 503 explainer at `/` — the designed-legal absent-bundle
# state, which is exactly why nobody noticed — and a machine where a developer had
# run `npm run build` baked that stale working-tree bundle in instead, so the same
# Dockerfile gave a different image per machine. `.dockerignore` now keeps the host's
# `dist/` out of the context entirely; this stage is the only way a bundle gets in.
#
# WORKDIR mirrors the repo layout deliberately: vite.config.ts writes to
# `../../src/rya/console/dist` relative to `web/console`, so the output lands at
# /build/src/rya/console/dist and the COPY in stage 2 uses the same path the wheel
# does. Moving either one without the other silently produces an empty console.
#
# Debian-based rather than Alpine: rollup and esbuild resolve platform-specific
# optional dependencies, and glibc is what CI's runner and the lockfile's default
# resolution assume. A discarded builder stage's size is not worth the risk.
# ---------------------------------------------------------------------------
FROM node:20-slim AS console
WORKDIR /build/web/console

# Manifests first so an edit to a .tsx does not invalidate the dependency layer.
# `npm ci` and not `npm install`: it installs the lockfile exactly and fails if
# package.json has drifted from it, which is what makes the bundle reproducible.
COPY web/console/package.json web/console/package-lock.json ./
RUN npm ci --no-fund --no-audit

COPY web/console/ ./
# `npm run build` is `tsc --noEmit && vite build`, so a type error fails the image
# build instead of shipping a console that happened to compile. The `test -f` catches
# the other direction — a build that "succeeded" while writing somewhere else.
RUN npm run build && test -f /build/src/rya/console/dist/index.html

# ---------------------------------------------------------------------------
# Stage 2 — the runtime.
# ---------------------------------------------------------------------------
FROM python:3.12-slim

WORKDIR /app
# LICENSE is not decoration here: pyproject.toml declares `license-files = ["LICENSE"]`
# and `readme = "README.md"`, and hatchling reads both during the install below.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

# The compiled console, from stage 1. It has to land BEFORE `pip install` so that
# hatchling's `artifacts = ["src/rya/console/dist/**"]` sees it and copies it into
# the installed package; afterwards would leave it in /app, which nothing serves.
COPY --from=console /build/src/rya/console/dist ./src/rya/console/dist

# `s3` (boto3) is not optional in practice for a multi-process deployment: the api
# and the workers are separate containers, so bundle archives have to live in a
# shared object store. Without it `rya publish` fails with E_BUNDLE_STORE.
RUN pip install --no-cache-dir '.[api,postgres,llm,mcp,s3]'

# Assert the console reached the INSTALLED package, not just the build context.
# A missing bundle is a legal runtime state that answers 503 rather than crashing,
# which makes it invisible until an operator opens the console — so this image
# refuses to build rather than ship that.
RUN python -c "import pathlib, rya, sys; \
p = pathlib.Path(rya.__file__).parent / 'console' / 'dist' / 'index.html'; \
sys.exit(0 if p.is_file() else f'console missing from the installed package: {p}')"

# The agent project (rya.agent.yaml + src/agent.py) is mounted at /project.
WORKDIR /project
EXPOSE 8787

# Serves the control-plane API + webhook trigger for the mounted agent.
CMD ["rya", "serve", "--host", "0.0.0.0", "--port", "8787"]
