"""The SDK surface, declared in code (PLATFORM_DESIGN D16, §14, §11 item 11).

D16 splits one source tree into two distributions: `rya` on PyPI is the thin
client SDK, the platform ships as `rya-server`. The split is achieved by
**packaging plus enforced import discipline**, not by relocating modules — the
import package stays `rya`, every module stays where it is, and this file is the
line between them. Everything under `src/rya` that is not listed in
`SDK_MODULES` is platform code.

Three consumers read this file, which is the point of it existing:

1. `tests/test_sdk_surface.py` walks the real import graph and fails if an SDK
   module reaches platform code. §11 item 13 sets the bar — "a second client
   repo built by someone who has never opened the `rya` codebase; if that is not
   possible, the boundary leaked" — and that test is what detects the leak.
2. `packaging/sdk/pyproject.toml` lists exactly these files; the same test
   asserts the two lists are equal, so packaging cannot drift from the
   declaration.
3. Anyone asking "is this module client-visible?" reads `SDK_MODULES` instead of
   guessing from §14 prose.

`DEFERRED_SDK_MODULES` and `ALLOWED_DEFERRED_EDGES` are the honest part: §14
assigns some modules to the SDK that today's implementations cannot honour, and
a short enumerated allowlist beats a green test that proves nothing. Both are
checked *positively* — the test asserts each exception is still real, so a
resolved one fails the build and gets promoted rather than rotting.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# The thin SDK: what actually ships in the `rya` wheel.
# ---------------------------------------------------------------------------
# Derived from §14's appendix table. Each entry is a module name, not a path;
# `__init__.py` is named by its package.
SDK_MODULES: frozenset[str] = frozenset({
    # §2's client contract: "a client repo needs `rya-sdk` and a deploy token."
    "rya",                        # `from rya import define_agent`
    "rya.errors",                 # the stable E_* codes a client branches on
    "rya.sdk",                    # package init; ctx is re-exported lazily
    "rya.sdk.agent",              # §14: "decorators are pure declaration"
    # §14: `manifest/` is BOTH — "shared schema, versioned". The client authors
    # and validates `rya.agent.yaml`; the platform validates it again on
    # admission. One schema, or the two disagree.
    "rya.manifest",
    "rya.manifest.loader",
    "rya.manifest.schema",
    # §14: `cloud.py` — "already the client-side connection store".
    "rya.cloud",
    # §14: `cli/` splits into an operator subset and a client subset.
    "rya.cli",
    "rya.cli.client",             # the `rya` console script in this distribution
    "rya.cli.scaffold",           # `rya create` — D16's "survives verbatim"
    "rya.cli.deploy_templates",   # writes Dockerfile/compose into the CLIENT's repo
    # Not in §14 (both postdate it), justified in DEVIATIONS below.
    "rya.bundles",                # D12: the client computes the hash the platform verifies
    "rya.skills",                 # coding-agent skills are authoring-side
    "rya.skills.skill_data",
})

# Non-module members of the SDK wheel, as `wheel path -> source path relative to
# the repo root`.
SDK_DATA_FILES: dict[str, str] = {
    # §14: for `sdk/context.py` "the SDK ships type stubs". They live outside
    # `src/` deliberately: a `context.pyi` sitting next to `context.py` would
    # take precedence over the real module for anyone type-checking the
    # *platform*, and the stub only describes the client-facing surface.
    "rya/sdk/context.pyi": "packaging/sdk/stubs/rya/sdk/context.pyi",
    # PEP 561 — without this the stubs are invisible to the client's checker.
    # SDK-only: the platform wheel makes no typing promise about its internals.
    "rya/py.typed": "packaging/sdk/py.typed",
}

# Third-party imports the SDK modules are allowed to make, mapped to the
# requirement that supplies them. The test derives the SDK's real third-party
# closure and compares, so `rya`'s dependency list cannot silently drift from
# what its code imports — the failure mode being a client wheel that installs
# and then raises ImportError on first use.
SDK_THIRD_PARTY: dict[str, str] = {
    "typer": "typer>=0.12",
    "rich": "rich>=13.7",
    "pydantic": "pydantic>=2.6",
    "yaml": "pyyaml>=6.0",
}

# Third-party packages SDK code imports only from inside a function, on a code
# path a client install never takes. Declared rather than depended on, so the
# client wheel stays four libraries wide.
SDK_OPTIONAL_THIRD_PARTY: dict[str, str] = {
    "boto3": (
        "`bundles._s3_client` — the object-storage arm of the bundle store (§5.3), "
        "which is a platform concern; the client path is `build_bundle`/`pack`. "
        "Same root cause as the rya.bundles -> rya.config entry below."
    ),
    "botocore": (
        "`bundles._s3_client` — arrives with boto3, same function and same "
        "rationale as the entry above. Needed because `s3.addressing_style` has "
        "no environment variable in botocore, so an S3-compatible endpoint "
        "(MinIO/Ceph/R2) must be given path-style addressing via "
        "`botocore.config.Config` at client construction."
    ),
}

# ---------------------------------------------------------------------------
# Enumerated exceptions.
# ---------------------------------------------------------------------------
# Edges from an SDK module into platform code that are NOT at module scope —
# function-local imports on a code path a client install never takes. They do
# not break `import rya`, but they would break at call time, so each one is
# named with the reason it is acceptable rather than being silently tolerated.
ALLOWED_DEFERRED_EDGES: dict[tuple[str, str], str] = {
    ("rya.sdk", "rya.sdk.context"): (
        "PEP 562 re-export of RuntimeContext/Event/LLMResponse. §14 keeps `ctx` "
        "as platform code and gives the SDK the interface only, so in an "
        "SDK-only install these names exist for type checkers (context.pyi) and "
        "not at runtime. `sdk/__init__.py` turns the resulting ImportError into "
        "an explicit message. Structural: closing it means shipping a runtime "
        "`ctx` stub, which would let client code construct one."
    ),
    ("rya.cli", "rya.cli.main"): (
        "PEP 562 re-export of the operator CLI's `app`, preserved so nothing on "
        "the platform side changes. Unreachable in the SDK, whose console script "
        "is `rya.cli.client:app`. Closing it means deleting a public re-export."
    ),
    ("rya.bundles", "rya.config"): (
        "`resolve_bundle_store` reads `config.legacy_env()` for the S3 arm "
        "(bundles.py:574-583) — object storage is a platform concern (§5.3). The "
        "client path is `build_bundle`/`pack`, which never reaches it. Closing it "
        "means splitting the store-resolution half out of bundles.py, i.e. moving "
        "code, which this change explicitly does not do."
    ),
}

# §14 rows that name a module as SDK (or partly SDK) which this split does NOT
# ship, with the reason. Each is a real disagreement with the appendix, reported
# rather than silently deviated from; the test asserts each still has a
# module-scope platform dependency, so a future cleanup fails here and forces a
# promotion instead of leaving a stale excuse behind.
DEFERRED_SDK_MODULES: dict[str, str] = {
    "rya.readiness": (
        "§14 gives it to the SDK as the local `--check`. It is not separable "
        "today: `check_readiness` takes a live `store` (calls `describe()` and "
        "`list_connections()`) and imports `providers.resolve_provider` and "
        "`sdk.context.load_env` at module scope. §9 also moves it the other way "
        "— 'a server-side admission check rather than a client-side courtesy' — "
        "and `gates.py` has started that. The SDK ships `rya check` (manifest + "
        "handler set) instead, which is the part §10 says CI depends on."
    ),
    "rya.evals": (
        "§14 gives it to the SDK as the local `--check`. Running an eval means "
        "running the agent: `run_evals` builds a `runtime.Engine` and resolves a "
        "model provider. §14's own note concedes this — 'both read the local "
        "project today, so server-side versions rewrite their inputs'. Until "
        "that rewrite, evals is platform code."
    ),
    "rya.tools.registry": (
        "§14 gives the SDK the 'handler registration' half. That half is already "
        "in `sdk/agent.py` (`@agent.tool`), which is what a client actually "
        "writes; what is left in registry.py is the platform's permissioned "
        "registry, and `default_registry()` imports `tools/builtins.py` -> "
        "`guard.py`. Shipping it would put a function in the client wheel that "
        "raises ImportError the first time it is called."
    ),
    "rya.cli.main": (
        "§14's 'client subset' of `cli/`. main.py is one 1592-line module that "
        "imports `..runtime`, `..store`, `..sdk.context`, `..config` and "
        "`..models.registry` at module scope, so the subset had to become a "
        "separate entry point: `cli/client.py`. main.py stays the operator CLI "
        "and ships only in `rya-server`."
    ),
}
