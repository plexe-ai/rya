#!/usr/bin/env python3
"""Phase 0 measurement: what a cold start actually costs.

`docs/MULTITENANT_PLAN.md` §2 requires two numbers before the expensive parts of
the epic are designed:

  - **fork + import** — gates D27's claimer scope, and so decides whether Phase 5
    (widen the claimer to per-tenant) happens at all. The re-plan trigger is
    "fork + import ≈ 2s budget even warm".
  - **`runsc` cold start** — can invalidate D23's choice of gVisor.

This script produces the first directly, and produces the second's *baseline*:
sandbox overhead is these same stages re-run under `runsc` minus these numbers.
That is why the stages are decomposed rather than reported as one total — an
unattributable 2.4s tells you nothing about which decision it threatens.

Everything is measured against ``COLD_START_TARGET_MS`` (``worker.py``), which is
the budget a worker has from process start to reaching the claim loop.

Usage
-----
    python scripts/bench_cold_start.py                    # all stages, table
    python scripts/bench_cold_start.py --json out.json    # machine-readable
    python scripts/bench_cold_start.py --iterations 15
    python scripts/bench_cold_start.py --agent examples/loan-renewal

No production code imports this. It is an instrument, not a feature.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"

# The modules a real `rya worker` has resident by the time it reaches the claim
# loop. Measured as one set because that is how it is paid: the worker does not
# get to skip psycopg because a given run never touches Postgres.
PLATFORM_IMPORTS = [
    "rya.worker",
    "rya.runtime.engine",
    "rya.store_postgres",
    "rya.bundles",
    "rya.guard",
]

# A tenant agent heavier than anything in examples/. The plan names
# "pydantic, httpx, a provider SDK" as the realistic surface; our own examples
# import only `rya` plus stdlib, so measuring only those would flatter the result.
SYNTHETIC_ENTRYPOINT = '''\
"""Synthetic tenant agent — the pessimistic import surface (bench only)."""

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pydantic

try:
    import anthropic  # provider SDK
except ImportError:
    anthropic = None
try:
    import boto3  # object store / bedrock
except ImportError:
    boto3 = None

from rya import define_agent


class _Profile(pydantic.BaseModel):
    """A model defined at import time, so pydantic-core builds a validator."""

    id: str
    score: float = 0.0
    tags: list[str] = []


agent = define_agent()


@agent.on_event
async def handle_event(ctx, event):
    ctx.logs.info("synthetic", type=event.type)
    return {"ok": True}
'''

SYNTHETIC_MANIFEST = """\
name: bench-synthetic
runtime: python
entrypoint: src/agent.py
version: 0.1.0
"""


# ---------------------------------------------------------------- measurement


@dataclass
class Stage:
    key: str
    label: str
    note: str
    samples_ms: list[float] = field(default_factory=list)

    @property
    def median(self) -> float:
        return statistics.median(self.samples_ms) if self.samples_ms else float("nan")

    @property
    def p95(self) -> float:
        if not self.samples_ms:
            return float("nan")
        ordered = sorted(self.samples_ms)
        return ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]

    @property
    def best(self) -> float:
        return min(self.samples_ms) if self.samples_ms else float("nan")

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "note": self.note,
            "median_ms": round(self.median, 1),
            "p95_ms": round(self.p95, 1),
            "min_ms": round(self.best, 1),
            "samples": len(self.samples_ms),
        }


def _child_env() -> dict:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{SRC}{os.pathsep}{existing}" if existing else str(SRC)
    # A cold start in production is cold: no warm bytecode cache advantage that a
    # repeated benchmark would otherwise accumulate for free.
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    return env


def _time_subprocess(code: str, iterations: int) -> list[float]:
    """Cold-interpreter stages: a fresh process per sample, timed from the outside.

    Timed externally on purpose — `python -X importtime` and in-process timers both
    omit exec + interpreter bring-up, which is the part scale-to-zero pays.
    """
    env = _child_env()
    out: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        proc = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
        )
        elapsed = (time.perf_counter_ns() - t0) / 1e6
        if proc.returncode != 0:
            raise RuntimeError(
                f"bench child failed ({proc.returncode}):\n{proc.stderr[-2000:]}"
            )
        out.append(elapsed)
    return out


def _time_fork(setup: str, work: str, iterations: int) -> list[float]:
    """Warm-parent stages: import `setup` once, then fork per sample and run `work`.

    The number reported is what a supervisor would observe: fork, child does the
    work, child signals. `os._exit` skips interpreter teardown and atexit handlers,
    which a discarded run-fork would also skip.
    """
    driver = f"""
import os, sys, time, json
{setup}

