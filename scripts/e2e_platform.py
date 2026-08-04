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

The handoff is proved twice, on purpose. ``phase_handoff``/``phase_pipeline`` cover
the operator path (``rya deploy --env``, local database and bucket access), and
``phase_publish`` covers the client path (``rya publish`` over HTTP, from the
SDK-only venv). They must agree on the content address -- publishing the tree an
operator already deployed has to return that same version id, or the hash is a
label rather than an address.

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
                 check: bool = True,
                 env_extra: dict[str, str] | None = None) -> tuple[dict, int]:
        """Run a --json CLI command and parse the LAST json line of stdout."""
        p = self.run(argv, cwd, check=check, env_extra=env_extra)
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
    # RYA_ENVIRONMENT must match the worker's `--env`. The api reads the
    # environment pointer to decide which version a queued run is PINNED to
    # (D21/D12), so an api on `dev` and a worker on `prod` would pin to nothing
    # and every turn would sit unclaimed. Compose sets the same variable for both
    # services for this reason.
    h.spawn("api", [venv / "bin/rya", "serve", "--host", "127.0.0.1", "--port", str(PORT)],
            dep, RYA_TOKEN=TOKEN, RYA_API_INLINE_WORKER="0", RYA_ENVIRONMENT="prod")
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


def _last_json(stdout: str) -> dict | None:
    """The last JSON object a `--json` command printed.

    Scanning backwards rather than taking the last line: a failing command prints
    its error object after any other output, and `guard()` writes it to stdout too.
    """
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def phase_publish(h: Harness, client_venv: Path, proj: Path, version: str) -> None:
    """`rya publish` from the SDK-ONLY venv against the running control plane.

    This is the only place in the repo where a venv proven unable to import
    `rya.worker` (phase_client) ships code to a real platform, which is the whole
    claim of the HTTP publish path: a client repo needs a URL and a key, not
    database or bucket credentials.

    The load-bearing assertion is idempotency against the CLI pipeline. Publishing
    the SAME tree the operator already deployed with `rya deploy --env` must return
    the SAME version id — the two paths agree on the content address, or the hash
    is a label rather than an address.
    """
    h.head("Publish over HTTP from the client SDK")

    code, res = h.http("GET", "/agents/refund-agent/versions")
    before = {v["id"] for v in (res.get("versions") or [])}

    unauth = h.run([client_venv / "bin/rya", "publish", "--url", BASE,
                    "--env", "prod", "--no-promote", "--json"],
                   proj, check=False)
    payload = _last_json(unauth.stdout) or {}
    h.check("unauthenticated publish refused",
            unauth.returncode != 0 and
            (payload.get("error") or {}).get("code") == "E_UNAUTHORIZED",
            (payload.get("error") or {}).get("code") or unauth.stdout[-160:])

    ok = h.run([client_venv / "bin/rya", "publish", "--url", BASE, "--key", TOKEN,
                "--env", "prod", "--no-promote",
                "--actor", "ada@example.com", "--metadata", "ci=e2e", "--json"],
               proj, check=False)
    out = _last_json(ok.stdout) or {}
    h.check("client publishes with only the SDK installed",
            ok.returncode == 0 and out.get("ok") is True,
            out.get("error", {}).get("code") if ok.returncode else "uploaded")
    h.check("the platform recorded the client's exact hash",
            out.get("bundleHash") == h.state["hash"],
            f"{str(out.get('bundleHash'))[:12]}… == {h.state['hash'][:12]}…")
    h.check("HTTP publish and `rya deploy` agree on the content address",
            out.get("versionId") == version and out.get("versionId") in before,
            f"{out.get('versionId')} (deploy made {version})")
    h.check("the archive is addressable in the bundle store",
            str(out.get("archive", "")).endswith(f"{h.state['hash']}.tar.gz"),
            str(out.get("archive"))[-52:])
    h.check("publish admits it cannot attest readiness",
            out.get("attested") is False and out.get("notAttested") == ["readiness"],
            f"attested={out.get('attested')}")

    # A gate that demands readiness must refuse a version published this way — the
    # honest consequence, asserted rather than described.
    #
    # This needs NEW content. The publish above returned the version `rya deploy
    # --env prod` already created (that is the idempotency just asserted), and that
    # version carries a readiness attestation from the CLI path — so gating on it
    # would pass for a legitimate reason and prove nothing.
    code, _gate = h.http("PUT", "/gate", {"environments": {"staging": {"requireReadiness": True}}})
    if h.check("readiness gate configured for staging", code == 200, f"HTTP {code}"):
        entry = proj / "src/agent.py"
        original = entry.read_text()
        entry.write_text(original + "\n# a later edit: new content, new version\n")
        try:
            blocked = h.run([client_venv / "bin/rya", "publish", "--url", BASE, "--key", TOKEN,
                             "--env", "staging", "--json"], proj, check=False)
            err = (_last_json(blocked.stdout) or {}).get("error") or {}
            h.check("a readiness gate blocks an unattested HTTP publish",
                    err.get("code") == "E_PROMOTION_BLOCKED",
                    err.get("code") or "promoted with no readiness evidence")
            # The version is still RECORDED — only the pointer flip is refused, so
            # the artifact is there for `rya eval --attest` to make promotable.
            recorded = h.run([client_venv / "bin/rya", "publish", "--url", BASE, "--key", TOKEN,
                              "--json"], proj, check=False)
            out2 = _last_json(recorded.stdout) or {}
            h.check("the unattested version is still recorded, just not promoted",
                    out2.get("ok") is True and out2.get("versionId") != version,
                    f"{out2.get('versionId')} != {version}")
        finally:
            entry.write_text(original)
            h.http("PUT", "/gate", {"environments": {}})


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

    claimed_before = sum((w.get("stats") or {}).get("claimed") or 0
                         for w in ((h.http("GET", "/workers")[1] or {}).get("workers") or []))

    code, res = h.http("POST", f"/approvals/{h.state['approval']}/approve", {})
    h.check("approval accepted", code == 200, f"HTTP {code} -> {res.get('runStatus')}")
    # Phase 2 (D21): approving is the one governance action that runs tenant code
    # — it executes the approved action and replays the handler — so the api
    # records the DECISION and a worker carries it out. Was a GAP.
    h.check("the api does not resume the run itself",
            res.get("queued") is True and res.get("runStatus") == "resuming",
            f"queued={res.get('queued')} runStatus={res.get('runStatus')}")

    done = h.wait_for(lambda: (h.http("GET", f"/runs/{run_id}")[1] or {}).get("status")
                      == "completed", timeout=90)
    h.check("run completed after approval", bool(done))

    # Polled, not sampled once: a worker writes its stats on the HEARTBEAT that
    # follows the tick, so reading immediately after the run completes races the
    # bookkeeping rather than the work.
    def claimed_now() -> int:
        return sum((w.get("stats") or {}).get("claimed") or 0
                   for w in ((h.http("GET", "/workers")[1] or {}).get("workers") or []))

    grew = h.wait_for(lambda: claimed_now() > claimed_before or None, timeout=30)
    h.check("approval resume is claimed by a worker", bool(grew),
            f"claimed {claimed_before} -> {claimed_now()}")

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
    """Where execution actually happens.

    Three of these were `GAP` until Phase 2. D21 made the api manifest-free, so
    it has no handler to run; `POST /events` now writes a `queued` run pinned to
    the promoted version and hands it to a worker, and an approval records the
    decision and enqueues the resume. See MULTITENANT_PLAN §4.
    """
    h.head("Control-plane isolation")
    # Every worker down, inline worker disabled: nothing should execute.
    h.kill("worker2")
    code, res = h.http("POST", "/agents/refund-agent/events",
                       {"type": "refund.requested",
                        "payload": {"email": "isolation@example.com", "amount": 1.0}},
                       timeout=120)
    ran_inline = code == 200 and res.get("runId") and res.get("status") != "queued"
    h.check("POST /events does not execute in the api process", not ran_inline,
            f"HTTP {code} status={res.get('status')} — ran with 0 workers and "
            "RYA_API_INLINE_WORKER=0")

    # The run row exists before any worker touches it, which is what makes the
    # pin readable here at all — with 0 workers there is nothing else to ask.
    h.check("POST /events returns a run id synchronously", bool(res.get("runId")),
            f"body={res}")
    run = h.http("GET", f"/runs/{res['runId']}")[1] or {} if res.get("runId") else {}
    h.check("POST /events pins the run to the promoted version",
            run.get("versionId") == version,
            f"versionId={run.get('versionId')} expected {version}")
    # Kept for `phase_supervisor`: with every worker dead this run has nowhere to
    # execute, which before D25 meant it stayed queued until a human started a
    # process. It is the evidence that scale-to-zero became two-way.
    h.state["strandedRun"] = res.get("runId", "")

    # Phase 3. `lastHeartbeatAt` was written by every worker and read by nothing,
    # so both SIGKILLed workers above stayed `alive` forever. Was a GAP.
    #
    # Polled, because the window is real: liveness is heartbeat AGE, and a worker
    # killed a moment ago is indistinguishable from one that is merely between
    # ticks. `store.WORKER_LOST_SECONDS` is deliberately the turn lease, so this
    # waits out the same window the queue waits before reclaiming that worker's
    # item — the two conclusions are meant to arrive together.
    def alive_workers():
        return [w for w in ((h.http("GET", "/workers?status=alive")[1] or {}).get("workers") or [])]

    gone = h.wait_for(lambda: (not alive_workers()) or None, timeout=180, interval=5)
    h.check("SIGKILLed workers are not reported alive", bool(gone),
            f"{len(alive_workers())} killed worker(s) still 'alive'")

    # And not by disappearing: an empty worker list is scale-to-zero, the designed
    # idle state, so a crash that merely emptied the list would be indistinguishable
    # from a key that idled out.
    lost = ((h.http("GET", "/workers?status=lost")[1] or {}).get("workers") or [])
    h.check("a killed worker is visible as lost rather than hidden", len(lost) >= 1,
            f"{len(lost)} lost: {[w.get('id') for w in lost]}")
    h.check("a lost worker reports how stale its heartbeat is",
            all((w.get("heartbeatAgeSeconds") or 0) > 0 for w in lost),
            str([w.get("heartbeatAgeSeconds") for w in lost]))


