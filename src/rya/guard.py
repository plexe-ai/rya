"""Action Guard — egress **policy**, grounding gate and id-secrecy scrub.

Every outbound request the runtime makes (HTTP tools, model calls, channel
sends) is checked here *before the bytes leave the process*:

    SSRF blocklist  →  static deny rules  →  static allow rules  →  default

A blocked request raises ``E_EGRESS_BLOCKED`` and never goes out.

**What this is and is not, after D24.** This module used to describe itself as "an
egress firewall … real network-level blocking, not advice", and that was a fair
description of a *cooperative* runtime and an overclaim about a hostile one. An
in-process check is bypassed by any code that does not call it, so against a tenant
that imports ``urllib`` directly it enforces nothing. MULTITENANT_DESIGN D24 moves
enforcement to the network layer and keeps this module as the **policy, audit and
governance** surface — which is the role it was always genuinely good at:
attributable verdicts, a reviewable allowlist, a version and an etag on every
decision, and grounding/secrecy checks the network cannot do at all.

So the division after D24 is:

======================  ==================================================
this module             *what is allowed*, and the audit record of asking
:mod:`rya.egress`       *what can physically leave*, and the reconciliation
======================  ==================================================

Both verdicts exist because they fail differently, and a **divergence** between
them is itself a signal — a stale sandbox network snapshot against a live policy is
the ordinary cause, and :func:`rya.egress.EgressService.divergences` is what makes
it alertable rather than invisible. Keeping the old "firewall" wording here would
have been exactly the kind of claim MULTITENANT_DESIGN §9 risk 4 exists to prevent.

Rule kinds: ``prefix`` (url startswith), ``glob`` (fnmatch), ``exact``. ``deny``
always beats ``allow``. ``default`` is ``deny`` | ``allow``.

Where the policy comes from (PLATFORM_DESIGN §5.1, D7, D8, §12 risk 7)
----------------------------------------------------------------------
The guard is *governance*, and under **D7** governance is platform-side: it must
not be forkable or laggable by client code. So the policy is **injected, never
discovered**. :func:`resolve_policy` accepts

  * an already-loaded ``dict``            → ``source="explicit"``
  * a store handle (see below)           → ``source="store"``
  * a path                               → ``source="file:<path>"``
  * ``None``                             → the legacy ambient fallback

and always returns a :class:`GuardPolicy` — a value object carrying ``version``
and ``etag`` plus the compiled rule set, so a verdict can be attributed to *which*
policy produced it (§12 risk 7: "who reviewed this allowlist change" is a
feature). Nothing below :func:`resolve_policy` ever touches the filesystem or the
environment.

The store handle is **duck-typed on purpose** — a policy source only has to offer

    policy_get(key) -> dict | None
    policy_set(key, value, actor=None) -> Any

with ``key == "guard"`` and a JSON-able envelope as the value (see
:func:`save_policy` for the exact shape). Anything that isn't a dict/path/policy
and doesn't expose ``policy_get`` degrades to the file fallback, so a store that
predates the protocol still works.

Reading the policy out of ``Path.cwd()`` or ``$RYA_GUARD_PATH`` survives as ONE
clearly-marked legacy fallback (:func:`_legacy_ambient_path`), used only when a
caller passes no source at all. Under the platform model a worker's cwd is an
artefact of where a bundle got extracted, and ambient env is exactly what **D8**
kills; a multi-process, multi-tenant deployment has no single policy file and
mtime is not a version.

Fail modes are deliberately **asymmetric**, and that asymmetry is the D7 property:

  * **no policy anywhere** (no file, nothing in the store) → the guard is a
    complete no-op. This is what keeps it opt-in and backward compatible.
  * **a policy that exists but cannot be read or parsed** (store raises, record
    corrupt, YAML broken) → **deny everything**. A governance component that
    silently switches itself off when its policy source breaks is not a
    governance component.
"""

from __future__ import annotations

import fnmatch
import hashlib
import ipaddress
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlparse

import yaml

from .errors import RyaError

GUARD_FILE = "rya.guard.yaml"

