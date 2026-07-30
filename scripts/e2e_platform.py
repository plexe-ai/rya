#!/usr/bin/env python3
"""End-to-end proof that an agent built with the thin SDK runs on the platform.

This is the whole product claim in one script: a client repo that installs only
``rya`` (the SDK) authors an agent, hands the platform a content-hashed bundle,
and the platform -- installed separately as ``rya-server`` -- admits it through a
promotion gate and executes it across two processes with a durable human pause.

What makes it an e2e rather than a demo is that the two sides are genuinely
separated. Two virtualenvs, two distributions, no shared import path: the client
never imports the runtime, and the platform never reads the client's source tree
-- it works from the bundle archive alone. If the SDK boundary regressed, or the
client's content hash stopped matching the server's, this fails.

Run it:

    python scripts/e2e_platform.py                # hermetic: offline mock model
    python scripts/e2e_platform.py --live         # allow real provider keys
    python scripts/e2e_platform.py --keep         # leave the workdir for poking

Outcomes are PASS, FAIL, or GAP. A GAP is a check that documents a known
platform defect rather than a broken test -- it is printed and summarised but
does not fail the run, so the harness stays useful in CI while the gap is open.
Exit code is 0 only if there are no FAILs.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("RYA_E2E_PORT", "8791"))
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "rya_e2e_operator_token"

# Provider/infra keys scrubbed unless --live. An ambient key silently turns the
# offline mock into a paid API call, which makes a "works offline" claim untested
# and the run cost money -- so hermetic is the default, not an option.
AMBIENT_KEYS = (
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "RESEND_API_KEY", "SLACK_WEBHOOK_URL",
    "RYA_DATABASE_URL", "RYA_BUNDLES_S3_BUCKET", "RYA_FILES_S3_BUCKET",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE",
    "RYA_TOKEN", "RYA_ADMIN_TOKEN", "RYA_MULTI_TENANT", "RYA_API_INLINE_WORKER",
    "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "OTEL_EXPORTER_OTLP_ENDPOINT",
)

# ---------------------------------------------------------------------------
# The client's agent. Inlined so the harness is self-contained and byte-stable:
# the bundle hash is an assertion target, so the source cannot come from a
# scaffold template that may change under us.
# ---------------------------------------------------------------------------
AGENT_PY = '''\
"""A refund agent, written against the thin `rya` SDK only.

Imports `rya` and nothing else from the platform: no engine, no store, no
server. `ctx` is supplied at run time by whichever deployment holds this bundle.

  tool call -> LLM -> DURABLE PAUSE for a human -> gated action -> send
"""

import hashlib

from rya import define_agent

agent = define_agent()


@agent.on_event
async def handle_refund(ctx, event):
    email = event.payload.get("email") or "anonymous"
    amount = float(event.payload.get("amount") or 0)

    ticket = await ctx.tools.call("billing.lookup", {"email": email})

    reply = await ctx.llm.respond(
        system="Draft a short, apologetic refund confirmation. Be concise.",
        input={"ticket": ticket, "amountUsd": amount},
    )

    # The human gate. `billing.refund` is approval_required in the manifest, so
    # the only way it ever executes is through this approval -- the handler
    # cannot call it directly and the model is never offered it.
    await ctx.approvals.request(
        title="Issue a " + format(amount, ".2f") + " USD refund to " + email,
        body=reply.text,
        action={"tool": "billing.refund",
                "input": {"ticket": ticket["id"], "amountUsd": amount}},
    )

    # Runs only after a human approved, in whichever process resumed the run.
    await ctx.channels.send("email", {"to": email, "subject": "Your refund",
                                      "body": reply.text})
    return {"ticket": ticket["id"], "refunded": amount, "notified": email}


@agent.tool("billing.lookup")
async def billing_lookup(input):
    """The client's own code. Deterministic: replay must reproduce it."""
    email = input.get("email", "anonymous")
    digest = hashlib.sha256(email.encode()).hexdigest()[:6].upper()
    return {"id": "TK-" + digest, "email": email, "plan": "pro"}