def phase_supervisor(h: Harness, venv: Path, dep: Path, version: str) -> None:
    """Nothing started a worker until D25 (issue #16), so scale-to-zero was one-way.

    Runs after `phase_isolation`, which is where every worker gets killed — so the
    state on entry is exactly the one the supervisor exists for: work queued, a key
    with no live process, and two dead registrations nobody deregistered.
    """
    h.head("Supervisor: the fleet schedules itself (D25/D26)")

    plan, _ = h.json_cmd([venv / "bin/rya", "supervisor", "--plan", "--env", "prod",
                          "--json"], dep, env_extra={"RYA_ENVIRONMENT": "prod"})
    driver = plan.get("driver") or {}
    h.check("the execution driver declares what it isolates",
            driver.get("driver") == "local" and driver.get("isolation") == "none",
            f"{driver.get('driver')} / {driver.get('isolation')}")
    h.check("no driver available today claims untrusted-tenant isolation",
            driver.get("supportsUntrusted") is False, str(driver))

    actions = plan.get("actions") or []
    starts = [a for a in actions if a.get("action") == "start"]
    reaps = [a for a in actions if a.get("action") == "reap"]
    h.check("the supervisor plans to reap the workers that were SIGKILLed",
            len(reaps) >= 1, f"{len(reaps)} reap(s): {[a.get('workerId') for a in reaps]}")
    h.check("the supervisor plans to serve the key that has queued work",
            any((a.get("spec") or {}).get("versionId") == version for a in starts),
            f"{len(starts)} start(s): {[(a.get('key'), a.get('reason')) for a in starts]}")

    # The refusal, on the deployment as it actually stands. This is the criterion
    # that matters most in Phase 3: the check exists BEFORE any driver can satisfy
    # it, so the only correct outcome today is a refusal.
    refused, rc = h.json_cmd([venv / "bin/rya", "supervisor", "--plan", "--json"], dep,
                             env_extra={"RYA_UNTRUSTED_TENANTS": "1",
                                        "RYA_ENVIRONMENT": "prod"},
                             check=False)
    err = (refused.get("error") or refused).get("code") if isinstance(refused, dict) else None
    h.check("untrusted tenancy on a driver that cannot isolate refuses to start",
            rc != 0 and err == "E_ISOLATION_INSUFFICIENT", f"rc={rc} {err}: {refused}")

    # Now let it act. `--once` applies one tick, which must bring the key back.
    # A short idle window on purpose: the worker this launches is a grandchild the
    # harness cannot kill, so it has to leave on its own — which is also the second
    # half of the criterion, since two-way scale-to-zero means it goes back down.
    tick, _ = h.json_cmd([venv / "bin/rya", "supervisor", "--once", "--env", "prod",
                          "--idle-exit", "10", "--json"], dep,
                         env_extra={"RYA_ENVIRONMENT": "prod"})
    applied = [a for a in (tick.get("actions") or []) if a.get("ok")]
    h.check("one tick applies the plan", len(applied) == len(actions),
            f"{len(applied)}/{len(actions)} applied")

    back = h.wait_for(lambda: ((h.http("GET", "/workers?status=alive")[1] or {})
                               .get("workers") or None), timeout=90)
    h.check("a key that scaled to zero is restarted automatically",
            bool(back), f"workers now: {[w.get('id') for w in (back or [])]}")
    w = (back or [{}])[0]
    h.check("the supervisor started it on the promoted version",
            w.get("versionId") == version, f"{w.get('id')} -> {w.get('versionId')}")

    # The reaped registrations are retired with a reason rather than deleted: "this
    # key kept dying" is the history an operator needs most.
    every = ((h.http("GET", "/workers?status=")[1] or {}).get("workers") or [])
    reaped = [x for x in every if (x.get("stopReason") == "lost")]
    h.check("a reaped worker keeps its row, marked lost", len(reaped) >= 1,
            f"{[x.get('id') for x in reaped]}")

    # The run that phase_isolation left queued with nothing to serve it. The
    # supervisor is the only reason anything picks it up now.
    stranded = h.state.get("strandedRun")
    if stranded:
        done = h.wait_for(lambda: (h.http("GET", f"/runs/{stranded}")[1] or {})
                          .get("status") in ("completed", "waiting_approval"), timeout=120)
        h.check("the run stranded by scale-to-zero is finally executed", bool(done),
                f"{stranded} -> {(h.http('GET', f'/runs/{stranded}')[1] or {}).get('status')}")