# The store key one agent's guard lives under. One key, one current version;
# history is the store's append-only policy log, not our problem.
#
# The bare literal is the UNQUALIFIED key, which is now only correct for a
# deployment serving one agent. `rya_policy` is keyed `(workspace_id, key)`, so
# before D28 two agents in one workspace silently shared a guard policy — and a
# guard is authored per project (`rya.guard.yaml` ships inside the bundle), so
# they cannot mean the same thing. Use `policy_key(agent)`.
POLICY_KEY = "guard"


def policy_key(agent: str | None = None) -> str:
    """The `rya_policy` key holding ``agent``'s guard policy (D28).

    ``None`` keeps the unqualified key, which is what a single-agent deployment
    wrote before this change and what `migrate_policy_keys` reads when it moves
    those rows. New writes always name an agent.
    """
    return f"{POLICY_KEY}:{agent}" if agent else POLICY_KEY


def store_key_for(store, agent: str | None = None) -> str:
    """Which key holds the policy ``agent`` is actually governed by (D28).

    The agent-qualified key once one has been written, else the pre-D28
    unqualified row. Read-time fallback rather than a rename at upgrade, for the
    same reason ``gates.gate_policy`` uses one: a rename can only be right where
    the workspace has exactly one agent, which is precisely where the fallback is
    already right — and a guard that quietly stopped enforcing at upgrade is a
    security regression, not an inconvenience.

    A read FAILURE returns the qualified key rather than falling back, so an
    unreachable store resolves to the agent's own (fail-closed) policy instead of
    a sibling's.
    """
    if agent:
        try:
            if store.policy_get(policy_key(agent)) is not None:
                return policy_key(agent)
        except Exception:
            return policy_key(agent)
    return POLICY_KEY

SOURCE_NONE = "none"
SOURCE_STORE = "store"
SOURCE_EXPLICIT = "explicit"

_SSRF_HOSTS = {"localhost", "metadata.google.internal", "169.254.169.254"}

# Anything that can name a policy: a loaded dict, a resolved GuardPolicy, a path,
# a store handle exposing policy_get, or None (= legacy ambient fallback). Left as
# `Any` deliberately — the store arm is duck-typed, so a closed Union would lie.
PolicySource = Any


# ---- compiled rules ---------------------------------------------------------
# Compiled ONCE per policy version. Previously every request re-ran `fnmatch`
# (which re-parses its pattern each call) and the secrecy regexes were cached by
# `id()` of a sub-dict — an address, not a version, so it could collide after a
# gc and could never be shared across processes or reasoned about in a log.

class _Matcher(NamedTuple):
    rule: dict
    methods: frozenset  # empty = any method
    test: Callable[[str], bool]

    def matches(self, url: str, method: str) -> bool:
        if self.methods and method.upper() not in self.methods:
            return False
        return self.test(url)


@dataclass(frozen=True)
class _Rules:
    """The compiled form of one policy version."""

    deny: tuple[_Matcher, ...] = ()
    allow: tuple[_Matcher, ...] = ()
    secrecy: tuple[tuple, ...] = ()   # (regex, replacement, id)


_EMPTY_RULES = _Rules()


def _compile_matcher(rule: dict) -> _Matcher:
    kind = rule.get("kind", "glob")
    pat = rule.get("pattern", "")
    methods = frozenset(m.upper() for m in (rule.get("methods") or []))
    if kind == "prefix":
        def test(url: str, _p: str = pat) -> bool:
            return url.startswith(_p)
    elif kind == "exact":
        def test(url: str, _p: str = pat) -> bool:
            return url == _p
    else:
        rx = re.compile(fnmatch.translate(pat))       # glob → regex, compiled once

        def test(url: str, _rx: re.Pattern = rx) -> bool:
            return _rx.match(url) is not None
    return _Matcher(rule=rule, methods=methods, test=test)


def _compile_secrecy_patterns(policy: dict) -> tuple[tuple, ...]:
    sec = (policy or {}).get("secrecy") or {}
    if not sec.get("enabled"):
        return ()
    out = []
    for p in sec.get("patterns", []) or []:
        if not isinstance(p, dict) or p.get("kind", "regex") != "regex":
            continue
        try:
            rx = re.compile(p["pattern"])
        except (re.error, KeyError, TypeError):
            # A malformed pattern is skipped, not fatal — a broken secrecy rule
            # must not take down every tool call. (deploy --check flags it.)
            continue
        out.append((rx, p.get("replacement", "(hidden)"), p.get("id", "secrecy")))
    return tuple(out)


