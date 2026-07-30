"""The SDK/platform boundary, enforced (PLATFORM_DESIGN D16, §11 items 11 and 13).

§11 item 13 states the acceptance bar for the boundary: "a second client repo
built by someone who has never opened the `rya` codebase. If that is not
possible, the boundary leaked." Nobody notices a leak by reading — a client wheel
leaks the moment one SDK module imports one platform module three hops away. So
this file walks the **real import graph** (`ast`, every `.py` under `src/rya`,
relative imports resolved, parent packages included because importing
`rya.a.b` executes `rya/a/__init__.py`) and fails when the closure of
`packaging/surface.py:SDK_MODULES` touches anything else.

It also checks the packaging, because a boundary that only holds in a test is
not a distribution: the SDK wheel's file list must equal `SDK_MODULES`, its
declared dependencies must equal the third-party closure of that code, and the
`rya-server` pyproject must stay in step with the root one.

Why module-scope and deferred imports are treated differently: a module-scope
platform import breaks `import rya` outright, and is never allowed. A
function-local one survives installation and breaks at call time on a path the
client may never take — still a leak, so each is enumerated in
`ALLOWED_DEFERRED_EDGES` with the reason it is tolerated. Neither list may grow
without an explicit edit here.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

try:  # 3.11+; the project floor is 3.10, so the packaging assertions skip there
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
PACKAGING = REPO_ROOT / "packaging"


def _load_surface():
    spec = importlib.util.spec_from_file_location("_rya_surface", PACKAGING / "surface.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


surface = _load_surface()


# ---------------------------------------------------------------------------
# The import graph
# ---------------------------------------------------------------------------
MODULE_SCOPE = "module"
DEFERRED = "deferred"       # inside a def/class body — survives install, breaks at call time
TYPING_ONLY = "typing"      # under `if TYPE_CHECKING:` — never executed
_RANK = {MODULE_SCOPE: 3, DEFERRED: 2, TYPING_ONLY: 1}


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _all_modules() -> dict[str, Path]:
    return {_module_name(p): p for p in sorted(SRC.rglob("*.py"))}


class _Imports(ast.NodeVisitor):
    def __init__(self, module: str, is_package: bool, known: dict[str, Path]) -> None:
        self.module, self.is_package, self.known = module, is_package, known
        self.depth = 0          # >0 => inside a function/class body
        self.type_checking = 0
        self.edges: list[tuple[str, str]] = []

    def _kind(self) -> str:
        if self.type_checking:
            return TYPING_ONLY
        return DEFERRED if self.depth else MODULE_SCOPE

    def _add(self, dotted: str) -> None:
        if not dotted or dotted.split(".")[0] != "rya":
            return
        # `from rya.x import Thing` names a module or an attribute of one; walk
        # up until a real module is found so both resolve to the same node.
        cand = dotted
        while cand and cand not in self.known:
            cand = cand.rsplit(".", 1)[0] if "." in cand else ""
        if cand and cand != self.module:
            self.edges.append((cand, self._kind()))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._add(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            base = self.module.split(".") if self.is_package else self.module.split(".")[:-1]
            up = node.level - 1
            base = base[: len(base) - up] if up else base
            full = ".".join(base + ([node.module] if node.module else []))
        else:
            full = node.module or ""
        if not full.startswith("rya"):
            return
        self._add(full)
        for alias in node.names:
            self._add(f"{full}.{alias.name}")

    def _scoped(self, node) -> None:
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1

    visit_FunctionDef = _scoped
    visit_AsyncFunctionDef = _scoped

    def visit_If(self, node: ast.If) -> None:
        test = node.test
        is_tc = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        )
        if not is_tc:
            self.generic_visit(node)
            return
        self.type_checking += 1
        for child in node.body:
            self.visit(child)
        self.type_checking -= 1
        for child in node.orelse:
            self.visit(child)


def _third_party(path: Path) -> tuple[set[str], set[str]]:
    """(module-scope, deferred) non-stdlib top-level package names.

    The distinction is the same one the `rya` edges get: a module-scope import
    must be a hard requirement of the distribution or the wheel is broken on
    install; a function-local one is an optional code path (`bundles.py`'s S3
    arm imports `boto3` that way) and is declared as optional instead.
    """
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    hard: set[str] = set()
    soft: set[str] = set()
    depth = 0

    class _Scan(ast.NodeVisitor):
        def _record(self, names: set[str]) -> None:
            target = soft if depth else hard
            target |= {n for n in names if n not in stdlib and n != "rya"}

        def visit_Import(self, node: ast.Import) -> None:
            self._record({a.name.split(".")[0] for a in node.names})

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.level == 0 and node.module:
                self._record({node.module.split(".")[0]})

        def _scoped(self, node) -> None:
            nonlocal depth
            depth += 1
            self.generic_visit(node)
            depth -= 1

        visit_FunctionDef = _scoped
        visit_AsyncFunctionDef = _scoped

    _Scan().visit(ast.parse(path.read_text()))
    return hard, soft - hard


def _build_graph():
    modules = _all_modules()
    graph: dict[str, dict[str, str]] = {}
    external: dict[str, tuple[set[str], set[str]]] = {}
    for name, path in modules.items():
        tree = ast.parse(path.read_text())
        visitor = _Imports(name, path.name == "__init__.py", modules)
        visitor.visit(tree)
        edges: dict[str, str] = {}
        for target, kind in visitor.edges:
            if _RANK[kind] > _RANK.get(edges.get(target, TYPING_ONLY), 0):
                edges[target] = kind
        # Importing `rya.a.b` also executes `rya/__init__.py` and `rya/a/__init__.py`.
        for target, kind in list(edges.items()):
            parts = target.split(".")
            for i in range(1, len(parts)):
                parent = ".".join(parts[:i])
                if parent in modules and parent != name and parent not in edges:
                    edges[parent] = kind
        graph[name] = edges
        external[name] = _third_party(path)
    return modules, graph, external


MODULES, GRAPH, EXTERNAL = _build_graph()
SDK = set(surface.SDK_MODULES)


def _closure(roots: set[str]) -> tuple[set[str], dict[str, tuple[str, ...]]]:
    """Modules executed by importing `roots`, with a witness path for each."""
    seen: set[str] = set()
    paths: dict[str, tuple[str, ...]] = {r: (r,) for r in roots}
    stack = sorted(roots)
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        for target, kind in sorted(GRAPH.get(node, {}).items()):
            if kind != MODULE_SCOPE:
                continue
            paths.setdefault(target, paths[node] + (target,))
            if target not in seen:
                stack.append(target)
    return seen, paths


# ---------------------------------------------------------------------------
# The declaration itself
# ---------------------------------------------------------------------------
def test_declared_sdk_modules_exist():
    missing = sorted(SDK - set(MODULES))
    assert not missing, f"packaging/surface.py names modules that do not exist: {missing}"


def test_sdk_data_files_exist():
    for wheel_path, source in surface.SDK_DATA_FILES.items():
        assert (REPO_ROOT / source).is_file(), f"{wheel_path} has no source at {source}"


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------
def test_sdk_import_closure_contains_no_platform_module():
    """`import rya` in a client repo must not need the platform (D2, §3).

    Walked transitively, because the leak that matters is never the direct edge:
    `rya/__init__.py` -> `rya.sdk.agent` looks like SDK-only code and used to
    drag in `store`, `guard`, `providers` and `config` through `rya/sdk/__init__.py`.
    """
    reached, paths = _closure(SDK)
    leaks = sorted(reached - SDK)
    report = "\n".join(f"  {' -> '.join(paths[m])}" for m in leaks)
    assert not leaks, (
        "SDK modules reach platform code at import time. Either the import is "
        "accidental (make it function-local or type-only), or the module belongs "
        "in the platform (drop it from SDK_MODULES with a reason):\n" + report
    )


def test_deferred_platform_edges_are_enumerated():
    """Function-local platform imports install fine and fail at call time.

    Not a leak that breaks `import rya`, so it is allowed — but only by name, in
    `ALLOWED_DEFERRED_EDGES`, with the reason it is acceptable.
    """
    found = {
        (module, target)
        for module in sorted(SDK)
        for target, kind in GRAPH.get(module, {}).items()
        if kind == DEFERRED and target not in SDK
    }
    allowed = set(surface.ALLOWED_DEFERRED_EDGES)
    undeclared = sorted(found - allowed)
    assert not undeclared, (
        "SDK modules defer-import platform code without an entry in "
        f"ALLOWED_DEFERRED_EDGES: {undeclared}"
    )
    stale = sorted(allowed - found)
    assert not stale, f"ALLOWED_DEFERRED_EDGES lists edges that no longer exist: {stale}"


def test_deferred_sdk_modules_still_leak():
    """Every §14 row we declined to ship must still have a reason to decline.

    This is what stops the allowlist rotting into a list of excuses: if someone
    untangles `readiness.py` from the store and the providers, this test fails
    and says so, instead of leaving the module quietly out of the SDK forever.
    """
    still_leaking = {}
    for module, reason in surface.DEFERRED_SDK_MODULES.items():
        assert module in MODULES, f"DEFERRED_SDK_MODULES names a module that does not exist: {module}"
        assert module not in SDK, f"{module} is both deferred and shipped"
        assert len(reason) > 40, f"{module} needs a real reason, not '{reason}'"
        reached, _ = _closure({module})
        platform = reached - SDK - {module}
        # Deferred edges count here: `tools/registry.py` has no module-scope
        # platform import, but `default_registry()` imports `tools/builtins.py`
        # -> `guard.py` — a client wheel that shipped it would install cleanly
        # and raise ImportError on first call, which is worse, not better.
        for node in reached:
            platform |= {
                t for t, kind in GRAPH.get(node, {}).items()
                if kind == DEFERRED and t not in SDK and t not in reached
            }
        still_leaking[module] = sorted(platform)
    resolved = sorted(m for m, leaks in still_leaking.items() if not leaks)
    assert not resolved, (
        "these modules no longer reach platform code at all — promote them into "
        f"SDK_MODULES and delete their DEFERRED_SDK_MODULES entry: {resolved}"
    )


def test_sdk_third_party_imports_match_declared_dependencies():
    """A client wheel that imports what it does not depend on is broken on install."""
    reached, _ = _closure(SDK)
    hard = set().union(*(EXTERNAL[m][0] for m in reached))
    soft = set().union(*(EXTERNAL[m][1] for m in reached))
    undeclared = sorted(hard - set(surface.SDK_THIRD_PARTY))
    assert not undeclared, (
        f"SDK code imports these at module scope but `rya` does not depend on them: {undeclared}"
    )
    unclaimed = sorted(soft - set(surface.SDK_THIRD_PARTY) - set(surface.SDK_OPTIONAL_THIRD_PARTY))
    assert not unclaimed, (
        "SDK code defer-imports third-party packages that are neither a dependency nor "
        f"declared optional in SDK_OPTIONAL_THIRD_PARTY: {unclaimed}"
    )
    stale = sorted(set(surface.SDK_OPTIONAL_THIRD_PARTY) - soft)
    assert not stale, f"SDK_OPTIONAL_THIRD_PARTY names packages the SDK never imports: {stale}"


# ---------------------------------------------------------------------------
# The distributions
# ---------------------------------------------------------------------------
def _pyproject(rel: str) -> dict:
    if tomllib is None:  # pragma: no cover
        pytest.skip("tomllib requires Python 3.11+")
    return tomllib.loads((REPO_ROOT / rel).read_text())


def test_sdk_wheel_ships_exactly_the_declared_surface():
    """The wheel's file list is the declaration, or the declaration is fiction."""
    data = _pyproject("packaging/sdk/pyproject.toml")
    include = data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]

    expected: dict[str, str] = {}
    for module in SDK:
        path = MODULES[module].relative_to(REPO_ROOT).as_posix()
        expected[f"../../{path}"] = path[len("src/"):]
    for wheel_path, source in surface.SDK_DATA_FILES.items():
        rel = Path(source).relative_to("packaging/sdk").as_posix()
        expected[rel] = wheel_path

    assert include == expected, (
        "packaging/sdk/pyproject.toml has drifted from packaging/surface.py.\n"
        f"  only in pyproject: {sorted(set(include) - set(expected))}\n"
        f"  only in surface:   {sorted(set(expected) - set(include))}"
    )