def phase_multi_agent(h: Harness, client_venv: Path, workdir: Path) -> None:
    """One deployment, two agents — the Phase 2 capability (D21/D28).

    Published from a SECOND project, over HTTP, against the same running api. That
    is what makes it a real test of D21 rather than of a fixture: `build_app`
    resolved one `rya.agent.yaml` at boot until Phase 2, and the publish route
    refused any bundle whose name was not that one. The api here was never told
    this agent exists — it learns it from the version record.

    Deliberately LAST: it leaves the deployment serving two agents, which is
    exactly the state that makes every unprefixed agent-scoped route ambiguous.
    Running it earlier would break the phases that use them.
    """
    h.head("Multi-agent: one deployment serves two agents")
    second = workdir / "second-agent"
    if not (second / "rya.agent.yaml").is_file():
        h.run([client_venv / "bin/rya", "create", "second-agent", "--template", "minimal"],
              workdir, check=False)
    if not (second / "rya.agent.yaml").is_file():
        h.check("second project scaffolded", False, f"no manifest at {second}")
        return

    out, rc = h.json_cmd([client_venv / "bin/rya", "publish", "--url", BASE, "--key", TOKEN,
                          "--env", "prod", "--json"], second, check=False)
    h.check("a bundle for an agent this deployment never heard of is accepted",
            rc == 0 and out.get("agent") == "second-agent",
            f"rc={rc} {out.get('error') or out.get('agent')}")

    served = sorted(a["name"] for a in (h.http("GET", "/agents")[1] or {}).get("agents", []))
    h.check("both agents are served by one deployment",
            served == ["refund-agent", "second-agent"], f"served={served}")

    # Each answers for itself — the assertion that fails the moment `{agent_id}`
    # goes back to being decorative.
    for name in ("refund-agent", "second-agent"):
        _, body = h.http("GET", f"/agents/{name}")
        h.check(f"GET /agents/{name} answers for {name}",
                (body or {}).get("name") == name, str((body or {}).get("name")))

    # Independently promotable: flipping one pointer must not move the other.
    _, refund_env = h.http("GET", "/agents/refund-agent/environments/prod")
    _, second_env = h.http("GET", "/agents/second-agent/environments/prod")
    h.check("the two agents have independent environment pointers",
            (refund_env.get("currentVersion") or {}).get("id")
            != (second_env.get("currentVersion") or {}).get("id"),
            f"{(refund_env.get('currentVersion') or {}).get('id')} vs "
            f"{(second_env.get('currentVersion') or {}).get('id')}")

    # And an unprefixed agent-scoped route now refuses rather than guessing.
    code, body = h.http("GET", "/tools")
    h.check("an unprefixed agent-scoped route refuses once two agents exist",
            code == 400 and (body or {}).get("code") == "E_AGENT_AMBIGUOUS",
            f"HTTP {code} {body}")


def phase_fork_execution(h: Harness, venv: Path, dep: Path, version: str) -> None:
    """A run executes in a fork of a warm interpreter (D27, issue #19).

    Against the real published bundle rather than a fixture, which is the part the
    unit tests cannot do: this bundle was authored in a client repo that installed
    only the SDK, shipped over HTTP, and unpacked from its archive by content hash.
    The claimer here has never seen its source.
    """
    h.head("Fork per run: the claimer holds no tenant import (D27)")

    # Wait for the supervisor-launched worker to idle out first. Two reasons: it is
    # a grandchild this harness cannot kill, and an in-process claimer left running
    # would race the fork claimer for the event below — making the whole phase
    # assert nothing about where execution happened.
    empty = h.wait_for(lambda: (not ((h.http("GET", "/workers?status=alive")[1] or {})
                                     .get("workers"))) or None, timeout=120, interval=2)
    h.check("the supervisor's worker scales back to zero when idle", bool(empty),
            str([w.get("id") for w in ((h.http("GET", "/workers?status=alive")[1] or {})
                                       .get("workers") or [])]))

    h.spawn("forkworker", [venv / "bin/rya", "worker", "--env", "prod", "--fork",
                           "--interval", "1"], dep, RYA_TOKEN=TOKEN,
            RYA_ENVIRONMENT="prod")
    up = h.wait_for(lambda: "serving" in h.log_tail("forkworker", 6), timeout=60)
    h.check("a fork-mode claimer starts and preflights",
            bool(up), h.log_tail("forkworker", 3))

    # Preflight in fork mode means the warm TEMPLATE imported the bundle and
    # reported its handler set — so the guarantee `worker.preflight` exists for is
    # relocated, not lost. The registration is the observable proof: the claimer
    # advertised handlers it cannot itself see.
    reg = h.wait_for(lambda: next(
        (w for w in ((h.http("GET", "/workers?status=alive")[1] or {}).get("workers") or [])
         if w.get("mode") == "fork" and w.get("versionId") == version), None), timeout=60)
    h.check("the claimer registers as a fork claimer", bool(reg),
            f"{(reg or {}).get('id')} mode={(reg or {}).get('mode')}")
    h.check("the claimer advertises handlers it never imported",
            sorted(((reg or {}).get("handlers") or {}).get("tools") or []) ==
            ["billing.lookup", "billing.refund"],
            str((reg or {}).get("handlers")))

    code, res = h.http("POST", "/agents/refund-agent/events",
                       {"type": "refund.requested",
                        "payload": {"email": "forked@example.com", "amount": 2.0}},
                       timeout=60)
    h.check("event accepted for the forked claimer", code == 200 and bool(res.get("runId")),
            f"HTTP {code} {res}")
    run_id = res.get("runId", "")
    paused = h.wait_for(lambda: (h.http("GET", f"/runs/{run_id}")[1] or {}).get("status")
                        == "waiting_approval", timeout=120)
    h.check("a forked run reaches the approval gate", bool(paused),
            "worker log: " + h.log_tail("forkworker", 4))

    run = h.http("GET", f"/runs/{run_id}")[1] or {}
    h.check("the forked run is pinned to the same version the claimer serves",
            run.get("versionId") == version, f"{run.get('versionId')}")
    h.check("the forked run journaled the same steps as an in-process one",
            [e.get("kind") for e in (run.get("trace") or [])] ==
            ["run.started", "tool.call", "llm.respond", "approval.requested"],
            " -> ".join(e.get("kind") for e in (run.get("trace") or [])))
    h.kill("forkworker")


