#!/usr/bin/env bash
#
# Phase 0 measurement: does D23 (gVisor) survive contact with the cold-start budget?
#
# `docs/MULTITENANT_PLAN.md` §2 asks for "sandbox start + bundle materialisation +
# interpreter start + import" against COLD_START_TARGET_MS. This script produces
# that by running the SAME stages from bench_cold_start.py twice — once natively
# and once inside a gVisor sandbox — so the sandbox overhead is a subtraction
# rather than a guess.
#
# Two costs are separated, because they behave differently and only one of them
# is gVisor-specific:
#
#   1. SANDBOX BRING-UP  — one-off per sandbox (`runsc do /bin/true`). Amortised
#      away by a warm pool, paid in full on scale-from-zero.
#   2. IN-SANDBOX EXECUTION — syscall interception tax on every import, fork and
#      file read. Never amortised; it is a permanent multiplier.
#
# Why it runs in a privileged container: this host has no `runsc`, no
# passwordless sudo, and AppArmor blocks unprivileged user namespaces
# (`apparmor_restrict_unprivileged_userns=1`), so rootless gVisor is unavailable.
# A privileged container is the only path to a real measurement here. That means
# `--ignore-cgroups`, so these numbers exclude cgroup setup — see the caveats
# printed at the end.
#
# Usage:  scripts/bench_gvisor.sh [iterations] [agent-project-path]
#
#   scripts/bench_gvisor.sh 10                      # synthetic heavy agent
#   scripts/bench_gvisor.sh 10 examples/followup_agent

set -euo pipefail

ITER="${1:-10}"
AGENT_ARG=""
[ -n "${2:-}" ] && AGENT_ARG="--agent $2"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_IMAGE="rya-bench:latest"
GVISOR_IMAGE="rya-bench-gvisor:latest"

cd "$REPO"

echo "==> ensuring base image ($BASE_IMAGE)"
docker image inspect "$BASE_IMAGE" >/dev/null 2>&1 || docker build -t "$BASE_IMAGE" .

echo "==> building gVisor bench image ($GVISOR_IMAGE)"
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

echo "==> running measurement (iterations=$ITER)"
docker run --rm --privileged \
  -v "$REPO":/repo -w /repo \
  -e ITER="$ITER" -e AGENT_ARG="$AGENT_ARG" \
  "$GVISOR_IMAGE" bash -euo pipefail -c '
RUNSC="runsc --platform=systrap --ignore-cgroups"
# Results land under .rya/, which is already gitignored, so a benchmark run does
# not dirty the working tree.
mkdir -p /repo/.rya/bench

echo
echo "############ 1. SANDBOX BRING-UP ############"
python - <<PY
import subprocess, statistics, time, os
iters = int(os.environ["ITER"])

def bench(argv, label):
    out = []
    for _ in range(iters):
        t0 = time.perf_counter_ns()
        p = subprocess.run(argv, capture_output=True)
        dt = (time.perf_counter_ns() - t0) / 1e6
        if p.returncode != 0:
            return label, None, p.stderr.decode()[-300:]
        out.append(dt)
    return label, out, None

results = {}
CASES = [
    (["/bin/true"], "native /bin/true (floor)"),
    ("runsc --platform=systrap --ignore-cgroups --network=none do /bin/true".split(),
     "runsc do (network=none)"),
    ("runsc --platform=systrap --ignore-cgroups --network=host do /bin/true".split(),
     "runsc do (network=host)"),
]

print("  %-34s %10s %10s %10s" % ("case", "median", "p95", "min"))
print("  " + "-" * 66)
base = None
for argv, label in CASES:
    label, samples, err = bench(argv, label)
    if samples is None:
        detail = err.strip().splitlines()[-1][:60] if err.strip() else "unknown"
        print("  %-34s   FAILED: %s" % (label, detail))
        continue
    med = statistics.median(samples)
    srt = sorted(samples)
    p95 = srt[min(len(srt)-1, round(0.95*(len(srt)-1)))]
    if base is None:
        base = med
    results[label] = round(med, 1)
    print(f"  {label:<34} {med:>9.1f}ms {p95:>9.1f}ms {min(samples):>9.1f}ms"
          + (f"   (+{med-base:.1f}ms vs floor)" if med != base else ""))