def test_sdk_distribution_metadata():
    data = _pyproject("packaging/sdk/pyproject.toml")["project"]
    assert data["name"] == "rya", "D16: `rya` on PyPI stays the client SDK"
    # D16: "`uvx rya create` survives verbatim" — same script name, client subset.
    assert data["scripts"] == {"rya": "rya.cli.client:app"}
    assert sorted(data["dependencies"]) == sorted(surface.SDK_THIRD_PARTY.values())


def test_server_distribution_tracks_the_root_project():
    """`rya-server` is the platform, so it is the root project under another name.

    Root `pyproject.toml` is the editable dev install (§2: the dev environment is
    the platform) and is never published; `packaging/server` is what gets built.
    Keeping the dependency sets identical by test is cheaper than a build hook
    and fails loudly the first time someone adds an extra to only one of them.
    """
    root = _pyproject("pyproject.toml")["project"]
    server = _pyproject("packaging/server/pyproject.toml")["project"]
    assert server["name"] == "rya-server"
    assert server["version"] == root["version"]
    assert server["dependencies"] == root["dependencies"]
    assert server["optional-dependencies"] == root["optional-dependencies"]
    # The platform keeps the full CLI behind both names.
    assert server["scripts"]["rya"] == "rya.cli.main:app"
    assert server["scripts"]["rya-server"] == "rya.cli.main:app"