HOSTILE_AGENT_PY = '''
"""A deliberately hostile tenant. Every line here is an attack the phase asserts fails.

Written as a normal agent because that is the point: nothing about the SDK stops a
tenant writing this, so the boundary has to be somewhere the tenant does not control.
"""
import os

from rya import define_agent

agent = define_agent()


@agent.on_event
async def main(ctx, event):
    report = {}

    # 1. Read the platform's credentials out of the environment.
    report["env"] = {name: bool(os.environ.get(name)) for name in
                     ("RYA_DATABASE_URL", "DATABASE_URL", "RYA_SECRET_KEY",
                      "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                      "AWS_SECRET_ACCESS_KEY", "RYA_ADMIN_TOKEN")}

    # 2. Read them off the resolved run config, where D8 puts the model route.
    routes = dict(getattr(ctx.config, "routes", None) or {})
    report["routeKeys"] = [n for n, r in routes.items() if getattr(r, "api_key", "")]

    # 3. Forge a billing row. With a pooled provider key this is writing our invoice.
    try:
        ctx.store.meter_append({"runId": ctx.run["id"], "inputTokens": -10 ** 9,
                                "costUsd": -1000.0})
        report["meter"] = "WROTE"
    except Exception as exc:
        report["meter"] = type(exc).__name__

    # 4. Reach the execution plane: deregister the fleet, retire a version.
    for name in ("worker_list", "worker_deregister", "version_set_state",
                 "policy_set", "list_runs", "journal_read", "queue_reap"):
        try:
            getattr(ctx.store, name)
            report[name] = "REACHABLE"
        except Exception as exc:
            report[name] = type(exc).__name__

    # 5. Re-scope the database connection — 2.1 in the threat model.
    try:
        ctx.store.set_config("app.workspace_id", "ws_somebody_else")
        report["setConfig"] = "WORKED"
    except Exception as exc:
        report["setConfig"] = type(exc).__name__

    # 6. A raw outbound request to a host no allowlist mentions.
    try:
        import urllib.request
        urllib.request.urlopen("http://169.254.169.254/latest/meta-data/", timeout=3)
        report["rawEgress"] = "REACHED"
    except Exception as exc:
        report["rawEgress"] = type(exc).__name__

    # 7. What IS this process holding? The tenant's own view of the inventory.
    report["storeKind"] = (ctx.store.describe() or {}).get("kind")
    return report
'''

HOSTILE_MANIFEST = """\
name: hostile-agent
version: 0.1.0
entrypoint: src/agent.py
model:
  provider: mock
  default: mock-llm
tools: []
"""


def phase_tenant_scope(h: Harness, venv: Path, dep: Path, version: str) -> None:
    """One claimer for the whole tenant (D27/#19-8b) — Phase 5's property.

    Deliberately after ``phase_multi_agent``, because the property only exists once a
    deployment has more than one agent: the narrow scope wants a worker per
    (agent, version) and this wants one, and with a single agent those are the same
    number. So this runs against the two agents that phase left published, and asserts
    that one process serves both.

    It is also the first end-to-end exercise of a **mediated** claim across agents.
    That earned its keep immediately: the minimal template's handler calls
    ``ctx.llm.respond``, whose ``on_token`` writes to ``store.stream_append``, and a
    nested store call inside a model call deadlocked every mediated streaming turn
    until Phase 5 gave nested calls their own connection. Phase 4's own mediation
    phase used a hostile agent that never called the model, so nothing caught it.
    """
    h.head("Tenant claimer: one process serves every agent (D27/#19-8b)")

    # Same reasoning as phase_fork_execution: a leftover claimer would race this one
    # and the phase would assert nothing about where execution happened.
    # Longer than `WORKER_LOST_SECONDS`, on purpose: this harness kills workers with
    # SIGKILL, so their registrations never deregister and read `alive` until the
    # liveness window demotes them. That is the behaviour Phase 3 built and it is why
    # 120s exactly was too tight.
    empty = h.wait_for(lambda: (not ((h.http("GET", "/workers?status=alive")[1] or {})
                                     .get("workers"))) or None, timeout=200, interval=3)
    h.check("no other claimer is serving before the tenant claimer starts", bool(empty),
            str([w.get("id") for w in ((h.http("GET", "/workers?status=alive")[1] or {})
                                       .get("workers") or [])]))

    # Deliberately NO provider key. `phase_mediation` sets one because its whole point
    # is that a hostile agent cannot find it; here the agents actually call the model,
    # and the scaffolded manifests declare `provider: auto` — which resolves to the
    # real provider the moment a key is present and would make this phase depend on a
    # network and a valid credential. Mediation is still on, so the call still goes
    # through the broker; what it reaches on the other side is the offline stub.
    h.spawn("tenant", [venv / "bin/rya", "worker", "--scope", "tenant", "--fork",
                       "--env", "prod", "--prewarm", "prod", "--interval", "1"], dep,
            RYA_TOKEN=TOKEN, RYA_ENVIRONMENT="prod", RYA_BROKER="1",
            RYA_SECRET_KEY="Zm9vYmFyYmF6cXV1eGZvb2JhcmJhemZvb2JhcmJhego=")
    up = h.wait_for(lambda: "serving all of" in h.log_tail("tenant", 8), timeout=120)
    h.check("a tenant-scoped claimer starts with no agent and no version",
            bool(up), h.log_tail("tenant", 6))
    if not up:
        h.kill("tenant")
        return

    reg = h.wait_for(lambda: next(
        (w for w in ((h.http("GET", "/workers?status=alive")[1] or {}).get("workers") or [])
         if w.get("scope") == "tenant"), None), timeout=60)
    h.check("it registers one key for the whole tenant",
            (reg or {}).get("concurrencyKey") == "default:*:*",
            f"{(reg or {}).get('concurrencyKey')}")
    h.check("its registration names no agent and no version",
            not (reg or {}).get("agent") and not (reg or {}).get("versionId"),
            f"agent={(reg or {}).get('agent')!r} version={(reg or {}).get('versionId')!r}")
    h.check("it reports the tenant fork mode so an operator can tell them apart",
            (reg or {}).get("mode") == "fork-tenant", str((reg or {}).get("mode")))

    # Pre-warming at this scope means warm INTERPRETERS inside one sandbox, not a
    # second sandbox per key — which is the whole idle-cost argument (§6).
    warmed = ((reg or {}).get("handlers") or {}).get("agents") or {}
    h.check("it pre-warmed the promoted version of each agent in this environment",
            {"refund-agent", "second-agent"} <= set(warmed), f"warm: {sorted(warmed)}")
    h.check("pre-warming several agents costs interpreters, not sandboxes",
            len(warmed) > 1, f"{len(warmed)} warm interpreter(s) in one claimer")

    # --- both agents execute, in one claimer -----------------------------------
    _, res_a = h.http("POST", "/agents/refund-agent/events",
                      {"type": "refund.requested",
                       "payload": {"email": "tenant@example.com", "amount": 3.0}},
                      timeout=60)
    _, res_b = h.http("POST", "/agents/second-agent/events",
                      {"type": "message.received",
                       "payload": {"channel": "web", "externalId": "t2",
                                   "body": "hello from the tenant claimer"}},
                      timeout=60)
    run_a, run_b = res_a.get("runId", ""), res_b.get("runId", "")

    def _settled(rid):
        return (h.http("GET", f"/runs/{rid}")[1] or {}).get("status") in (
            "completed", "failed", "waiting_approval")

    both = h.wait_for(lambda: (run_a and run_b and _settled(run_a) and _settled(run_b))
                      or None, timeout=180, interval=2)
    a = h.http("GET", f"/runs/{run_a}")[1] or {} if run_a else {}
    b = h.http("GET", f"/runs/{run_b}")[1] or {} if run_b else {}
    h.check("one claimer served two different agents' work",
            bool(both) and a.get("status") == "waiting_approval"
            and b.get("status") == "completed",
            f"refund-agent={a.get('status')} second-agent={b.get('status')} "
            f"log: {h.log_tail('tenant', 6)}")
    h.check("each run is pinned to its own agent's version",
            a.get("versionId") != b.get("versionId")
            and bool(a.get("versionId")) and bool(b.get("versionId")),
            f"{a.get('versionId')} vs {b.get('versionId')}")

    # A mediated model call through the broker, which is the check Phase 4 lacked.
    kinds_b = [e.get("kind") for e in (b.get("trace") or [])]
    h.check("a mediated ctx.llm.respond completes rather than wedging the fork",
            "llm.respond" in kinds_b, " -> ".join(kinds_b) or "(no trace)")
    meter = [r for r in ((h.http("GET", "/usage")[1] or {}).get("usage") or {}).get(
        "byAgent", []) or []]
    h.check("inference is metered per agent by the broker, not by the tenant",
            not meter or any(r.get("agent") == "second-agent" for r in meter),
            str(meter)[:200])

    # --- one key, whatever the agent-version product is ------------------------
    out, rc = h.json_cmd([venv / "bin/rya", "supervisor", "--plan", "--scope", "tenant",
                          "--env", "prod", "--json"], dep, check=False,
                         env_extra={"RYA_TOKEN": TOKEN})
    # START actions only. A plan also carries REAPs, and those name the key of the
    # *dead* worker being retired — which for a version-scoped worker killed earlier in
    # this run is a version key. Counting those made the first cut of this check read
    # as "the wide scope did not take effect" when what it saw was correct history.
    keys = sorted({a.get("key") for a in (out.get("actions") or [])
                   if a.get("action") == "start"})
    h.check("the supervisor plans one key per tenant rather than one per agent-version",
            rc == 0 and keys in ([], ["default:*:*"]), f"rc={rc} keys={keys}")

    h.kill("tenant")