def _compile(policy: dict) -> _Rules:
    rules = policy.get("rules") or []
    return _Rules(
        deny=tuple(_compile_matcher(r) for r in rules
                   if isinstance(r, dict) and r.get("action") == "deny"),
        allow=tuple(_compile_matcher(r) for r in rules
                    if isinstance(r, dict) and r.get("action") == "allow"),
        secrecy=_compile_secrecy_patterns(policy),
    )


# ---- the policy value object ------------------------------------------------

@dataclass(frozen=True)
class GuardPolicy:
    """One resolved policy version — the thing a guard verdict is attributable to.

    ``etag`` is a content hash of the normalized policy and is the real cache key;
    ``version`` is whatever the source calls this revision (a store-supplied
    version if it has one, else the etag). ``source`` says where it came from so a
    log line can distinguish a governed store policy from a dev file."""

    policy: dict
    etag: str
    version: str
    source: str
    rules: _Rules = _EMPTY_RULES
    error: str | None = None   # set ⇒ the source broke ⇒ fail closed (deny all)

    @property
    def enforced(self) -> bool:
        """False only for "no policy anywhere", the one case that stays a no-op."""
        return self.source != SOURCE_NONE

    def describe(self) -> dict:
        """Provenance for a trace/audit line — JSON-able, no rule bodies."""
        return {"version": self.version, "etag": self.etag, "source": self.source,
                "default": self.policy.get("default"),
                "rules": len(self.policy.get("rules") or []),
                "error": self.error}


NO_POLICY = GuardPolicy(policy={}, etag="", version=SOURCE_NONE, source=SOURCE_NONE)


def _normalize(policy: dict | None) -> dict:
    p = dict(policy or {})
    p.setdefault("ssrf", True)
    p.setdefault("default", "deny")
    p.setdefault("fail", "closed")
    p.setdefault("rules", [])
    return p


def _etag(policy: dict) -> str:
    return hashlib.sha256(
        json.dumps(policy, sort_keys=True, default=str).encode()).hexdigest()[:16]


# version cache: (source, version, etag) -> GuardPolicy. Keyed on the CONTENT
# hash, never on an mtime and never on a store-supplied version alone — two
# tenants may both call their policy "1" while meaning different rule sets, and
# their compiled rules must not contaminate each other. Bounded because the api
# process is long-lived and multi-tenant.
_policies: dict = {}
_POLICY_CACHE_MAX = 64


def _build(policy: dict, *, source: str, version: str | None = None,
           error: str | None = None) -> GuardPolicy:
    p = _normalize(policy)
    etag = _etag(p)
    key = (source, version or etag, etag)
    hit = _policies.get(key)
    if hit is not None:
        return hit
    gp = GuardPolicy(policy=p, etag=etag, version=version or etag, source=source,
                     rules=_compile(p), error=error)
    if len(_policies) >= _POLICY_CACHE_MAX:
        _policies.pop(next(iter(_policies)), None)
    _policies[key] = gp
    return gp


def _closed(reason: str, source: str) -> GuardPolicy:
    """A policy that exists but is unusable ⇒ deny everything (D7).

    Deliberately NOT cached: the next request should retry the source rather than
    stay latched off a transient read failure."""
    return GuardPolicy(policy=_normalize({}), etag="", version="unreadable",
                       source=source, rules=_EMPTY_RULES, error=reason)


# ---- resolution -------------------------------------------------------------

def _legacy_ambient_path(path: str | None = None) -> Path:
    """LEGACY — the dev-only fallback. cwd/env policy discovery, kept for
    `rya dev` and the existing single-project CLI, and for nothing else. Under the
    platform model a worker's cwd is an artefact of bundle extraction and ambient
    env is what D8 removes: platform callers pass a source explicitly."""
    if path:
        return Path(path)
    env = os.environ.get("RYA_GUARD_PATH")
    return Path(env) if env else Path.cwd() / GUARD_FILE


# Kept as an alias: older call sites (and tests) referenced this name.
_policy_path = _legacy_ambient_path