@agent.tool("billing.refund")
async def billing_refund(input):
    """The money-moving action. Reached only via an approved approval."""
    return {"issued": True, "ticket": input.get("ticket"),
            "amountUsd": input.get("amountUsd")}
'''

MANIFEST = """\
name: refund-agent
runtime: python
entrypoint: src/agent.py
version: 0.1.0
owner: e2e@example.com
instructions: >
  Handles refund requests: looks the customer up, drafts a reply, and pauses for
  a human before any money moves.

model:
  default: mock-llm
  fallback: mock-llm-mini

tools:
  - id: billing.lookup        # project code via @agent.tool, not a registry mock
    permission: allowed
  - id: billing.refund        # moves money: only reachable through an approval
    permission: approval_required

channels:
  - type: email
  - type: webhook
    path: /inbound

approvals:
  default: required_for_external_actions

observability:
  logs: true
  traces: true
  audit: true
"""

EVALS = """\
# `rya eval --attest` files this result against the version under test, which is
# what a promotion gate with --require-evals admits on.
evals:
  - id: pauses_before_moving_money
    trigger:
      type: refund.requested
      payload: { email: "ada@example.com", amount: 42.5 }
    expect:
      status: waiting_approval          # must STOP, not refund and apologise
      approval_requested: true
      tools_called: [billing.lookup]
      tools_not_called: [billing.refund]
      no_failure: true
      max_tokens: 100000
"""

GUARD = """\
# Action Guard: egress allowlist + grounding gate.
egress:
  allow: []
grounding:
  enabled: false
"""


class Harness:
    def __init__(self, workdir: Path, live: bool):
        self.dir = workdir
        self.live = live
        self.results: list[tuple[str, str, str]] = []   # (outcome, name, detail)
        self.procs: dict[str, subprocess.Popen] = {}
        self.section = ""
        self.state: dict[str, str] = {}

    # ---- reporting -------------------------------------------------------
    def head(self, title: str) -> None:
        self.section = title
        print(f"\n\033[1m── {title}\033[0m")

    def check(self, name: str, ok: bool, detail: str = "", *, gap: bool = False) -> bool:
        if ok:
            outcome = "PASS"
            colour = "\033[32m✓\033[0m"
        elif gap:
            outcome = "GAP"
            colour = "\033[33m▲\033[0m"
        else:
            outcome = "FAIL"
            colour = "\033[31m✗\033[0m"
        self.results.append((outcome, f"{self.section} / {name}", detail))
        print(f"  {colour} {name}" + (f"  \033[2m{detail}\033[0m" if detail else ""))
        return ok

    # ---- process plumbing ------------------------------------------------
    def env(self, **extra: str) -> dict:
        env = dict(os.environ)
        if not self.live:
            for k in AMBIENT_KEYS:
                env.pop(k, None)
        else:
            for k in ("RYA_TOKEN", "RYA_API_INLINE_WORKER", "RYA_MULTI_TENANT"):
                env.pop(k, None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.update(extra)
        return env

    def run(self, argv: Sequence[str | Path], cwd: Path | None = None, *,
            check: bool = True, timeout: int = 300,
            env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        p = subprocess.run([str(a) for a in argv], cwd=str(cwd or self.dir),
                           capture_output=True, text=True, timeout=timeout,
                           env=self.env(**(env_extra or {})))
        if check and p.returncode != 0:
            raise RuntimeError(f"command failed ({p.returncode}): {' '.join(map(str, argv))}\n"
                               f"stdout: {p.stdout[-2000:]}\nstderr: {p.stderr[-2000:]}")
        return p

    def json_cmd(self, argv: Sequence[str | Path], cwd: Path | None = None, *,
                 check: bool = True) -> tuple[dict, int]:
        """Run a --json CLI command and parse the LAST json line of stdout."""
        p = self.run(argv, cwd, check=check)
        blob: dict = {}
        for line in reversed((p.stdout or "").strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    blob = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        return blob, p.returncode

    def spawn(self, name: str, argv: Sequence[str | Path], cwd: Path,
              **envx: str) -> subprocess.Popen:
        log = (self.dir / f"{name}.log").open("w")
        proc = subprocess.Popen([str(a) for a in argv], cwd=str(cwd), stdout=log,
                                stderr=subprocess.STDOUT, env=self.env(**envx),
                                start_new_session=True)
        self.procs[name] = proc
        return proc

    def kill(self, name: str, sig: int = signal.SIGKILL) -> None:
        proc = self.procs.pop(name, None)
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    def teardown(self) -> None:
        for name in list(self.procs):
            self.kill(name)

    def log_tail(self, name: str, n: int = 15) -> str:
        path = self.dir / f"{name}.log"
        if not path.exists():
            return ""
        return "\n".join(path.read_text(errors="replace").splitlines()[-n:])

    # ---- http ------------------------------------------------------------
    def http(self, method: str, path: str, body: dict | None = None, *,
             token: str | None = TOKEN, timeout: int = 60) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method)
        req.add_header("content-type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode(errors="replace")
                return r.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, {"raw": raw[:400]}
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            # Not listening yet (or gone). Callers poll on this, so it must be a
            # falsy status rather than an exception that aborts the phase.
            return 0, {"unreachable": str(e)}

    def wait_for(self, predicate, *, timeout: float = 45, interval: float = 0.5):
        """Poll until predicate returns a truthy value; return it or None."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(interval)
        return None


# ===========================================================================
# Phases
# ===========================================================================
def phase_wheels(h: Harness) -> tuple[Path, Path]:
    h.head("Build the two distributions (D16)")
    dist = h.dir / "dist"
    # NOTE: build from packaging/sdk and packaging/server, never the repo root.
    # The root pyproject is the editable dev install; it is ALSO named `rya` and
    # produces an identically-named wheel containing the whole platform.
    h.run(["uv", "build", "--wheel", "-o", dist / "sdk", REPO / "packaging/sdk"])
    h.run(["uv", "build", "--wheel", "-o", dist / "server", REPO / "packaging/server"])
    sdk = next((dist / "sdk").glob("rya-*.whl"))
    server = next((dist / "server").glob("rya_server-*.whl"))

    import zipfile
    sdk_mods = {n for n in zipfile.ZipFile(sdk).namelist() if n.endswith(".py")}
    srv_mods = {n for n in zipfile.ZipFile(server).namelist() if n.endswith(".py")}
    platform_only = ["rya/worker.py", "rya/gates.py", "rya/quotas.py", "rya/store.py",
                     "rya/deployments.py", "rya/api/app.py", "rya/runtime/engine.py"]
    leaked = [m for m in platform_only if m in sdk_mods]
    h.check("SDK wheel excludes platform modules", not leaked,
            f"{len(sdk_mods)} modules" + (f", LEAKED {leaked}" if leaked else ""))
    h.check("server wheel is a superset", all(m in srv_mods for m in platform_only)
            and sdk_mods <= srv_mods, f"{len(srv_mods)} modules")
    return sdk, server


def phase_client(h: Harness, sdk: Path) -> Path:
    h.head("Client repo: install the thin SDK only")
    venv = h.dir / "venv-sdk"
    h.run(["uv", "venv", "--python", "3.12", venv])
    py = venv / "bin/python"
    h.run(["uv", "pip", "install", "--python", py, sdk])

    probe = (
        "import importlib, json, rya\n"
        "mods = ['rya.runtime.engine','rya.api.app','rya.worker','rya.gates',"
        "'rya.quotas','rya.store','rya.deployments','rya.turns','rya.queue','rya.tenancy']\n"
        "leaks = []\n"
        "for m in mods:\n"
        "    try:\n"
        "        importlib.import_module(m); leaks.append(m)\n"
        "    except ImportError: pass\n"
        "import rya.bundles as b\n"
        "print(json.dumps({'agent': callable(rya.define_agent), 'leaks': leaks,\n"
        "                  'bundles': hasattr(b,'build_bundle')}))"
    )
    out = json.loads(h.run([py, "-c", probe]).stdout.strip().splitlines()[-1])
    h.check("define_agent importable", out["agent"])
    h.check("no platform module reachable from the SDK", not out["leaks"],
            f"leaks: {out['leaks']}" if out["leaks"] else "engine/store/api/worker all absent")
    h.check("bundles ships in the SDK", out["bundles"],
            "the client must compute the hash the server verifies")

    serve = h.run([venv / "bin/rya", "serve", "--help"], check=False)
    h.check("client CLI has no `serve`", serve.returncode != 0,
            "operator commands are not in the client surface")
    return venv


def phase_author(h: Harness, venv: Path) -> tuple[Path, str]:
    h.head("Author an agent with the SDK")
    proj = h.dir / "client/refund-agent"
    (proj / "src").mkdir(parents=True, exist_ok=True)
    (proj / "src/agent.py").write_text(AGENT_PY)
    (proj / "rya.agent.yaml").write_text(MANIFEST)
    (proj / "rya.evals.yaml").write_text(EVALS)
    (proj / "rya.guard.yaml").write_text(GUARD)
    (proj / "pyproject.toml").write_text(
        '[project]\nname = "refund-agent"\nversion = "0.1.0"\n'
        'requires-python = ">=3.10"\ndependencies = ["rya"]\n')
    # A real secret in the tree: readiness must not bundle it.
    (proj / ".env").write_text("SOME_PRIVATE_TOKEN=super-secret-value\n")

    info, _ = h.json_cmd([venv / "bin/rya", "check", "--json"], proj)
    h.check("`rya check` validates with no runtime installed", info.get("ok") is True,
            f"handlers: {info.get('toolHandlers')}")

    archive = h.dir / "handoff/refund-agent.tar.gz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    b, _ = h.json_cmd([venv / "bin/rya", "bundle", "--out", archive, "--json"], proj)
    h.check("client content-hashes its bundle", bool(b.get("hash")),
            f"{b.get('hash','')[:12]}… {b.get('fileCount')} files, {b.get('sizeBytes')}B")
    h.state["hash"] = b["hash"]
    return proj, b["hash"]


def phase_platform_env(h: Harness, server: Path) -> Path:
    h.head("Platform: install rya-server separately")
    venv = h.dir / "venv-server"
    h.run(["uv", "venv", "--python", "3.12", venv])
    h.run(["uv", "pip", "install", "--python", venv / "bin/python", f"{server}[api]"])
    probe = ("import importlib,json\n"
             "mods=['rya.runtime.engine','rya.api.app','rya.worker','rya.gates','rya.quotas']\n"
             "print(json.dumps([m for m in mods if importlib.import_module(m)]))")
    got = json.loads(h.run([venv / "bin/python", "-c", probe]).stdout.strip().splitlines()[-1])
    h.check("platform has the runtime", len(got) == 5, f"{len(got)}/5 modules")
    return venv


def phase_handoff(h: Harness, venv: Path, archive: Path, client_hash: str) -> Path:
    h.head("Handoff: the platform trusts bytes, not the client's word")
    dep = h.dir / "platform/deployment"
    dep.mkdir(parents=True, exist_ok=True)
    script = (
        "import json, shutil, sys, pathlib\n"
        "from rya import bundles\n"
        "archive = pathlib.Path(sys.argv[1]); dep = pathlib.Path(sys.argv[2]); h = sys.argv[3]\n"
        "out = {}\n"
        "try:\n"
        "    bundles.verify(archive, h); out['verified'] = True\n"
        "except Exception as e:\n"
        "    out['verified'] = False; out['err'] = str(e)\n"
        "try:\n"
        "    bundles.verify(archive, '0'*64); out['tamper_rejected'] = False\n"
        "except Exception as e:\n"
        "    out['tamper_rejected'] = True; out['tamper_code'] = getattr(e,'code','?')\n"
        "dest = bundles.bundle_archive_path(h, bundles.default_archive_root(dep))\n"
        "dest.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(archive, dest)\n"
        "bundles.unpack(archive, dep)\n"
        "out['files'] = sorted(str(p.relative_to(dep)) for p in dep.rglob('*')\n"
        "                      if p.is_file() and '.rya/' not in str(p.relative_to(dep)))\n"
        "print(json.dumps(out))"
    )
    got = json.loads(h.run([venv / "bin/python", "-c", script, archive, dep, client_hash]
                           ).stdout.strip().splitlines()[-1])
    h.check("server reproduces the client's digest", got["verified"],
            got.get("err", "")[:120] or "same hash from a different distribution")
    h.check("a wrong hash is rejected", got["tamper_rejected"],
            got.get("tamper_code", ""))
    h.check(".env was not bundled", ".env" not in got["files"],
            f"{len(got['files'])} files, secrets excluded")

    rb, _ = h.json_cmd([venv / "bin/rya", "bundle", "--json"], dep)
    h.check("hash round-trips through pack/unpack", rb.get("hash") == client_hash,
            f"{str(rb.get('hash'))[:12]}… == {client_hash[:12]}…")
    return dep


def phase_pipeline(h: Harness, venv: Path, dep: Path, client_hash: str) -> str:
    h.head("Promotion gate (§9): evidence bound to content")
    rya = venv / "bin/rya"
    rep, _ = h.json_cmd([rya, "deploy", "--check", "--json"], dep, check=False)
    h.check("readiness is green", rep.get("ready") is True,
            f"{rep.get('summary',{}).get('blocks','?')} blocks, "
            f"{rep.get('summary',{}).get('warnings','?')} warnings")

    g, _ = h.json_cmd([rya, "gate", "set", "--env", "prod", "--require-readiness",
                       "--require-evals", "--json"], dep)
    h.check("gate enforced for prod", g.get("gate", {}).get("enforced") is True,
            "requireReadiness + requireEvals")

    d, _ = h.json_cmd([rya, "deploy", "--env", "prod", "--no-promote", "--actor",
                       "ci@example.com", "--metadata", "gitSha=e2e0001", "--json"], dep)
    version = d.get("versionId", "")
    h.check("version recorded from the client's bundle",
            d.get("bundleHash") == client_hash and bool(version),
            f"{version} @ {str(d.get('bundleHash'))[:12]}…")
    h.check("recording does not promote", d.get("promoted") is False)

    blocked, code = h.json_cmd([rya, "promote", "--env", "prod", "--version", version,
                                "--json"], dep, check=False)
    err = blocked.get("error", {})
    h.check("promote BLOCKED without eval evidence",
            code == 7 and err.get("code") == "E_PROMOTION_BLOCKED",
            f"exit {code} {err.get('code','')}")

    ev, ec = h.json_cmd([rya, "eval", "--attest", "--version", version,
                         "--actor", "ci@example.com", "--json"], dep, check=False)
    h.check("eval suite passes and is attested", ec == 0 and ev.get("score") == 1.0,
            f"{ev.get('passed')}/{ev.get('total')} cases, score {ev.get('score')}")

    ok, oc = h.json_cmd([rya, "promote", "--env", "prod", "--version", version,
                         "--actor", "ci@example.com", "--json"], dep, check=False)
    h.check("promote ADMITTED with evidence",
            oc == 0 and ok.get("currentVersionId") == version,
            f"prod -> {ok.get('currentVersionId')}")

    # Evidence must not transfer: a second version has no attestations of its own.
    (dep / "src/agent.py").write_text(AGENT_PY.replace("Be concise.", "Be brief."))
    d2, _ = h.json_cmd([rya, "deploy", "--env", "prod", "--no-promote", "--json"], dep)
    b2, c2 = h.json_cmd([rya, "promote", "--env", "prod", "--version", d2.get("versionId", ""),
                         "--json"], dep, check=False)
    h.check("a different tree cannot ride the first version's evidence",
            c2 == 7 and b2.get("error", {}).get("code") == "E_PROMOTION_BLOCKED",
            f"new hash {str(d2.get('bundleHash'))[:12]}… blocked")
    (dep / "src/agent.py").write_text(AGENT_PY)   # restore the promoted content

    h.state["version"] = version
    return version


def phase_processes(h: Harness, venv: Path, dep: Path, version: str) -> None:
    h.head("Two processes: api executes nothing, worker executes everything")
    h.spawn("api", [venv / "bin/rya", "serve", "--host", "127.0.0.1", "--port", str(PORT)],
            dep, RYA_TOKEN=TOKEN, RYA_API_INLINE_WORKER="0")
    up = h.wait_for(lambda: h.http("GET", "/healthz")[0] == 200, timeout=45)
    if not h.check("api is up", bool(up), h.log_tail("api", 6) if not up else BASE):
        raise RuntimeError("api never came up")

    code, _ = h.http("POST", "/agents/refund-agent/turns", {"type": "refund.requested"},
                     token=None)
    h.check("unauthenticated write refused", code == 401, f"HTTP {code}")
    code, _ = h.http("POST", "/agents/refund-agent/turns", {"type": "refund.requested"},
                     token="wrong-token")
    h.check("bad token refused", code == 401, f"HTTP {code}")

    h.spawn("worker1", [venv / "bin/rya", "worker", "--env", "prod", "--interval", "1"],
            dep, RYA_TOKEN=TOKEN)
    workers = h.wait_for(lambda: (h.http("GET", "/workers")[1] or {}).get("workers"), timeout=45)
    w = (workers or [{}])[0]
    h.check("worker registered pinned to the promoted version",
            w.get("versionId") == version, f"{w.get('id')} -> {w.get('versionId')}")
    h.check("worker loaded the client's exact bundle",
            w.get("bundleHash") == h.state["hash"], f"{str(w.get('bundleHash'))[:12]}…")
    h.check("worker advertises the client's handlers",
            sorted((w.get("handlers") or {}).get("tools") or []) ==
            ["billing.lookup", "billing.refund"],
            str((w.get("handlers") or {}).get("tools")))
    h.check("cold start is fast", (w.get("coldStartMs") or 9e9) < 2000,
            f"{w.get('coldStartMs')}ms (target <2000)")


def phase_run(h: Harness, version: str) -> str:
    h.head("Run it: SDK-free HTTP caller -> queue -> worker")
    code, res = h.http("POST", "/agents/refund-agent/turns",
                       {"type": "refund.requested",
                        "payload": {"email": "ada@example.com", "amount": 42.5}})
    turn = res.get("turnId", "")
    h.check("turn accepted", code == 200 and bool(turn), f"{turn}")

    def find_run():
        runs = (h.http("GET", "/agents/refund-agent/runs")[1] or {}).get("runs") or []
        return next((r for r in runs if r.get("turnId") == turn
                     and r.get("status") == "waiting_approval"), None)

    run = h.wait_for(find_run, timeout=90)
    if run is None:
        h.check("run paused for a human", False, "worker log: " + h.log_tail("worker1", 4))
        raise RuntimeError("run never reached waiting_approval")
    h.check("run paused for a human", True, run["id"])
    run_id = run["id"]

    h.check("run is pinned to the promoted version", run.get("versionId") == version,
            f"{run.get('versionId')}")
    stats = ((h.http("GET", "/workers")[1] or {}).get("workers") or [{}])[0].get("stats", {})
    h.check("the WORKER claimed the turn, not the api", (stats.get("claimed") or 0) >= 1,
            f"claimed={stats.get('claimed')}")

    trace = (h.http("GET", f"/runs/{run_id}/trace")[1] or {}).get("trace") or []
    kinds = [e.get("kind") for e in trace]
    h.check("journal stops at the gate",
            kinds == ["run.started", "tool.call", "llm.respond", "approval.requested"],
            " -> ".join(kinds))

    apr = next((a for a in (h.http("GET", "/approvals?status=pending")[1] or {})
                .get("approvals") or [] if a.get("runId") == run_id), {})
    h.check("the gated action is the pending approval",
            (apr.get("action") or {}).get("tool") == "billing.refund",
            f"{apr.get('id')}: {(apr.get('action') or {}).get('input')}")
    h.state["approval"] = apr.get("id", "")
    h.state["run"] = run_id
    return run_id


def phase_durability(h: Harness, venv: Path, dep: Path, run_id: str) -> None:
    h.head("Durability: a different process finishes the run")
    h.kill("worker1")
    h.check("worker1 killed with SIGKILL (no graceful shutdown)", True,
            "the paused run now has no process anywhere")

    h.spawn("worker2", [venv / "bin/rya", "worker", "--env", "prod", "--interval", "1"],
            dep, RYA_TOKEN=TOKEN)
    started = h.wait_for(lambda: "serving" in h.log_tail("worker2", 5), timeout=45)
    h.check("a fresh worker process is serving", bool(started), h.log_tail("worker2", 2))

    code, res = h.http("POST", f"/approvals/{h.state['approval']}/approve", {})
    h.check("approval accepted", code == 200, f"HTTP {code} -> {res.get('runStatus')}")

    done = h.wait_for(lambda: (h.http("GET", f"/runs/{run_id}")[1] or {}).get("status")
                      == "completed", timeout=90)
    h.check("run completed after approval", bool(done))

    run = h.http("GET", f"/runs/{run_id}")[1] or {}
    trace = run.get("trace") or []
    kinds = [e.get("kind") for e in trace]
    for want in ("approval.action", "approval.approved", "channel.send", "run.completed"):
        h.check(f"journal has {want}", want in kinds)
    h.check("the journal grew rather than restarting",
            kinds[:4] == ["run.started", "tool.call", "llm.respond", "approval.requested"],
            f"{len(kinds)} steps, first 4 memoized on replay")

    # The resumed side-effect must reflect the approved action, not a re-run.
    tool_steps = [e for e in trace if e.get("kind") == "approval.action"]
    h.check("the approved refund executed exactly once", len(tool_steps) == 1,
            f"{len(tool_steps)} approval.action step(s)")


def phase_isolation(h: Harness, version: str) -> None:
    """Checks that document where execution actually happens. See FINDINGS."""
    h.head("Control-plane isolation (known gaps)")
    before = ((h.http("GET", "/workers")[1] or {}).get("workers") or [{}])
    claimed_before = sum((w.get("stats") or {}).get("claimed") or 0 for w in before)

    # Every worker down, inline worker disabled: nothing should execute.
    h.kill("worker2")
    code, res = h.http("POST", "/agents/refund-agent/events",
                       {"type": "refund.requested",
                        "payload": {"email": "isolation@example.com", "amount": 1.0}},
                       timeout=120)
    ran_inline = code == 200 and res.get("runId") and res.get("status") != "queued"
    h.check("POST /events does not execute in the api process", not ran_inline,
            f"HTTP {code} status={res.get('status')} — ran with 0 workers and "
            "RYA_API_INLINE_WORKER=0", gap=True)

    if res.get("runId"):
        run = h.http("GET", f"/runs/{res['runId']}")[1] or {}
        h.check("POST /events pins the run to the promoted version",
                run.get("versionId") == version,
                f"versionId={run.get('versionId')} — executed against the api's "
                "working tree, not the content-hashed bundle", gap=True)

    after = ((h.http("GET", "/workers")[1] or {}).get("workers") or [{}])
    claimed_after = sum((w.get("stats") or {}).get("claimed") or 0 for w in after)
    h.check("approval resume is claimed by a worker", claimed_after > claimed_before,
            f"claimed {claimed_before} -> {claimed_after}: the api resumed the run "
            "synchronously in phase_durability", gap=True)

    dead = [w for w in after if w.get("status") == "alive"]
    h.check("SIGKILLed workers are not reported alive", not dead,
            f"{len(dead)} killed worker(s) still 'alive' — lastHeartbeatAt is "
            "written but never read", gap=True)


def phase_quotas(h: Harness, venv: Path, dep: Path) -> None:
    h.head("Quotas: admission control, not mid-run killing")
    q, _ = h.json_cmd([venv / "bin/rya", "quotas", "set", "--max-runs-per-day", "1",
                       "--json"], dep)
    h.check("quota set", (q.get("quota") or {}).get("maxRunsPerDay") == 1,
            str(q.get("quota")))
    code, res = h.http("POST", "/agents/refund-agent/events",
                       {"type": "refund.requested",
                        "payload": {"email": "over@example.com", "amount": 1.0}},
                       timeout=120)
    err = (res.get("error") or res).get("code") if isinstance(res, dict) else None
    h.check("over-quota run refused with 429/E_QUOTA_EXCEEDED",
            code == 429 and err == "E_QUOTA_EXCEEDED", f"HTTP {code} {err}")
    h.run([venv / "bin/rya", "quotas", "clear", "--json"], dep, check=False)


# ===========================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true",
                    help="allow ambient provider keys (real model calls, costs money)")
    ap.add_argument("--keep", action="store_true", help="keep the working directory")
    ap.add_argument("--workdir", default=os.environ.get("RYA_E2E_DIR", ""),
                    help="where to build the two environments")
    args = ap.parse_args()

    workdir = Path(args.workdir).expanduser().resolve() if args.workdir else \
        Path(os.environ.get("TMPDIR", "/tmp")) / "rya-e2e"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)

    h = Harness(workdir, live=args.live)
    print(f"\033[1mRya end-to-end: SDK -> bundle -> gate -> platform\033[0m")
    print(f"  workdir : {workdir}")
    print(f"  mode    : {'LIVE (ambient keys kept)' if args.live else 'hermetic (offline mock model)'}")

    failed_hard = False
    try:
        sdk, server = phase_wheels(h)
        client_venv = phase_client(h, sdk)
        client_hash = phase_author(h, client_venv)[1]
        server_venv = phase_platform_env(h, server)
        dep = phase_handoff(h, server_venv, h.dir / "handoff/refund-agent.tar.gz", client_hash)
        version = phase_pipeline(h, server_venv, dep, client_hash)
        phase_processes(h, server_venv, dep, version)
        run_id = phase_run(h, version)
        phase_durability(h, server_venv, dep, run_id)
        phase_isolation(h, version)
        phase_quotas(h, server_venv, dep)
    except Exception as exc:                       # a phase blew up: report, don't hide
        failed_hard = True
        print(f"\n\033[31mABORTED in '{h.section}': {exc}\033[0m")
    finally:
        h.teardown()

    passes = [r for r in h.results if r[0] == "PASS"]
    fails = [r for r in h.results if r[0] == "FAIL"]
    gaps = [r for r in h.results if r[0] == "GAP"]
    print(f"\n\033[1m── Summary\033[0m")
    print(f"  \033[32m{len(passes)} passed\033[0m  "
          f"\033[31m{len(fails)} failed\033[0m  \033[33m{len(gaps)} known gaps\033[0m")
    for _, name, detail in fails:
        print(f"  \033[31mFAIL\033[0m {name}: {detail}")
    for _, name, detail in gaps:
        print(f"  \033[33mGAP \033[0m {name}: {detail}")

    if not args.keep:
        shutil.rmtree(workdir, ignore_errors=True)
    else:
        print(f"\n  kept: {workdir}")
    return 1 if (fails or failed_hard) else 0


if __name__ == "__main__":
    sys.exit(main())