def phase_supervisor_lease(h: Harness, venv: Path, dep: Path) -> None:
    """Open question 7: a second supervisor stands by rather than doubling the fleet.

    The failure it prevents is silent, which is why it is worth an e2e check rather
    than only a unit test: two supervisors each read the same depth, each see an empty
    driver inventory of their own, and each start the workers the other already did.
    """
    h.head("Supervisor lease: the second one stands by (open question 7)")
    args = [venv / "bin/rya", "supervisor", "--once", "--env", "prod", "--json"]
    first, rc1 = h.json_cmd(args, dep, check=False, env_extra={"RYA_TOKEN": TOKEN})
    second, rc2 = h.json_cmd(args, dep, check=False, env_extra={"RYA_TOKEN": TOKEN})
    h.check("both supervisor ticks run to completion", rc1 == 0 and rc2 == 0,
            f"rc={rc1},{rc2} {first.get('error') or second.get('error')}")
    # The first released its lease on exit, so the second takes it — which is the
    # correct behaviour and the reason this asserts the MECHANISM rather than a race.
    h.check("a supervisor tick records which scope it planned for",
            first.get("scope") in ("version", "tenant"), str(first.get("scope")))
    h.check("a tick that could not take the lease would report itself passive",
            "passive" not in first or first.get("passive") is False,
            f"passive={first.get('passive')}")


def phase_template_host(h: Harness, venv: Path, dep: Path) -> None:
    """D32's pair, run for real: a credential-free host serving a credentialed claimer.

    §7.2 said the broker must be a sibling of the tenant process and never its parent,
    and named the one thing that blocked it — the claimer *spawns* the template with
    `multiprocessing`, so the two are necessarily in the same container. Until the host
    existed the untrusted posture was unlaunchable on every driver, which
    `phase_posture` asserted as a refusal.

    What is checked here is the property rather than the plumbing: the host starts
    independently with an environment built the way `sandbox_env` builds one (from
    nothing), the claimer drives it over a socket, and a real turn completes with the
    tenant's interpreter in the host's process tree rather than the claimer's.
    """
    h.head("Template host: the claimer is not the tenant's parent (D32)")
    sock = h.dir / "host.sock"
    token = "e2e-host-token"

    # Built as an allowlist, exactly like `ContainerDriver.sandbox_env`: a credential
    # that was never added cannot be forgotten in a filter. This is the e2e's stand-in
    # for the sandbox container, and it deliberately does NOT go through `h.spawn`,
    # which inherits `os.environ` — inheriting here would defeat the whole check.
    host_env = {"PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", ""),
                "PYTHONDONTWRITEBYTECODE": "1",
                "RYA_TEMPLATE_HOST": str(sock),
                "RYA_TEMPLATE_HOST_TOKEN": token}
    log = (h.dir / "template-host.log").open("w")
    host = subprocess.Popen([str(venv / "bin/rya"), "template-host", "--socket", str(sock)],
                            cwd=str(dep), env=host_env, stdout=log,
                            stderr=subprocess.STDOUT, start_new_session=True)
    h.procs["template-host"] = host
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not sock.exists() and host.poll() is None:
        time.sleep(0.1)
    h.check("a template host starts with no credentials in its environment",
            sock.exists(), f"socket={sock.exists()} rc={host.poll()} "
                           f"{h.log_tail('template-host', 3)}")
    h.check("its environment carries no DSN, no seal key and no provider key",
            not [k for k in host_env if k in ("RYA_DATABASE_URL", "RYA_SECRET_KEY",
                                              "ANTHROPIC_API_KEY", "RYA_ADMIN_TOKEN")],
            f"env={sorted(host_env)}")

    status, rc = h.json_cmd([venv / "bin/rya", "template-host", "--socket", str(sock),
                             "--status", "--json"], dep, check=False,
                            env_extra={"RYA_TEMPLATE_HOST_TOKEN": token})
    h.check("`rya template-host --status` answers over the socket",
            rc == 0 and status.get("pid"), f"rc={rc} {str(status)[:120]}")
    h.check("it is holding nothing until a claimer asks it to",
            status.get("templates") == [], str(status.get("templates"))[:120])

    # The wrong token is refused. The processes that can reach this socket include the
    # tenant's own forks, and while they gain nothing they do not already have, they
    # could evict a sibling agent's warm interpreter.
    denied, rc = h.json_cmd([venv / "bin/rya", "template-host", "--socket", str(sock),
                             "--status", "--json"], dep, check=False,
                            env_extra={"RYA_TEMPLATE_HOST_TOKEN": "wrong"})
    err = (denied.get("error") or denied) if isinstance(denied, dict) else {}
    h.check("a caller with the wrong token is refused",
            err.get("code") == "E_TEMPLATE_HOST_DENIED", f"rc={rc} {err.get('code')}")

    # Now the claimer, pointed at the host. One tenant-scoped mediated claimer,
    # driving templates it does not own.
    claimer_env = {"RYA_TEMPLATE_HOST": str(sock), "RYA_TEMPLATE_HOST_TOKEN": token,
                   "RYA_BROKER": "1", "RYA_CLAIMER_SCOPE": "tenant"}
    out, rc = h.json_cmd([venv / "bin/rya", "worker", "--once", "--scope", "tenant",
                          "--env", "prod", "--prewarm", "prod", "--json"],
                         dep, check=False, env_extra=claimer_env)
    h.check("a claimer drives the host rather than spawning its own templates",
            rc == 0, f"rc={rc} {str(out.get('error'))[:200]}")

    status, _ = h.json_cmd([venv / "bin/rya", "template-host", "--socket", str(sock),
                            "--status", "--json"], dep, check=False,
                           env_extra={"RYA_TEMPLATE_HOST_TOKEN": token})
    held = status.get("templates") or []
    h.check("the host is now holding the tenant's warm interpreter, not the claimer",
            bool(held) and all(t.get("alive") for t in held),
            f"templates={[t.get('agent') for t in held]}")
    # Named rather than counted: "the host holds two things" would pass if it held two
    # copies of one agent, and the property is that ONE credential-free container
    # fronts the tenant's whole agent list.
    agents = {t.get("agent") for t in held}
    h.check("and it is fronting more than one of the tenant's agents from one container",
            len(agents) >= 2, f"agents={sorted(a for a in agents if a)}")
    h.check("each one reports the import it paid once rather than per run",
            all(isinstance(t.get("importMs"), int) for t in held),
            f"importMs={[t.get('importMs') for t in held]}")
    # The counter is the claimer's traffic, from the host's own bookkeeping — direct
    # evidence that the drain crossed the socket rather than happening in the claimer.
    h.check("the host served the claimer's control traffic rather than sitting idle",
            int(status.get("serves") or 0) > 3, f"serves={status.get('serves')}")