def _from_file(path: Path) -> GuardPolicy:
    if not path.is_file():
        # ABSENT policy = no policy = no-op. This is what makes the guard
        # strictly opt-in and backward compatible. Contrast _from_store below:
        # absence is a choice, an unreadable-but-present policy is a failure.
        return NO_POLICY
    try:
        blob = path.read_bytes()
        loaded = yaml.safe_load(blob.decode()) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        return _closed(f"policy file {path.name} is unreadable: {type(e).__name__}: {e}",
                       f"file:{path}")
    if not isinstance(loaded, dict):
        return _closed(f"policy file {path.name} is not a mapping", f"file:{path}")
    # No mtime anywhere: the etag of the bytes IS the version, so a dev edit-and-save
    # takes effect on the next request (hot reload preserved) while the compiled
    # rules are still built once per distinct policy content.
    return _build(loaded, source=f"file:{path}",
                  version=hashlib.sha256(blob).hexdigest()[:16])


def _from_store(store: Any, key: str) -> GuardPolicy:
    try:
        raw = store.policy_get(key)
    except Exception as e:  # ANY read failure is a governance failure, not a warning
        return _closed(f"policy store read failed: {type(e).__name__}: {e}", SOURCE_STORE)
    if raw is None:
        # Nothing provisioned for this workspace yet — fall through to the dev
        # file so `rya dev` against a store keeps working. Absence is NOT a
        # failure, so this must not fail closed.
        return _from_file(_legacy_ambient_path())
    if not isinstance(raw, dict):
        return _closed("policy record is not a JSON object", SOURCE_STORE)
    # Liberal about the envelope: {"version":…, "policy":{…}} as written by
    # save_policy, or a bare policy dict from a hand-seeded row.
    policy = raw.get("policy", raw)
    if not isinstance(policy, dict):
        return _closed("policy record has no usable `policy` object", SOURCE_STORE)
    version = raw.get("version")
    return _build(policy, source=SOURCE_STORE,
                  version=str(version) if version else None)


def resolve_policy(source: PolicySource = None, *, key: str = POLICY_KEY) -> GuardPolicy:
    """Resolve ``source`` to a :class:`GuardPolicy`. Never returns None.

    ``source`` may be a GuardPolicy (returned as-is), a loaded dict, a path, a
    store handle exposing ``policy_get``, or None for the legacy ambient
    fallback."""
    if isinstance(source, GuardPolicy):
        return source
    if isinstance(source, dict):
        return _build(source, source=SOURCE_EXPLICIT)
    if isinstance(source, (str, Path)):
        return _from_file(Path(source))
    if source is not None and hasattr(source, "policy_get"):
        return _from_store(source, key)
    # Either no source at all (LEGACY ambient lookup — see the module docstring) or
    # a store handle that predates the policy protocol, which degrades to the file
    # fallback rather than failing, so this seam can land before the store side.
    return _from_file(_legacy_ambient_path())


def effective_policy(store: Any, agent: str | None = None, *,
                     guard_file: str | Path | None = None) -> GuardPolicy:
    """The policy ``agent`` is ACTUALLY governed by — the read-only half of
    ``api.app._guard_source``.

    Store row first (that is what ``PUT /guard`` writes and what the egress
    checker resolves), then the project file for a single-agent tree that has
    never written one. The two resolvers must agree on that precedence, because
    when they disagree a dashboard describes a policy the runtime is not using:
    audit §4.5, where `snapshot._governance` read `rya.guard.yaml` unconditionally
    and so reported the file's allowlist — or "not configured", on a published
    bundle that ships no file — while the store's policy did the enforcing.

    ``_guard_source`` stays separate rather than calling this: it answers the
    harder WRITE question, of which file a write is permitted to touch at all.
    """
    if hasattr(store, "policy_get"):
        key = store_key_for(store, agent)
        try:
            if store.policy_get(key) is not None:
                return resolve_policy(store, key=key)
        except Exception as e:
            # A read failure is a governance failure. Fail closed rather than
            # describing the file as though it were the thing in force.
            return _closed(f"policy store read failed: {type(e).__name__}: {e}",
                           SOURCE_STORE)
    return _from_file(Path(guard_file)) if guard_file is not None else NO_POLICY


