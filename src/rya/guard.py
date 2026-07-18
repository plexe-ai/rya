"""Action Guard — the agent's egress firewall.

Every outbound request the runtime makes (HTTP tools, model calls, channel
sends) is checked here *before the bytes leave the process*:

    SSRF blocklist  →  static deny rules  →  static allow rules  →  default

A blocked request raises ``E_EGRESS_BLOCKED`` and never goes out — real
network-level blocking, not advice. The policy lives in ``rya.guard.yaml`` and is
hot-reloaded per request (edit + save = takes effect immediately). If no policy
file exists the guard is a no-op, so it's strictly opt-in and backward-compatible.

Rule kinds: ``prefix`` (url startswith), ``glob`` (fnmatch), ``exact``. ``deny``
always beats ``allow``. ``default`` is ``deny`` | ``allow``.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import os
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import yaml

from .errors import RyaError

GUARD_FILE = "rya.guard.yaml"

_SSRF_HOSTS = {"localhost", "metadata.google.internal", "169.254.169.254"}

# Tiny mtime cache so per-request reads are cheap but still hot-reload on save.
_cache: dict = {}


def _policy_path(path: Optional[str] = None) -> Path:
    if path:
        return Path(path)
    env = os.environ.get("RYA_GUARD_PATH")
    return Path(env) if env else Path.cwd() / GUARD_FILE


def load_policy(path: Optional[str] = None) -> Optional[dict]:
    """Load the guard policy, hot-reloaded by mtime. None = no policy (no-op)."""
    p = _policy_path(path)
    if not p.is_file():
        return None
    mtime = p.stat().st_mtime
    cached = _cache.get(str(p))
    if cached and cached[0] == mtime:
        return cached[1]
    policy = yaml.safe_load(p.read_text()) or {}
    policy.setdefault("ssrf", True)
    policy.setdefault("default", "deny")
    policy.setdefault("fail", "closed")
    policy.setdefault("rules", [])
    _cache[str(p)] = (mtime, policy)
    return policy


def save_policy(policy: dict, path: Optional[str] = None) -> Path:
    p = _policy_path(path)
    p.write_text(yaml.safe_dump(policy, sort_keys=False))
    _cache.pop(str(p), None)
    return p


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def is_ssrf(host: str) -> bool:
    if not host:
        return True
    if host in _SSRF_HOSTS or host.endswith(".local") or host.endswith(".internal"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
    except ValueError:
        return False  # a domain name — not resolved here


def _matches(rule: dict, url: str, method: str) -> bool:
    methods = [m.upper() for m in (rule.get("methods") or [])]
    if methods and method.upper() not in methods:
        return False
    kind = rule.get("kind", "glob")
    pat = rule.get("pattern", "")
    if kind == "prefix":
        return url.startswith(pat)
    if kind == "exact":
        return url == pat
    return fnmatch.fnmatch(url, pat)


def evaluate(url: str, method: str, policy: dict) -> dict:
    """Return {decision: 'allow'|'block', reason, rule?} for one request."""
    if policy.get("ssrf", True) and is_ssrf(_host(url)):
        return {"decision": "block", "reason": "SSRF blocklist (private/loopback/metadata host)"}

    rules = policy.get("rules", [])
    for r in rules:
        if r.get("action") == "deny" and _matches(r, url, method):
            return {"decision": "block", "reason": r.get("note") or "matched a deny rule", "rule": r}
    for r in rules:
        if r.get("action") == "allow" and _matches(r, url, method):
            return {"decision": "allow", "reason": r.get("note") or "matched an allow rule", "rule": r}

    default = policy.get("default", "deny")
    if default == "allow":
        return {"decision": "allow", "reason": "default allow"}
    return {"decision": "block", "reason": "default deny (not in the allowlist)"}


def check_egress(url: str, method: str = "POST", path: Optional[str] = None) -> None:
    """Enforce the policy on one outbound request. Raises if blocked; no-op if no policy."""
    policy = load_policy(path)
    if policy is None:
        return
    result = evaluate(url, method, policy)
    if result["decision"] == "block":
        raise RyaError("E_EGRESS_BLOCKED",
                       f"Action Guard blocked {method} {url} — {result['reason']}.",
                       hint="Add an allow rule in rya.guard.yaml, or fix the request.")


# --- policy self-test (drives the console metrics) ------------------------
def run_tests(policy: dict) -> dict:
    """Probe the policy with benign + attack requests and score it."""
    cases = []
    # Benign: each allow rule should pass with a conforming request.
    for r in policy.get("rules", []):
        if r.get("action") != "allow":
            continue
        url = _example_url(r)
        method = (r.get("methods") or ["GET"])[0]
        cases.append({"label": "benign", "url": url, "method": method, "expect": "allow"})
    # Attacks: each deny rule should block; plus fixed exfil/SSRF probes.
    for r in policy.get("rules", []):
        if r.get("action") == "deny":
            cases.append({"label": "attack", "url": _example_url(r), "method": "POST", "expect": "block"})
    for url in ("https://webhook.site/abc", "http://169.254.169.254/latest/meta-data/",
                "http://localhost:8888/admin", "https://api.stripe.com/v1/charges"):
        cases.append({"label": "attack", "url": url, "method": "POST", "expect": "block"})

    passed = attacks_blocked = attacks_total = benign_false_blocks = benign_total = 0
    for c in cases:
        got = evaluate(c["url"], c["method"], policy)["decision"]
        c["got"] = got
        ok = got == c["expect"]
        c["pass"] = ok
        passed += ok
        if c["label"] == "attack":
            attacks_total += 1
            attacks_blocked += got == "block"
        else:
            benign_total += 1
            benign_false_blocks += got == "block"
    total = len(cases)
    return {
        "total": total, "passed": passed,
        "attacksBlocked": attacks_blocked, "attacksTotal": attacks_total,
        "benignFalseBlocks": benign_false_blocks, "benignTotal": benign_total,
        "accuracy": round(100 * passed / total) if total else 100,
        "cases": cases,
    }


def _example_url(rule: dict) -> str:
    pat = rule.get("pattern", "https://example.com/")
    if rule.get("kind") == "exact":
        return pat
    url = pat.replace("*", "x")
    return url + ("probe" if url.endswith("/") else "/probe")


# ---- grounding gate ---------------------------------------------------------
# Serving-path check (pattern proven in the AutoRentals concierge): every money
# figure in an outbound reply must be traceable to a tool output of the same
# run, so the model can never invent a price. Opt in via `grounding.enabled`
# in rya.guard.yaml; also callable directly as ctx.guard.check_grounding(text).

# $1,234.56 / USD 999 / 199 USD / EUR-style symbols. Deliberately currency-only:
# plain numbers (dates, counts) would drown the check in false positives.
_MONEY_RE = re.compile(
    r"(?:[$€£₹]\s?\d[\d,]*(?:\.\d+)?)|(?:\b(?:USD|EUR|GBP|INR|AED)\s?\d[\d,]*(?:\.\d+)?)"
    r"|(?:\b\d[\d,]*(?:\.\d+)?\s?(?:USD|EUR|GBP|INR|AED)\b)",
    re.IGNORECASE,
)
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _money_values(text: str) -> list:
    """Numeric values of every money figure in ``text`` (normalized floats)."""
    out = []
    for m in _MONEY_RE.findall(text or ""):
        n = _NUM_RE.search(m)
        if n:
            out.append(float(n.group().replace(",", "")))
    return out


def _all_numbers(obj) -> set:
    """Every numeric value reachable in a tool output (JSON-serialized scan)."""
    import json as _json
    blob = _json.dumps(obj, default=str)
    return {float(n.replace(",", "")) for n in _NUM_RE.findall(blob)}


def grounding_check(text: str, tool_outputs: list) -> dict:
    """Check that every money figure in ``text`` appears in ``tool_outputs``.

    Returns ``{ok, figures, violations}`` where figures are the money values
    found in the text and violations the subset with no grounding."""
    figures = _money_values(text)
    if not figures:
        return {"ok": True, "figures": [], "violations": []}
    grounded: set = set()
    for out in tool_outputs or []:
        grounded |= _all_numbers(out)
    violations = [f for f in figures if f not in grounded]
    return {"ok": not violations, "figures": figures, "violations": violations}