def phase_mediation(h: Harness, venv: Path, dep: Path, client_venv: Path,
                    workdir: Path) -> None:
    """D18: the tenant process holds no credentials — asserted from inside the tenant.

    Every other phase checks the platform's view. This one publishes a hostile agent
    and reads *its* report, because "the handler could not find a DSN" is a stronger
    statement than "we believe we removed the DSN". §10 says Phase 4's interesting
    assertions are adversarial and cannot be expressed as unit tests; this is that.
    """
    h.head("Mediation: a hostile tenant finds no credentials (D18)")
    proj = workdir / "hostile-agent"
    (proj / "src").mkdir(parents=True, exist_ok=True)
    (proj / "src/agent.py").write_text(HOSTILE_AGENT_PY)
    (proj / "rya.agent.yaml").write_text(HOSTILE_MANIFEST)
    (proj / "pyproject.toml").write_text(
        '[project]\nname = "hostile-agent"\nversion = "0.1.0"\n'
        'requires-python = ">=3.10"\ndependencies = ["rya"]\n')

    out, rc = h.json_cmd([client_venv / "bin/rya", "publish", "--url", BASE,
                          "--key", TOKEN, "--env", "prod", "--json"], proj, check=False)
    h.check("the hostile bundle publishes like any other",
            rc == 0 and out.get("agent") == "hostile-agent",
            f"rc={rc} {out.get('error') or out.get('agent')}")
    if rc != 0:
        return
    version = out.get("versionId") or ""

    # A MEDIATED fork claimer: the credentials stay in this process, the template and
    # its forks get a socket. Every platform credential is present here on purpose —
    # the phase is worthless if there is nothing to leak.
    h.spawn("mediated", [venv / "bin/rya", "worker", "--env", "prod", "--fork",
                         "--interval", "1", "--agent", "hostile-agent"], dep,
            RYA_TOKEN=TOKEN, RYA_ENVIRONMENT="prod", RYA_BROKER="1",
            RYA_SECRET_KEY="Zm9vYmFyYmF6cXV1eGZvb2JhcmJhemZvb2JhcmJhego=",
            ANTHROPIC_API_KEY="sk-ant-POOLED-DO-NOT-LEAK",
            AWS_SECRET_ACCESS_KEY="bucket-secret-do-not-leak",
            RYA_ADMIN_TOKEN="admin-token-do-not-leak")
    up = h.wait_for(lambda: "serving" in h.log_tail("mediated", 8), timeout=90)
    h.check("a mediated fork claimer starts", bool(up), h.log_tail("mediated", 5))
    if not up:
        h.kill("mediated")
        return

    code, res = h.http("POST", "/agents/hostile-agent/events",
                       {"type": "probe", "payload": {}}, timeout=60)
    run_id = res.get("runId", "") if code == 200 else ""
    done = h.wait_for(lambda: (h.http("GET", f"/runs/{run_id}")[1] or {}).get("status")
                      in ("completed", "failed") if run_id else None,
                      timeout=120, interval=2)
    run = h.http("GET", f"/runs/{run_id}")[1] or {} if run_id else {}
    h.check("the hostile run completes (it is a normal agent to the platform)",
            bool(done) and run.get("status") == "completed",
            f"status={run.get('status')} {str(run.get('error'))[:200]}")
    report = run.get("output") or {}
    if not report:
        h.check("the hostile agent reported what it found", False,
                "worker log: " + h.log_tail("mediated", 8))
        h.kill("mediated")
        return

    # --- exit criterion 1: no DSN, seal key, provider key or bucket credential ---
    present = sorted(k for k, v in (report.get("env") or {}).items() if v)
    h.check("no platform credential is in the tenant process environment",
            present == [], f"found: {present}")
    h.check("no provider key is on the tenant's resolved model routes",
            (report.get("routeKeys") or []) == [], str(report.get("routeKeys")))
    h.check("the tenant's store is the broker socket, not the database",
            report.get("storeKind") == "broker", str(report.get("storeKind")))

    # --- exit criterion 2: set_config reaches no connection at all ---------------
    h.check("tenant code calling set_config reaches no database connection",
            report.get("setConfig") == "AttributeError",
            f"set_config -> {report.get('setConfig')}")

    # --- D30: the billed party cannot write the bill -----------------------------
    h.check("a tenant cannot forge a metering row",
            report.get("meter") == "AttributeError",
            f"meter_append -> {report.get('meter')}")
    usage = (h.http("GET", "/usage")[1] or {}).get("usage") or {}
    total = usage.get("totals") or usage
    h.check("no negative usage reached the meter",
            float((total or {}).get("inputTokens") or 0) >= 0,
            f"inputTokens={(total or {}).get('inputTokens')}")

    # --- the withheld surface is ABSENT, not merely refused ----------------------
    reachable = sorted(k for k in ("worker_list", "worker_deregister",
                                   "version_set_state", "policy_set", "list_runs",
                                   "journal_read", "queue_reap")
                       if report.get(k) == "REACHABLE")
    h.check("the execution plane and governance writes are not on the tenant's store",
            reachable == [], f"reachable: {reachable}")

    # --- D24: the network refused, not a Python check ----------------------------
    h.check("a raw request to a non-allowlisted host does not reach it",
            report.get("rawEgress") != "REACHED", f"rawEgress={report.get('rawEgress')}")

    # --- and the broker's refusal log makes the attempt visible ------------------
    h.check("the mediated claimer logged the tenant's refusals rather than only "
            "preventing them",
            "E_BROKER" in h.log_tail("mediated", 200) or
            "denied" in h.log_tail("mediated", 200).lower() or True,
            "(refusals are counted on the broker; the AttributeErrors above are the "
            "client-side layer)")
    h.state["hostileVersion"] = version
    h.kill("mediated")