def load_policy(path: str | None = None, source: PolicySource = None) -> dict | None:
    """The raw policy dict, or ``None`` when no policy exists anywhere (no-op guard).

    Compatibility shim over :func:`resolve_policy`: prefer ``resolve_policy`` in
    new code, because ``None`` here cannot distinguish "opt-out" from anything
    else and the dict carries no provenance. A present-but-unreadable policy
    returns a deny-everything dict, never ``None``."""
    gp = resolve_policy(source if source is not None else path)
    return None if not gp.enforced else gp.policy


def save_policy(policy: dict, path: str | None = None, *,
                source: PolicySource = None, actor: str | None = None,
                key: str = POLICY_KEY) -> dict:
    """Write a new policy version and return its **audit record**.

    The record is the JSON-able envelope handed to ``policy_set(POLICY_KEY, …)``
    and is also what an append-only policy log should store:

        {"key": "guard", "version": <etag>, "previousVersion": <etag|None>,
         "actor": <str|None>, "changedAt": <iso8601 Z>, "policy": {…},
         "diff": {"added": [...], "removed": [...], "changed": [...]}}

    §12 risk 7: once allowlists leave the client's pull request, "who changed this
    and what did it look like before" has to be answerable from the platform."""
    target = source if source is not None else path
    new = _normalize(policy)

    store = target if (target is not None and not isinstance(target, (str, Path, dict))
                       and hasattr(target, "policy_set")) else None
    if store is not None:
        prev = resolve_policy(store, key=key)
        record = _audit_record(prev.policy if prev.enforced else None, prev.version if prev.enforced else None,
                              new, actor, key=key)
        # `actor` is advisory: a store that predates it still gets the actor inside
        # the record. Sniffed rather than try/except TypeError so a TypeError raised
        # *inside* policy_set can never cause a duplicate write.
        if _accepts_actor(store.policy_set):
            store.policy_set(key, record, actor=actor)
        else:
            store.policy_set(key, record)
        return record

    # LEGACY file fallback (dev). Still returns the same audit record so the CLI
    # and the API report a version either way; no store means no append-only log.
    p = Path(target) if isinstance(target, (str, Path)) else _legacy_ambient_path()
    before = _from_file(p)
    record = _audit_record(before.policy if before.enforced else None,
                           before.version if before.enforced else None, new, actor, key=key)
    p.write_text(yaml.safe_dump(policy, sort_keys=False))
    record["path"] = str(p)
    return record


def _accepts_actor(fn) -> bool:
    import inspect
    try:
        return "actor" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True


def _rule_id(rule: dict) -> str:
    if not isinstance(rule, dict):
        return json.dumps(rule, default=str, sort_keys=True)
    methods = ",".join(sorted(m.upper() for m in (rule.get("methods") or []))) or "*"
    return f"{rule.get('action', 'allow')} {rule.get('kind', 'glob')} {rule.get('pattern', '')} [{methods}]"


def _audit_record(before: dict | None, before_version: str | None,
                  after: dict, actor: str | None, *, key: str = POLICY_KEY) -> dict:
    """{version, actor, changedAt, diff} + the new policy. Plain JSON-able dict."""
    old_rules = {_rule_id(r) for r in ((before or {}).get("rules") or [])}
    new_rules = {_rule_id(r) for r in (after.get("rules") or [])}
    changed = sorted(k for k in set(after) | set(before or {})
                     if k != "rules" and (before or {}).get(k) != after.get(k))
    return {
        "key": key,
        "version": _etag(after),
        "previousVersion": before_version,
        "actor": actor,
        "changedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "policy": after,
        "diff": {"added": sorted(new_rules - old_rules),
                 "removed": sorted(old_rules - new_rules),
                 "changed": changed},
    }


# ---- egress evaluation ------------------------------------------------------

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
    """Uncompiled single-rule match. Kept for callers that hold a bare rule dict;
    the hot path uses the per-version compiled matchers instead."""
    return _compile_matcher(rule).matches(url, method)


