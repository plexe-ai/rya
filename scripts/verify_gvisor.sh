#!/usr/bin/env bash
#
# D23, verified rather than declared — MULTITENANT_PLAN §6 "What is not proven".
#
# Phase 4 built the sandbox and never executed it, which left two claims resting on a
# reading rather than a measurement: that the third-party wheels work under `runsc`,
# and that the isolation probe recognises a real sentry. §9 risk 8's whole point is
# that an isolation claim nobody checked is worse than one nobody made, so this is the
# script that checks it.
#
# It answers three questions:
#
#   1. Do `cryptography`, `pydantic-core` and `psycopg` work under gVisor — not just
#      import, but do the syscall-heavy thing each of them exists for?
#   2. Does `os.fork` work in there? (D27's warm pool is the whole execution plane.)
#   3. Does `read_isolation_signals` return `verified=True` against a REAL sentry —
#      including in the hardened configuration, where `--cap-drop=ALL` makes `dmesg`
#      unreadable and `/proc/version` is the only signal left?
#
# Question 3 is the one that found a bug the first time it was asked. See the comment
# on `GVISOR_VERSION_MARKER` in `execution/drivers.py`.
#
# Why it runs nested in a privileged container: same reason `bench_gvisor.sh` does.
# This host has no `runsc`, no passwordless sudo, and AppArmor blocks unprivileged
# user namespaces (`apparmor_restrict_unprivileged_userns=1`), so rootless gVisor is
# unavailable and a daemon reconfiguration would disrupt unrelated containers. The
# nesting affects TIMING and nothing else — the sentry is real, the syscall
# interception is real, and correctness is what this script measures.
#
# Usage:  scripts/verify_gvisor.sh
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_IMAGE="rya-bench:latest"
GVISOR_IMAGE="rya-bench-gvisor:latest"
cd "$REPO"

echo "==> ensuring gVisor image ($GVISOR_IMAGE)"
if ! docker image inspect "$GVISOR_IMAGE" >/dev/null 2>&1; then
  docker image inspect "$BASE_IMAGE" >/dev/null 2>&1 || docker build -t "$BASE_IMAGE" .
  docker build -t "$GVISOR_IMAGE" -f - . >/dev/null <<EOF
FROM $BASE_IMAGE
RUN apt-get update -qq \
 && apt-get install -y -qq curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN ARCH=\$(uname -m) \
 && curl -fsSL -o /usr/local/bin/runsc \
      "https://storage.googleapis.com/gvisor/releases/release/latest/\${ARCH}/runsc" \
 && chmod 755 /usr/local/bin/runsc \
 && runsc --version
EOF
fi

OUT="$REPO/.rya/bench/gvisor-verify.json"

echo "==> running the payload inside a gVisor sandbox"
# The CONTAINER writes the results file, not this shell. `.rya/` is created root-owned
# by whichever bench container made it first, so a host-side redirect into it fails
# with EPERM for the developer who ran the container — which is a papercut, but the
# fix here is to write from the side that owns the directory rather than to require a
# chown the developer cannot do.
docker run --rm --privileged -v "$REPO":/repo -w /repo "$GVISOR_IMAGE" \
  bash -euo pipefail -c '
mkdir -p /repo/.rya/bench
runsc --version | head -1
# --network=none, because a sandbox that needs the network to prove it works would
# be proving the wrong thing. Every check below is deliberately offline.
runsc --platform=systrap --ignore-cgroups --network=none \
  do python scripts/verify_gvisor.py > /tmp/verify.out 2>&1 || {
    echo "  the sandbox run FAILED:"; tail -20 /tmp/verify.out; exit 1; }
grep -o "VERIFY:.*" /tmp/verify.out | sed "s/^VERIFY://" \
  > /repo/.rya/bench/gvisor-verify.json
'

echo "==> reading the verdict"
OUT="$OUT" python3 - <<'PY'
import json, os, pathlib, sys
sys.path.insert(0, "src")

data = json.loads(pathlib.Path(os.environ["OUT"]).read_text())
print(f"  sandbox: python {data['python']} on {data['machine']}, "
      f"kernel {data['uname']!r}")
print()
print("  %-16s %-6s %s" % ("dependency", "", "detail"))
print("  " + "-" * 74)
for check in data["checks"]:
    mark = "\033[32mok\033[0m  " if check["ok"] else "\033[31mFAIL\033[0m"
    body = (json.dumps(check["detail"]) if check["ok"] else check["error"])[:56]
    print(f"  {check['name']:<16} {mark}   {body}")
    if not check["ok"]:
        print("      " + check.get("traceback", "").replace("\n", "\n      ")[-500:])
print()

# ---- the probe, against a real sentry rather than a fixture -----------------
from rya.execution.drivers import ISOLATION_SANDBOXED, read_isolation_signals

signals = data["signals"]
raw = "\n".join(f"{k}={v}" for k, v in signals.items())
full = read_isolation_signals(raw, driver="docker", declared=ISOLATION_SANDBOXED)

# The hardened case, and the one that matters: `ContainerDriver.hardening_args`
# always passes --cap-drop=ALL, which is usually what makes dmesg unreadable. If the
# probe only works when dmesg is readable, it does not work in the configuration the
# platform actually launches.
hardened_raw = "\n".join(f"{k}=" if k == "dmesg" else f"{k}={v}"
                         for k, v in signals.items())
hardened = read_isolation_signals(hardened_raw, driver="docker",
                                  declared=ISOLATION_SANDBOXED)

print("  ISOLATION PROBE, against a real sentry")
for label, probe in (("dmesg readable", full), ("dmesg SUPPRESSED (hardened)", hardened)):
    mark = "\033[32m✓\033[0m" if probe.verified else "\033[31m✗\033[0m"
    print(f"    {mark} {label:<28} verified={probe.verified} "
          f"effective={probe.effective}")
    print(f"      {probe.detail[:88]}")

ok = data["ok"] and full.verified and hardened.verified
print()
print("  " + ("\033[32mD23 HOLDS\033[0m — every dependency works under runsc and the "
              "probe recognises a real sentry"
              if ok else
              "\033[31mD23 IS IN QUESTION\033[0m — see the failures above; §9's "
              "trigger says the Kata / per-tenant-node-pool alternatives become live"))
raise SystemExit(0 if ok else 1)
PY