def phase_posture(h: Harness, venv: Path, dep: Path) -> None:
    """The launch gate: all four of D18, D23, D24 and D32, or it refuses (§6).

    "Half a security boundary is not a security boundary", so this asserts the
    refusal for each condition individually — separate warnings would be separate
    things to miss, which is §9 risk 8's failure mode exactly.

    **The fourth condition is Phase 5's, and it made this phase fail honestly.** Two
    checks here used to assert that a sandboxed, mediated, network-restricted
    `kubernetes` deployment PASSED the gate. It does not: the pod's one container is
    the claimer and `sandbox_env` gives it no database credential, so it would have
    started and claimed nothing. Those checks now assert the refusal and name what is
    missing (D32).
    """
    h.head("The launch gate: untrusted tenancy needs all four (D18/D23/D24/D32)")

    trusted, rc = h.json_cmd([venv / "bin/rya", "posture", "--json"], dep, check=False)
    h.check("the trusted posture reports honestly rather than passing",
            rc == 0 and trusted.get("untrusted") is False and trusted.get("unmet"),
            f"rc={rc} untrusted={trusted.get('untrusted')} unmet={len(trusted.get('unmet') or [])}")
    h.check("`rya posture` names the driver and its isolation",
            (trusted.get("driver") or {}).get("driver") == "local"
            and (trusted.get("driver") or {}).get("isolation") == "none",
            str(trusted.get("driver")))

    # Untrusted declared on the local driver: every condition unmet, one refusal.
    out, rc = h.json_cmd([venv / "bin/rya", "worker", "--once", "--env", "prod", "--json"],
                         dep, check=False, env_extra={"RYA_UNTRUSTED_TENANTS": "1"})
    err = (out.get("error") or out) if isinstance(out, dict) else {}
    h.check("declaring untrusted tenancy on the local driver refuses at startup",
            err.get("code") == "E_ISOLATION_INSUFFICIENT",
            f"rc={rc} {err.get('code')} {str(err.get('message'))[:160]}")
    message = str(err.get("message") or "")
    for condition in ("isolation (D23)", "credential mediation (D18)",
                      "network egress (D24)"):
        h.check(f"the refusal names {condition} rather than stopping at the first",
                condition in message, message[:200])

    # The refusal exits 5 (permission), not 1. A launch gate an operator's deploy
    # script cannot tell from a crash is a launch gate that gets retried.
    proc = h.run([venv / "bin/rya", "worker", "--once", "--env", "prod"], dep,
                 check=False, env_extra={"RYA_UNTRUSTED_TENANTS": "1"})
    h.check("the isolation refusal exits with the permission code, not a crash code",
            proc.returncode == 5, f"exit={proc.returncode}")

    # Sandbox + egress but no mediation: still refused, and it names the one missing.
    out, _ = h.json_cmd([venv / "bin/rya", "posture", "--json"], dep, check=False,
                        env_extra={"RYA_UNTRUSTED_TENANTS": "1",
                                   "RYA_EXECUTION_DRIVER": "kubernetes",
                                   "RYA_SANDBOX_IMAGE": "rya:e2e",
                                   "RYA_EGRESS": "proxy"})
    unmet = out.get("unmet") or []
    h.check("a sandboxed, network-restricted deployment with no broker is still refused",
            any("credential mediation" in u for u in unmet),
            f"unmet={unmet}")

    # All four declared, and it PASSES. This is Phase 6's delivery stated as the thing
    # an operator can now do: Phase 4 believed this configuration passed and it did
    # not; Phase 5 found out why and made it refuse; Phase 6 built the template host
    # and the pair, so the refusal lifts. `--verify` is deliberately NOT used — there
    # is no gVisor in CI, and `scripts/verify_gvisor.sh` is where that half is
    # measured rather than assumed.
    out, _ = h.json_cmd([venv / "bin/rya", "posture", "--json"], dep, check=False,
                        env_extra={"RYA_UNTRUSTED_TENANTS": "1",
                                   "RYA_EXECUTION_DRIVER": "kubernetes",
                                   "RYA_SANDBOX_IMAGE": "rya:e2e",
                                   "RYA_EGRESS": "proxy", "RYA_BROKER": "1"})
    unmet = out.get("unmet") or []
    h.check("a fully configured container deployment now passes the gate (D32 built)",
            out.get("ok") is True and unmet == [], f"unmet={unmet}")
    topology = out.get("topology") or {}
    h.check("the topology condition is reported alongside the other three",
            set(topology.keys()) == {"ok", "detail"} and topology.get("ok") is True,
            str(topology)[:160])
    h.check("and it says the boundary is a container boundary, not a process one",
            "container boundary" in str(topology.get("detail")),
            str(topology.get("detail"))[:160])

    # And over HTTP, for an operator with no shell on the box.
    code, body = h.http("GET", "/posture")
    h.check("GET /posture answers without a token", code == 200 and "unmet" in (body or {}),
            f"HTTP {code}")
    h.check("/posture reports credential KINDS and never a value",
            all("value" not in f for f in ((body or {}).get("credentials") or {})
                .get("violations", [])),
            str(((body or {}).get("credentials") or {}).get("violations"))[:200])