def evaluate(url: str, method: str, policy: PolicySource) -> dict:
    """Return ``{decision: 'allow'|'block', reason, rule?, policy}`` for one request.

    ``policy`` is any policy source; passing a resolved :class:`GuardPolicy`
    skips the (cheap, but not free) re-hash and is what the hot path does."""
    gp = policy if isinstance(policy, GuardPolicy) else resolve_policy(policy)

    if gp.error:
        # Fail closed: the policy exists but we could not read it (D7).
        return {"decision": "block", "reason": f"policy unavailable — failing closed ({gp.error})",
                "policy": gp.describe()}

    prov = gp.describe()
    if gp.policy.get("ssrf", True) and is_ssrf(_host(url)):
        return {"decision": "block", "reason": "SSRF blocklist (private/loopback/metadata host)",
                "policy": prov}

    for m in gp.rules.deny:
        if m.matches(url, method):
            return {"decision": "block", "reason": m.rule.get("note") or "matched a deny rule",
                    "rule": m.rule, "policy": prov}
    for m in gp.rules.allow:
        if m.matches(url, method):
            return {"decision": "allow", "reason": m.rule.get("note") or "matched an allow rule",
                    "rule": m.rule, "policy": prov}

    if gp.policy.get("default", "deny") == "allow":
        return {"decision": "allow", "reason": "default allow", "policy": prov}
    return {"decision": "block", "reason": "default deny (not in the allowlist)", "policy": prov}


def check_egress(url: str, method: str = "POST", policy: PolicySource = None, *,
                 path: str | None = None) -> None:
    """Enforce the policy on one outbound request. Raises if blocked.

    ``policy`` is the injected source (dict / GuardPolicy / store / path). With
    neither ``policy`` nor ``path`` this falls back to the legacy ambient lookup —
    every platform call site is expected to pass a source."""
    gp = resolve_policy(policy if policy is not None else path)
    if not gp.enforced:
        return  # no policy anywhere → guard is opt-in → no-op
    result = evaluate(url, method, gp)
    if result["decision"] == "block":
        raise RyaError("E_EGRESS_BLOCKED",
                       f"Action Guard blocked {method} {url} — {result['reason']} "
                       f"[policy {gp.source}@{gp.version}].",
                       hint="Add an allow rule to the guard policy, or fix the request.")


# --- policy self-test (drives the console metrics) ------------------------
def run_tests(policy: PolicySource = None) -> dict:
    """Probe the policy with benign + attack requests and score it."""
    gp = policy if isinstance(policy, GuardPolicy) else resolve_policy(policy)
    cases = []
    # Benign: each allow rule should pass with a conforming request.
    for m in gp.rules.allow:
        r = m.rule
        cases.append({"label": "benign", "url": _example_url(r),
                      "method": (r.get("methods") or ["GET"])[0], "expect": "allow"})
    # Attacks: each deny rule should block; plus fixed exfil/SSRF probes.
    for m in gp.rules.deny:
        cases.append({"label": "attack", "url": _example_url(m.rule), "method": "POST",
                      "expect": "block"})
    for url in ("https://webhook.site/abc", "http://169.254.169.254/latest/meta-data/",
                "http://localhost:8888/admin", "https://api.stripe.com/v1/charges"):
        cases.append({"label": "attack", "url": url, "method": "POST", "expect": "block"})

    passed = attacks_blocked = attacks_total = benign_false_blocks = benign_total = 0
    for c in cases:
        got = evaluate(c["url"], c["method"], gp)["decision"]
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
        "policy": gp.describe(),
        "cases": cases,
    }


def _example_url(rule: dict) -> str:
    pat = rule.get("pattern", "https://example.com/")
    if rule.get("kind") == "exact":
        return pat
    url = pat.replace("*", "x")
    return url + ("probe" if url.endswith("/") else "/probe")


# ---- grounding gate ---------------------------------------------------------
# Serving-path check (a pattern proven in production concierge agents): every
# money figure in an outbound reply must be traceable to a tool output of the
# same run, so the model can never invent a price. Opt in via `grounding.enabled`
# in the policy; also callable directly as ctx.guard.check_grounding(text).

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
    blob = json.dumps(obj, default=str)
    return {float(n.replace(",", "")) for n in _NUM_RE.findall(blob)}


def grounding_policy(source: PolicySource = None) -> dict:
    """The ``grounding`` block of the resolved policy (``{}`` when absent).

    An unreadable policy reports the gate as ENABLED — failing closed here means
    checking, not skipping."""
    gp = resolve_policy(source)
    if gp.error:
        return {"enabled": True, "error": gp.error}
    return gp.policy.get("grounding") or {}