import json
with open("/repo/.rya/bench/bringup.json", "w") as fh:
    json.dump(results, fh, indent=2)
PY

echo
echo "############ 2. STAGES, NATIVE (baseline in this same image) ############"
python scripts/bench_cold_start.py --iterations "$ITER" $AGENT_ARG \
  --label "native" --json /repo/.rya/bench/native.json \
  | sed -n "/stage  /,/^---/p;/^  interpreter/,/bundle unpack/p"

echo
echo "############ 3. STAGES, INSIDE GVISOR ############"
# --json-stdout, not --json: a sandbox write does not reach the bind mount.
$RUNSC --network=host do python scripts/bench_cold_start.py --iterations "$ITER" \
  $AGENT_ARG --label "gvisor" --json-stdout > /tmp/gv.out 2>/tmp/gv.err || {
    echo "  gVisor stage run FAILED:"; tail -5 /tmp/gv.err; exit 1; }
sed -n "/stage  /,/^---/p;/^  interpreter/,/bundle unpack/p" /tmp/gv.out
grep -o "JSONRESULT:.*" /tmp/gv.out | sed "s/^JSONRESULT://" > /repo/.rya/bench/gvisor.json
'

echo
echo "==> comparing"
python3 - <<'PY'
import json, pathlib
def load(p):
    f = pathlib.Path(p)
    return json.loads(f.read_text()) if f.is_file() else None

nat, gv = load(".rya/bench/native.json"), load(".rya/bench/gvisor.json")
if not (nat and gv):
    print("  (missing one side; skipping comparison)")
    raise SystemExit(0)

target = nat["meta"]["target_ms"]
gvs = {s["key"]: s for s in gv["stages"]}
print(f"  {'stage':<30} {'native':>10} {'gVisor':>10} {'x':>7} {'% budget':>9}")
print("  " + "-" * 70)
for s in nat["stages"]:
    g = gvs.get(s["key"])
    if not g or not s["samples"] or not g["samples"]:
        continue
    n, v = s["median_ms"], g["median_ms"]
    ratio = v / n if n else float("nan")
    print(f"  {s['label']:<30} {n:>9.1f}ms {v:>9.1f}ms {ratio:>6.2f}x {100*v/target:>8.1f}%")
print("  " + "-" * 70)
print(f"  budget COLD_START_TARGET_MS = {target}ms")

# The number D23 actually turns on: everything a scale-from-zero run pays before
# the handler's first line. Bring-up + materialise the bundle + boot + import.
bring = load(".rya/bench/bringup.json") or {}
setup = bring.get("runsc do (network=host)", 0.0)
def med(src, key):
    for s in src["stages"]:
        if s["key"] == key:
            return s["median_ms"]
    return 0.0

print()
print("  END-TO-END SCALE-FROM-ZERO (bring-up + bundle + boot + import)")
for name, src, extra in (("native (runc)", nat, 0.0), ("gVisor (runsc)", gv, setup)):
    total = extra + med(src, "bundle") + med(src, "cold_total")
    print(f"    {name:<16} {total:>8.1f}ms   {100*total/target:>5.1f}% of budget"
          + (f"   (incl. {extra:.0f}ms sandbox bring-up)" if extra else ""))
print(f"    agent measured: {nat['meta']['agent']['name']}")
print()
print("  CAVEATS")
print("   - aarch64 host; systrap platform; no KVM (no /dev/kvm on this instance)")
print("   - nested in a privileged container with --ignore-cgroups, so cgroup")
print("     setup cost is excluded")
print("   - `runsc do` approximates sandbox bring-up; a production launch via")
print("     docker --runtime=runsc or a k8s RuntimeClass adds image/snapshot")
print("     setup, which runc pays too")
PY
