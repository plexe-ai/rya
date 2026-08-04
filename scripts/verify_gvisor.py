#!/usr/bin/env python3
"""D23's untested criterion, run inside the sandbox rather than reasoned about.

MULTITENANT_PLAN §6 "What is not proven" listed two things gVisor's absence left
open, and they are different sizes:

1. **The third-party wheels.** ``psycopg``, ``pydantic-core`` and ``cryptography``
   under ``runsc`` — the three that do syscall-heavy or crypto-accelerated work.
   D23 rests on the answer, and §9's trigger says that if one of them fails, D23 is
   reopened and the Kata / per-tenant-node-pool alternatives become live again.
2. **The isolation probe's positive path.** Its negative path had a real test
   (`test_a_container_on_a_host_kernel_is_refuted` feeds it this machine's actual
   `/proc/version`); its positive path was asserted against a captured fixture,
   which is a recording rather than a measurement.

This script answers both, from *inside* a gVisor sandbox. It is the payload;
``scripts/verify_gvisor.sh`` is what puts it there.

**Why each import is followed by real work.** An import proves the ELF loaded and
its `__init__` ran. It does not prove the parts that could plausibly differ under a
reimplemented kernel: `cryptography` reaches for CPU feature detection and
`getrandom`, `psycopg` opens sockets and does epoll, `pydantic-core` is a large Rust
extension whose allocator behaviour is the interesting question. So each check does
the thing the wheel exists for and reports the result, not just the import.

Output is one ``VERIFY:{json}`` line, because the caller reads it back across the
sandbox boundary where a file write does not reach the bind mount.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
import traceback
from typing import Any, Callable, Dict, List

# The three D23 names, plus the platform's own two — `yaml` and `httpx` are what
# every bundle imports before any tenant line runs, so a failure there would be the
# whole platform rather than one dependency.
CHECKS: List[tuple] = []


def check(name: str, why: str) -> Callable:
    def register(fn):
        CHECKS.append((name, why, fn))
        return fn
    return register


@check("cryptography", "the seal path; reaches for CPU feature detection and getrandom")
def _cryptography() -> dict:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    key = Fernet.generate_key()
    token = Fernet(key).encrypt(b"a tenant secret")
    assert Fernet(key).decrypt(token) == b"a tenant secret"
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"s",
                   info=b"rya").derive(b"root")
    # os.urandom is the syscall underneath, and it is the one a reimplemented kernel
    # is most likely to get subtly wrong. Two draws that collide would be catastrophic
    # and silent.
    assert os.urandom(32) != os.urandom(32)
    return {"fernet": True, "hkdf": len(derived) == 32,
            "urandom": True, "version": __import__("cryptography").__version__}


@check("pydantic-core", "a large Rust extension; allocator and unwinding behaviour")
def _pydantic() -> dict:
    from pydantic import BaseModel, ValidationError

    class Model(BaseModel):
        name: str
        count: int

    assert Model(name="a", count=2).count == 2
    try:
        Model(name="a", count="not-a-number")
        raised = False
    except ValidationError:
        # The interesting half: pydantic-core raises from Rust across the FFI
        # boundary, so a broken unwind shows up here rather than at import.
        raised = True
    import pydantic_core

    return {"validated": True, "raisedAcrossFfi": raised,
            "version": pydantic_core.__version__}


@check("psycopg", "sockets and epoll; the store's connection path")
def _psycopg() -> dict:
    import psycopg
    from psycopg import conninfo

    # No database is reachable from inside `runsc do --network=none`, and that is
    # fine: what is being tested is that the extension module loads and its C-level
    # machinery works, not that this sandbox can reach Postgres. A connect attempt to
    # a closed port exercises the socket path and must fail with psycopg's OWN error
    # rather than crashing the interpreter.
    parsed = conninfo.conninfo_to_dict("postgresql://u:p@127.0.0.1:1/db")
    outcome = "unexpected-success"
    try:
        psycopg.connect("postgresql://u:p@127.0.0.1:1/db", connect_timeout=2)
    except psycopg.OperationalError:
        outcome = "refused-cleanly"
    except Exception as exc:  # noqa: BLE001
        outcome = f"{type(exc).__name__}: {exc}"
    return {"parsed": parsed.get("host") == "127.0.0.1", "connect": outcome,
            "version": psycopg.__version__}


@check("yaml", "every manifest load, on every cold start")
def _yaml() -> dict:
    import yaml

    doc = yaml.safe_load("name: a\ntools: [{id: t}]\n")
    return {"loaded": doc["tools"][0]["id"] == "t",
            "cLoader": yaml.__with_libyaml__}


@check("httpx", "the provider and egress path; TLS and DNS")
def _httpx() -> dict:
    import httpx

    # Constructed, not called: `--network=none` is the point of the sandbox. What is
    # asserted is that the client builds its TLS context, which is where a
    # reimplemented kernel's `getrandom` and cert-store reads would surface.
    client = httpx.Client(timeout=1.0)
    try:
        return {"built": True, "version": httpx.__version__}
    finally:
        client.close()


@check("fork+import", "D27's own path: os.fork inside the sandbox")
def _fork() -> dict:
    """The one that would invalidate the whole execution plane rather than one wheel.

    Fork-per-run is D27, and a sandbox where `os.fork` misbehaves would not be a
    dependency problem — it would mean the warm pool cannot exist under gVisor and
    the claimer has to go back to import-at-startup. Cheap to check, so it is checked.
    """
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        os.write(write_fd, json.dumps({"childPid": os.getpid()}).encode())
        os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    with os.fdopen(read_fd, "rb") as fh:
        payload = json.loads(fh.read().decode())
    _, status = os.waitpid(pid, 0)
    return {"childPid": payload["childPid"], "exitCode": os.waitstatus_to_exitcode(status),
            "parentPid": os.getpid()}


def _probe_signals() -> Dict[str, str]:
    """The same three signals `ISOLATION_PROBE_SCRIPT` collects, from in here.

    Read directly rather than by shelling out to the script, so this works whether or
    not `rya` is importable in the sandbox — and reported raw so the caller can feed
    them to the real `read_isolation_signals` on the outside.
    """
    def read(path: str) -> str:
        try:
            with open(path) as fh:
                return fh.read()[:200].strip()
        except OSError as exc:
            return f"<unreadable: {exc.__class__.__name__}>"

    try:
        dmesg = subprocess.run(["dmesg"], capture_output=True, text=True,
                               timeout=10).stdout[:200].strip()
    except Exception:  # noqa: BLE001
        dmesg = ""
    return {"version": read("/proc/version"), "dmesg": dmesg,
            "self": ",".join(sorted(os.listdir("/proc/self"))[:40])}


def main() -> int:
    results = []
    for name, why, fn in CHECKS:
        t0 = time.perf_counter()
        try:
            detail = fn()
            results.append({"name": name, "why": why, "ok": True, "detail": detail,
                            "ms": round((time.perf_counter() - t0) * 1000, 1)})
        except BaseException as exc:  # noqa: BLE001 - a failure IS the finding
            results.append({"name": name, "why": why, "ok": False,
                            "error": f"{type(exc).__name__}: {exc}",
                            "traceback": traceback.format_exc()[-800:],
                            "ms": round((time.perf_counter() - t0) * 1000, 1)})
    payload = {
        "python": sys.version.split()[0],
        "machine": platform.machine(),
        "uname": platform.release(),
        "signals": _probe_signals(),
        "checks": results,
        "ok": all(r["ok"] for r in results),
    }
    print("VERIFY:" + json.dumps(payload))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