def grounding_check(text: str, tool_outputs: list, policy: PolicySource = None) -> dict:
    """Check that every money figure in ``text`` appears in ``tool_outputs``.

    Returns ``{ok, figures, violations, enabled}``. ``enabled`` reflects
    ``grounding.enabled`` in ``policy`` so a caller needs one call rather than two;
    with no ``policy`` argument it is ``True``, because the caller asked for the
    check directly (``ctx.guard.check_grounding``) and the check always runs."""
    enabled = True if policy is None else bool(grounding_policy(policy).get("enabled"))
    figures = _money_values(text)
    if not figures:
        return {"ok": True, "figures": [], "violations": [], "enabled": enabled}
    grounded: set = set()
    for out in tool_outputs or []:
        grounded |= _all_numbers(out)
    violations = [f for f in figures if f not in grounded]
    return {"ok": not violations, "figures": figures, "violations": violations,
            "enabled": enabled}


# ---- id-secrecy scrub -------------------------------------------------------
# Some tool outputs carry ids that must never reach the model or land in an
# outbound message (e.g. an external CRM's internal alphanumeric "master id",
# which the model must never confuse with a numeric account id). Unlike
# grounding — a check that BLOCKS — secrecy SCRUBS: it rewrites the offending
# substrings to a safe token and lets the result through, so the model keeps
# working with a redacted value instead of failing the turn. It applies at the
# tool boundary (before the model sees a result) and on outbound (before bytes
# leave). Opt in via `secrecy.enabled`; also callable as ctx.guard.check_secrecy(text).


def _compile_secrecy(policy: PolicySource = None) -> list:
    """Compiled secrecy patterns — ``[(regex, replacement, id)]``, ``[]`` when
    disabled/absent (so the scrub is a pure no-op).

    ``policy`` is any policy source; ``None`` means "no policy", i.e. no-op —
    it does NOT fall back to the ambient file, because a scrub that silently
    starts reading a stray file in cwd is exactly the D8 bug. The compile itself
    happens once per policy version, not per call."""
    if policy is None:
        return []
    return list(resolve_policy(policy).rules.secrecy)


def _as_compiled(compiled) -> list:
    """Accept either an already-compiled pattern list or a policy source, so the
    scrub helpers can be handed a policy directly."""
    if isinstance(compiled, (list, tuple)):
        return list(compiled)
    return _compile_secrecy(compiled)


def secrecy_scrub_text(text: str, compiled) -> tuple:
    """Apply every compiled pattern to one string. Returns ``(scrubbed, hits)``
    where hits is the list of pattern ids that matched. ``compiled`` is a compiled
    list or any policy source."""
    compiled = _as_compiled(compiled)
    if not text or not compiled:
        return text, []
    hits: list = []
    out = text
    for rx, repl, pid in compiled:
        def _sub(_m, _pid=pid, _repl=repl):
            hits.append(_pid)
            return _repl
        out = rx.sub(_sub, out)
    return out, hits


def secrecy_scrub(obj, compiled):
    """Recursively scrub every string LEAF of a tool result (dict/list/str/other).

    Scrubs values only, never dict keys (field names are structure, not payload),
    and returns non-string leaves untouched. Operating on the parsed object — not a
    serialized JSON blob — keeps the replacement token from ever corrupting JSON."""
    compiled = _as_compiled(compiled)
    return _scrub(obj, compiled)


def _scrub(obj, compiled: list):
    if not compiled:
        return obj
    if isinstance(obj, str):
        return secrecy_scrub_text(obj, compiled)[0]
    if isinstance(obj, dict):
        return {k: _scrub(v, compiled) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v, compiled) for v in obj]
    return obj


def secrecy_check(text: str, policy: PolicySource = None) -> dict:
    """Report whether any secret pattern appears in ``text`` (no mutation of the
    caller's value beyond the returned ``scrubbed``). Returns ``{ok, hits, scrubbed}``."""
    compiled = _compile_secrecy(policy)
    scrubbed, hits = secrecy_scrub_text(text or "", compiled)
    return {"ok": not hits, "hits": sorted(set(hits)), "scrubbed": scrubbed}