samples = []
for _ in range({iterations}):
    r, w = os.pipe()
    t0 = time.perf_counter_ns()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
{_indent(work, 12)}
            os.write(w, b"k")
        except BaseException as exc:
            try:
                os.write(w, ("e" + repr(exc)[:400]).encode())
            except Exception:
                pass
        finally:
            os._exit(0)
    os.close(w)
    got = b""
    while True:
        chunk = os.read(r, 4096)
        if not chunk:
            break
        got += chunk
    samples.append((time.perf_counter_ns() - t0) / 1e6)
    os.close(r)
    os.waitpid(pid, 0)
    if not got.startswith(b"k"):
        print("CHILDFAIL:" + got.decode(errors="replace"), file=sys.stderr)
        raise SystemExit(3)

print("RESULT:" + json.dumps(samples))
"""
    proc = subprocess.run(
        [sys.executable, "-c", driver],
        env=_child_env(),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"fork bench failed:\n{proc.stderr[-2000:]}")
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT:"):
            return json.loads(line[len("RESULT:") :])
    raise RuntimeError(f"fork bench produced no result:\n{proc.stdout[-2000:]}")


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + ln if ln.strip() else ln for ln in text.strip().splitlines())


# ------------------------------------------------------------------- fixtures


def _make_synthetic_project(root: Path) -> Path:
    proj = root / "bench-synthetic"
    (proj / "src").mkdir(parents=True, exist_ok=True)
    (proj / "src" / "agent.py").write_text(SYNTHETIC_ENTRYPOINT)
    (proj / "rya.agent.yaml").write_text(SYNTHETIC_MANIFEST)
    return proj


def _load_agent_code(project: Path) -> str:
    return (
        "from pathlib import Path\n"
        "from rya.manifest import load_manifest\n"
        "from rya.runtime.engine import load_agent\n"
        f"_root = Path({str(project)!r})\n"
        "load_agent(load_manifest(_root / 'rya.agent.yaml'), _root)\n"
    )


# --------------------------------------------------------------------- stages


def run_stages(project: Path, iterations: int, *, include_bundle: bool) -> list[Stage]:
    platform_import = "\n".join(f"import {m}" for m in PLATFORM_IMPORTS)
    agent_code = _load_agent_code(project)

    stages: list[Stage] = []

    s = Stage("interp", "interpreter floor", "`python -c pass`, timed externally")
    s.samples_ms = _time_subprocess("pass", iterations)
    stages.append(s)

    s = Stage(
        "platform",
        "+ platform import",
        f"cold: interpreter + {len(PLATFORM_IMPORTS)} platform modules",
    )
    s.samples_ms = _time_subprocess(platform_import, iterations)
    stages.append(s)

    s = Stage(
        "cold_total",
        "+ load_agent (COLD TOTAL)",
        "what scale-to-zero pays today: fresh process to agent loaded",
    )
    s.samples_ms = _time_subprocess(platform_import + "\n" + agent_code, iterations)
    stages.append(s)

    s = Stage(
        "fork_miss",
        "fork + load_agent (POOL MISS)",
        "THE PHASE 5 NUMBER: warm platform parent, agent imported in the child",
    )
    s.samples_ms = _time_fork(platform_import, agent_code, iterations)
    stages.append(s)

    s = Stage(
        "fork_hit",
        "fork only (POOL HIT)",
        "parent already holds the agent; child only dispatches",
    )
    s.samples_ms = _time_fork(platform_import + "\n" + agent_code, "pass", iterations)
    stages.append(s)

    if include_bundle:
        stages.append(_bundle_stage(project, iterations))

    return stages


def _bundle_stage(project: Path, iterations: int) -> Stage:
    """Bundle materialisation: unpack + hash-verify, as a pinned worker does it."""
    code = f"""
import tempfile, shutil
from pathlib import Path
from rya import bundles

_proj = Path({str(project)!r})
_b = bundles.build_bundle(_proj)
_tmp = Path(tempfile.mkdtemp())
_archive = bundles.pack(_b, _tmp / "a.tar.gz")

import time, json
samples = []
for _ in range({iterations}):
    dest = _tmp / f"u{{_}}{{time.perf_counter_ns()}}"
    t0 = time.perf_counter_ns()
    bundles.unpack(_archive, dest)
    bundles.verify(dest, _b.hash)
    samples.append((time.perf_counter_ns() - t0) / 1e6)
    shutil.rmtree(dest, ignore_errors=True)
shutil.rmtree(_tmp, ignore_errors=True)
print("RESULT:" + json.dumps(samples))
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=_child_env(),
        capture_output=True,
        text=True,
    )
    s = Stage(
        "bundle",
        "bundle unpack + verify",
        "in-process; the pinned/sandbox path pays this before any import",
    )
    if proc.returncode != 0:
        s.note = f"FAILED: {proc.stderr.strip().splitlines()[-1][:160] if proc.stderr.strip() else 'unknown'}"
        return s
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT:"):
            s.samples_ms = json.loads(line[len("RESULT:") :])
    return s