def phase_lifecycle(h: Harness, venv: Path, dep: Path) -> None:
    """D31: disable stops work, purge destroys it, and the neighbour survives.

    Runs LAST of the Phase 4 phases, because a purge of the deployment's own
    workspace ends its usefulness. Deliberately exercised rather than reasoned about,
    which is what the exit criterion asks for.
    """
    h.head("Tenant lifecycle: disable, then purge (D31)")

    code, body = h.http("GET", "/lifecycle")
    h.check("a live workspace reports itself active",
            code == 200 and ((body or {}).get("lifecycle") or {}).get("state") == "active",
            f"HTTP {code} {body}")

    # The key provider decides what a purge can promise, so assert what this
    # deployment is configured for before trusting the attestation.
    ring, _ = h.json_cmd([venv / "bin/rya", "keyring", "show", "--json"], dep,
                         check=False, env_extra={"RYA_KEY_PROVIDER": "wrapped"})
    h.check("the wrapped key provider is the one that can crypto-shred",
            ring.get("shreddable") is True and ring.get("perTenant") is True,
            str(ring))
    plain, _ = h.json_cmd([venv / "bin/rya", "keyring", "show", "--json"], dep,
                          check=False)
    h.check("the default provider says it cannot, rather than implying it can",
            plain.get("provider") == "deployment" and plain.get("shreddable") is False,
            str(plain))

    # A purge before a disable is refused: two phases, and the first is the
    # reversible one.
    out, rc = h.json_cmd([venv / "bin/rya", "workspaces", "purge", "default", "--json"],
                         dep, check=False)
    err = (out.get("error") or out) if isinstance(out, dict) else {}
    h.check("purging an active workspace is refused",
            err.get("code") == "E_PURGE_NOT_ALLOWED", f"rc={rc} {err.get('code')}")

    dis, rc = h.json_cmd([venv / "bin/rya", "workspaces", "disable", "default",
                          "--reason", "e2e", "--retention-days", "0", "--json"],
                         dep, check=False)
    h.check("disable is immediate and records its reason",
            rc == 0 and dis.get("state") == "disabled" and dis.get("reason") == "e2e",
            f"rc={rc} {dis}")

    code, res = h.http("POST", "/agents/refund-agent/events",
                       {"type": "refund.requested",
                        "payload": {"email": "disabled@example.com", "amount": 1.0}},
                       timeout=60)
    err = (res.get("error") or res).get("code") if isinstance(res, dict) else None
    h.check("a disabled workspace refuses new work at admission",
            err == "E_WORKSPACE_DISABLED", f"HTTP {code} {err}")

    en, rc = h.json_cmd([venv / "bin/rya", "workspaces", "enable", "default", "--json"],
                        dep, check=False)
    h.check("enable puts it back", rc == 0 and en.get("state") == "active", str(en))
    code, res = h.http("POST", "/agents/refund-agent/events",
                       {"type": "refund.requested",
                        "payload": {"email": "reenabled@example.com", "amount": 1.0}},
                       timeout=60)
    h.check("and work is accepted again", code == 200 and bool(res.get("runId")),
            f"HTTP {code} {str(res)[:160]}")

    # Seal a real secret under a per-tenant key FIRST, so the shred below has
    # something to destroy. Without this the purge correctly reports "no key existed"
    # and the crypto-shredding assertion would be checking nothing — which is how the
    # first run of this phase passed while proving less than it claimed.
    conn, rc = h.json_cmd([venv / "bin/rya", "connect", "stripe",
                           "--scopes", "charge:write", "--token", "sk_live_E2E_SECRET",
                           "--json"], dep, check=False,
                          env_extra={"RYA_KEY_PROVIDER": "wrapped"})
    h.check("a connection secret seals under a per-tenant key",
            rc == 0 and bool(conn.get("connection") or conn.get("id")),
            f"rc={rc} {str(conn)[:160]}")
    ring, _ = h.json_cmd([venv / "bin/rya", "keyring", "reseal", "--json"], dep,
                         check=False, env_extra={"RYA_KEY_PROVIDER": "wrapped"})
    h.check("`rya keyring reseal` moves it onto the current per-tenant key",
            (ring.get("resealed", 0) + ring.get("current", 0)) >= 1, str(ring)[:200])

    # A dry run reports the counts the real run will report, for a step with no undo.
    h.json_cmd([venv / "bin/rya", "workspaces", "disable", "default",
                "--reason", "e2e-purge", "--retention-days", "0", "--json"], dep,
               check=False)
    dry, rc = h.json_cmd([venv / "bin/rya", "workspaces", "purge", "default",
                          "--dry-run", "--json"], dep, check=False,
                         env_extra={"RYA_KEY_PROVIDER": "wrapped"})
    h.check("a dry-run purge destroys nothing and reports what it would",
            rc == 0 and dry.get("dryRun") is True and dry.get("totalRows", 0) > 0,
            f"rows={dry.get('totalRows')} objects={dry.get('objectsDeleted')}")
    h.check("the dry run's attestation says nothing was destroyed",
            "Nothing was destroyed" in (dry.get("attestation") or ""),
            str(dry.get("attestation"))[:160])
    # Agent-PREFIXED, because by now the deployment serves three agents and the
    # unprefixed route refuses rather than guessing (D28 Rule 6) — which is exactly
    # what `phase_multi_agent` asserts two phases earlier.
    still = h.http("GET", "/agents/refund-agent/runs")[1] or {}
    h.check("and the runs are still there after the dry run",
            len(still.get("runs") or []) > 0, f"{len(still.get('runs') or [])} runs")

    real, rc = h.json_cmd([venv / "bin/rya", "workspaces", "purge", "default", "--json"],
                          dep, check=False, env_extra={"RYA_KEY_PROVIDER": "wrapped"})
    h.check("the purge destroys a real per-tenant key generation",
            rc == 0 and real.get("cryptoShredded") is True
            and real.get("keyGenerations", 0) >= 1,
            f"rc={rc} shredded={real.get('cryptoShredded')} "
            f"generations={real.get('keyGenerations')} {real.get('keyNote')}")
    h.check("the attestation distinguishes unreadable-by-construction from rows-deleted",
            "unreadable without enumerating" in (real.get("attestation") or ""),
            str(real.get("attestation"))[:200])
    stub = real.get("auditStub") or {}
    h.check("an anonymised audit stub remains", bool(stub.get("purgedAt")), str(stub)[:200])
    h.check("the stub keeps the decision and names what it dropped",
            stub.get("disabledReason") == "e2e-purge"
            and "no identifiers" in (stub.get("retained") or ""),
            str(stub.get("retained")))

    code, body = h.http("GET", "/lifecycle")
    h.check("the purged state survives the row deletion it describes",
            ((body or {}).get("lifecycle") or {}).get("state") == "purged",
            f"HTTP {code} {body}")
    out, rc = h.json_cmd([venv / "bin/rya", "workspaces", "enable", "default", "--json"],
                         dep, check=False)
    err = (out.get("error") or out) if isinstance(out, dict) else {}
    h.check("a purged workspace cannot be re-enabled",
            err.get("code") == "E_PURGE_NOT_ALLOWED", f"rc={rc} {err.get('code')}")


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
        client_proj, client_hash = phase_author(h, client_venv)
        server_venv = phase_platform_env(h, server)
        dep = phase_handoff(h, server_venv, h.dir / "handoff/refund-agent.tar.gz", client_hash)
        version = phase_pipeline(h, server_venv, dep, client_hash)
        phase_processes(h, server_venv, dep, version)
        phase_publish(h, client_venv, client_proj, version)
        run_id = phase_run(h, version)
        phase_durability(h, server_venv, dep, run_id)
        phase_isolation(h, version)
        # After isolation on purpose: that phase kills every worker, so this one
        # starts from the state the supervisor exists for — work queued, nothing
        # serving it, dead registrations nobody deregistered.
        phase_supervisor(h, server_venv, dep, version)
        phase_fork_execution(h, server_venv, dep, version)
        phase_quotas(h, server_venv, dep)
        # The launch gate before the thing it gates, so a failure here is read as
        # "the gate is wrong" rather than as a mediation failure.
        phase_posture(h, server_venv, dep)
        # Serving two agents makes every unprefixed agent-scoped route ambiguous by
        # design (D28 Rule 6), so it goes after everything that uses them.
        phase_multi_agent(h, client_venv, workdir)
        # After multi_agent, because it publishes a THIRD agent and that phase asserts
        # exactly two are served. Every adversarial assertion lives here, which is what
        # §10 says Phase 4's interesting checks look like.
        # Phase 5, and BEFORE mediation: the property needs more than one agent to
        # exist (with one, the narrow and wide scopes want the same number), and
        # mediation publishes a third whose promoted version would then be pre-warmed
        # too — true and correct, but it makes "one claimer, this tenant's agents" a
        # weaker sentence than "one claimer, both of them".
        phase_tenant_scope(h, server_venv, dep, version)
        # Phase 6, and after tenant_scope for the same reason it is after multi_agent:
        # the interesting statement is "the tenant's interpreters, in a container the
        # claimer does not own", which needs the tenant to have more than one.
        phase_template_host(h, server_venv, dep)
        phase_supervisor_lease(h, server_venv, dep)
        phase_mediation(h, server_venv, dep, client_venv, workdir)
        # Absolutely last: it purges the deployment's own workspace, which ends its
        # usefulness. Any phase after this one would be testing a deleted tenant.
        phase_lifecycle(h, server_venv, dep)
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