# ---------------------------------------------------------------------------
# The ctx stubs (§14: "the SDK ships type stubs")
# ---------------------------------------------------------------------------
def _stub_tree() -> ast.Module:
    return ast.parse((REPO_ROOT / surface.SDK_DATA_FILES["rya/sdk/context.pyi"]).read_text())


def test_ctx_stub_matches_the_real_runtime_context():
    """A stub that promises a method `ctx` does not have is worse than no stub.

    The client's handler is `async def handle(ctx, event)` and the client has no
    platform installed, so the stub is the only description of `ctx` their editor
    and type checker ever see. It is checked against the live class rather than
    reviewed by eye.
    """
    pytest.importorskip("pydantic")
    from rya.sdk import context as real

    stub = _stub_tree()
    classes = {n.name: n for n in stub.body if isinstance(n, ast.ClassDef)}

    # Every stubbed class exists, and every method on it exists on the real one.
    for name, node in classes.items():
        target = getattr(real, name, None)
        assert target is not None, f"context.pyi declares class {name}, which context.py does not"
        stubbed = {b.name for b in node.body if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))}
        missing = sorted(m for m in stubbed if not hasattr(target, m))
        assert not missing, f"context.pyi declares {name}.{missing} which does not exist"

    # Every `ctx.<name>` sub-interface the real class builds is described. Read
    # off `RuntimeContext.__init__` (context.py:252-269, "the spec's ctx.*
    # surface") rather than hardcoded, so a new one added there fails here.
    ctx_source = ast.parse(Path(real.__file__).read_text())
    ctx_attrs: set[str] = set()
    for node in ast.walk(ctx_source):
        if not isinstance(node, ast.FunctionDef) or node.name != "__init__":
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.Call):
                continue
            fn = stmt.value.func
            target = stmt.targets[0]
            if (
                isinstance(fn, ast.Name)
                and fn.id.startswith("_")
                and isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                ctx_attrs.add(target.attr)
    assert len(ctx_attrs) >= 15, f"failed to read the ctx.* surface off context.py (got {ctx_attrs})"

    stubbed_attrs = {
        t.target.id
        for t in classes["RuntimeContext"].body
        if isinstance(t, ast.AnnAssign) and isinstance(t.target, ast.Name)
    }
    assert ctx_attrs <= stubbed_attrs, (
        f"context.pyi is missing ctx.* surfaces: {sorted(ctx_attrs - stubbed_attrs)}"
    )