# --------------------------------------------------------------------- report


def _target_ms() -> int:
    """Read COLD_START_TARGET_MS from the source, so the budget cannot drift."""
    text = (SRC / "rya" / "worker.py").read_text()
    for line in text.splitlines():
        if line.startswith("COLD_START_TARGET_MS"):
            return int(line.split("=")[1].split("#")[0].strip())
    raise RuntimeError("COLD_START_TARGET_MS not found in worker.py")


def _agent_fingerprint(project: Path) -> dict:
    py = sorted(p for p in (project / "src").rglob("*.py")) if (project / "src").is_dir() else []
    return {
        "path": str(project),
        "name": project.name,
        "py_files": len(py),
        "py_lines": sum(len(p.read_text().splitlines()) for p in py),
    }


def _installed_versions() -> dict:
    out = {}
    for mod in ("pydantic", "httpx", "anthropic", "boto3", "psycopg", "fastapi", "mcp"):
        code = f"import {mod}; print(getattr({mod}, '__version__', 'unknown'))"
        proc = subprocess.run(
            [sys.executable, "-c", code], env=_child_env(), capture_output=True, text=True
        )
        out[mod] = proc.stdout.strip() if proc.returncode == 0 else "MISSING"
    return out


def report(stages: list[Stage], meta: dict, target: int) -> None:
    print()
    print("=" * 78)
    print("  Rya cold-start measurement — MULTITENANT_PLAN.md Phase 0")
    print("=" * 78)
    print(f"  host      {meta['platform']['machine']} / {meta['platform']['system']} "
          f"/ {meta['platform']['cpus']} cpu")
    print(f"  python    {meta['platform']['python']}")
    print(f"  agent     {meta['agent']['name']} "
          f"({meta['agent']['py_files']} file(s), {meta['agent']['py_lines']} lines)")
    print(f"  budget    COLD_START_TARGET_MS = {target}")
    print(f"  samples   {meta['iterations']} per stage")
    missing = [k for k, v in meta["versions"].items() if v == "MISSING"]
    if missing:
        print(f"  NOTE      not installed, so NOT in these numbers: {', '.join(missing)}")
    print("-" * 78)
    print(f"  {'stage':<32} {'median':>9} {'p95':>9} {'min':>9}  {'% budget':>8}")
    print("-" * 78)
    for s in stages:
        if not s.samples_ms:
            print(f"  {s.label:<32} {'--':>9} {'--':>9} {'--':>9}  {'--':>8}   {s.note}")
            continue
        pct = 100.0 * s.median / target
        print(f"  {s.label:<32} {s.median:>8.1f}ms {s.p95:>8.1f}ms {s.best:>8.1f}ms "
              f"{pct:>7.1f}%")
    print("-" * 78)
    for s in stages:
        print(f"  {s.key:<11} {s.note}")
    print("=" * 78)
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--iterations", type=int, default=10)
    ap.add_argument("--agent", default=None,
                    help="project root to load (default: a synthetic heavy agent)")
    ap.add_argument("--json", dest="json_out", default=None)
    # Writes from inside a `runsc do` sandbox do not reach a host bind mount, so
    # the gVisor arm of bench_gvisor.sh collects its results off stdout instead.
    ap.add_argument("--json-stdout", action="store_true",
                    help="also emit one-line JSON prefixed JSONRESULT: (sandbox-safe)")
    ap.add_argument("--no-bundle", action="store_true")
    ap.add_argument("--label", default=None, help="tag this run in the JSON output")
    args = ap.parse_args()

    if not hasattr(os, "fork"):
        print("This measurement requires os.fork (Linux/macOS).", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="rya-bench-"))
    try:
        project = Path(args.agent).resolve() if args.agent else _make_synthetic_project(tmp)
        if not (project / "rya.agent.yaml").is_file():
            print(f"No rya.agent.yaml at {project}", file=sys.stderr)
            return 2

        target = _target_ms()
        meta = {
            "label": args.label or ("synthetic" if not args.agent else project.name),
            "iterations": args.iterations,
            "target_ms": target,
            "agent": _agent_fingerprint(project),
            "versions": _installed_versions(),
            "platform": {
                "machine": platform.machine(),
                "system": platform.system(),
                "release": platform.release(),
                "python": platform.python_version(),
                "cpus": os.cpu_count(),
            },
        }
        stages = run_stages(project, args.iterations, include_bundle=not args.no_bundle)
        report(stages, meta, target)

        payload = {"meta": meta, "stages": [s.as_dict() for s in stages]}
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(payload, indent=2) + "\n")
            print(f"wrote {args.json_out}")
        if args.json_stdout:
            print("JSONRESULT:" + json.dumps(payload))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
