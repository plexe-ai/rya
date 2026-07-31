# Packaging — `rya` and `rya-server`

Implements **PLATFORM_DESIGN D16** and §11 item 11: "`rya` on PyPI stays the
client SDK; the platform ships as `rya-server`."

## The two distributions are alternatives, not halves

`rya` and `rya-server` are **mutually exclusive**. They both install into the
same `rya` import namespace, and both own `rya/__init__.py`. The relationship is
`opencv-python` vs `opencv-python-headless`, not `foo` vs `foo-extras`:

- a **client repo** installs `rya` — four dependencies, no server, no database
  driver, no model SDK;
- a **platform image** installs `rya-server` — the whole tree, `rya` modules
  included, because the platform imports `manifest/`, `sdk/agent.py` and
  `bundles.py` too (§14's BOTH rows);
- nothing installs both. Pip will not stop you; whichever landed second wins for
  every file the two share, and you get a half-platform.

## Nothing moved

D16 is achieved by **packaging plus enforced import discipline**, not by
relocating code. The import package stays `rya`, every module stays at its
current path, and no import in the tree was rewritten. D16 itself is the
argument: "Operators deploy an image, so the platform's PyPI name is
near-cosmetic" — a mass relocation would be a large mechanical risk for a
near-cosmetic gain.

What makes the split real is `packaging/surface.py`, which declares in code
exactly which modules constitute the SDK, and `tests/test_sdk_surface.py`, which
walks the actual import graph and fails if any of them reaches platform code.
§11 item 13 sets the bar — "a second client repo built by someone who has never
opened the `rya` codebase. If that is not possible, the boundary leaked" — and
that test is what detects the leak.

## Layout

```
pyproject.toml            name = "rya", the WHOLE tree.
                          The editable dev install and nothing else — §2's dev
                          environment is the platform. Never published.
packaging/
  surface.py              the SDK module set + the enumerated exceptions.
                          Single source of truth; the test enforces it.
  sdk/
    pyproject.toml        name = "rya"          — the thin client SDK
    README.md             the SDK's OWN long description (see below)
    py.typed              PEP 561, SDK wheel only
    stubs/rya/sdk/context.pyi   the `ctx` type stubs (§14)
  server/
    pyproject.toml        name = "rya-server"   — the platform
    README.md             symlink to the root README — the platform's docs ARE
                          the repo's docs, so it should track them
```

The SDK ships its **own** README rather than a symlink to the repo root. The root
README sells the platform — `rya serve`, Postgres, the console, self-hosting — and
a client developer installing `rya` gets none of that. A PyPI page advertising
features the package cannot provide is a support burden, not marketing, so the
thin SDK's page describes exactly the thin SDK and points at `rya-server` for the
rest. `packaging/server/README.md` stays a symlink for the mirror-image reason.

Both packaging pyprojects reach the sources with hatchling `force-include`
entries pointing at `../../src/rya/...`, so there is one copy of every file and
no build-time staging step. The SDK's list is generated from `surface.py` and
checked against it; the server's is the whole directory.

Root `pyproject.toml` keeps `name = "rya"` deliberately: `pip install -e .`,
`uv sync`, `uv.lock` and the `Dockerfile` are unchanged, and the dev environment
is the platform. `tests/test_sdk_surface.py` asserts `packaging/server` and the
root stay in step on version, dependencies and extras.

## Building

```bash
uv build --wheel packaging/sdk     -o dist
uv build --wheel packaging/server  -o dist
# or: python -m build --wheel packaging/sdk
```

**Never build the repo root.** `uv build --wheel .` succeeds and writes
`dist/rya-<version>-py3-none-any.whl` — the exact filename PyPI takes as the thin
SDK — containing the *whole platform*, because the root pyproject is the dev
install and is also named `rya`. It lands on the same path as the real SDK wheel
and silently replaces it. Build the two `packaging/` directories only, and prefer
separate output dirs (`-o dist/sdk`, `-o dist/server`) so a stray root build
cannot impersonate a distribution. A quick assertion that you have the right
artifact:

```bash
python -c "import zipfile,sys; n=zipfile.ZipFile(sys.argv[1]).namelist(); \
  assert 'rya/worker.py' not in n, 'this is the PLATFORM, not the SDK'; print(len(n),'entries')" \
  dist/sdk/rya-*.whl
```

**Wheels only.** `python -m build` without `--wheel` builds an sdist first and
then a wheel *from* that sdist, which cannot work here: the sdist would contain
the sources at `rya/...` while the pyproject inside it still points at
`../../src/rya/...`. Publishing sdists needs either a staging step or a hatch
build hook that rewrites the paths; it is not built yet.

## What is in the SDK wheel

17 files. `rya/__init__.py`, `errors.py`, `sdk/{__init__.py,agent.py}`,
`manifest/*`, `cloud.py`, `bundles.py`, `cli/{__init__.py,client.py,scaffold.py,
deploy_templates.py}`, `skills/*`, plus `py.typed` and `sdk/context.pyi`.

The `rya` console script survives verbatim (D16) — `uvx rya create` works — but
points at `rya.cli.client:app`, the client subset of §14's `cli/` row.
`cli/main.py` is the operator CLI and ships only in `rya-server`, which exposes
it under both `rya` and `rya-server`.

```bash
rya create <name>    # scaffold
rya init             # scaffold in place
rya check            # manifest + handler set, starts nothing (§10)
rya bundle           # the D12 content hash, computed by the same code the
                     # platform verifies with
rya publish          # upload that bundle to a deployment as an immutable
                     # version (§9 over HTTP — no database or bucket needed).
                     # Cannot attest readiness: readiness.py is platform code,
                     # and the control plane does not import bundles either.
rya login / logout / whoami
rya skills install
```

`publish` is also registered in `cli/main.py`, so it survives in `rya-server`. That
is not cosmetic: an editable install of the platform replaces the SDK and repoints
the `rya` console script, which is exactly what a developer dev-linking an example
repo against a local checkout does.

## `ctx` in a client repo

§14: `ctx` stays platform code and "the SDK ships type stubs". A handler is
typed against the stubs and never imports the implementation:

```python
from typing import TYPE_CHECKING

from rya import define_agent

if TYPE_CHECKING:
    from rya.sdk import RuntimeContext

agent = define_agent()


@agent.on_event
async def handle(ctx: "RuntimeContext", event) -> None:
    reply = await ctx.llm.respond(system="be brief", input=event.payload)
    await ctx.approvals.request(title="send?", body=reply.text, action={})
```

`from rya.sdk import RuntimeContext` **at runtime** raises an `ImportError` that
says so, on purpose — §3's first property is that no client-versioned code holds
a store handle or makes a policy decision, and a runtime `ctx` class in the
client wheel would be a way to try.

The stubs live outside `src/` (`packaging/sdk/stubs/`) because a `context.pyi`
sitting next to `context.py` would take precedence over the real module for
anyone type-checking the *platform*, and the stub describes only the client
surface. `tests/test_sdk_surface.py` checks every name in the stub against the
live class, and checks that every `ctx.*` sub-interface the real
`RuntimeContext.__init__` builds is described.

## Known boundary exceptions

`packaging/surface.py` carries two enumerated lists, both checked *positively* —
the test asserts each exception is still real, so a resolved one fails the build
and gets promoted instead of rotting:

- **`ALLOWED_DEFERRED_EDGES`** — function-local imports from SDK code into the
  platform. They install fine and would fail at call time on a path a client
  never takes: the `ctx` re-export in `sdk/__init__.py`, the `app` re-export in
  `cli/__init__.py`, and `bundles.resolve_bundle_store` reading
  `config.legacy_env()` for its S3 arm.
- **`DEFERRED_SDK_MODULES`** — §14 rows this split does *not* ship, with the
  reason: `readiness.py`, `evals.py`, `tools/registry.py` and `cli/main.py`.
  Each is a real disagreement with the appendix, recorded rather than hidden.
