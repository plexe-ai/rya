"""Network-layer egress enforcement (D24) — what can physically leave a sandbox.

``guard.py`` decides *what is allowed*. This module decides *what can leave*, and
the difference is the whole of D24: an in-process allowlist is bypassed by any code
that does not call it, so against a tenant that imports ``urllib`` directly it
enforces nothing at all. The mechanism has to sit where the tenant's code is not.

**Deny-by-default, with one mediated hole.**

::

    sandbox (no route out)  ──unix socket──►  broker  ──►  allowlisted hosts
        raw urllib: fails at connect()          egress.fetch: policy applies

A sandbox gets no network namespace route (`docker --network none`, a k8s
``NetworkPolicy`` with no egress rule). A handler's raw request therefore fails at
``connect()`` — refused by the kernel, not by a Python check — and the only way out
is ``ctx``, which reaches the broker, which lands here. That is the exit criterion
"blocked by the network, with ``guard.py`` recording the attempt": the network
provides the enforcement, and the guard verdict is still evaluated and still
recorded, so the audit trail is unchanged from the trusted posture.

**Two verdicts, and why the divergence is real rather than ceremonial.**

The network posture is a **snapshot**. A sandbox's egress rules are computed when it
is created — the hosts are resolved, the rules are written into the substrate — and
the policy in ``rya_policy`` is *live*. Promote a new guard policy and the two
disagree until every sandbox has been recycled. That is not a hypothetical: it is
the ordinary consequence of a policy change, and it is the single most likely way
for "the allowlist says yes and the network says no" (or worse, the reverse) to
happen in production.

So every mediated request is evaluated against both, and the result is:

* both allow → the request goes out
* either blocks → refused, **fail closed**, and the refusal names which one
* they disagree → the above, plus a recorded divergence with both etags

Fail-closed on disagreement is the only defensible answer. Trusting the live policy
would let a request out through a network the operator has not yet permitted;
trusting the snapshot would keep enforcing a rule the operator has already revoked.
Refusing does neither and makes the operator's next action obvious.

**The local arm enforces nothing, and says so.** With the `local` driver there is no
network namespace to restrict, so ``NetworkPosture.enforced`` is False and this
module is a policy-and-audit path exactly like ``guard.py``. That is honest rather
than degraded: `local` declares ``isolation="none"`` and the untrusted posture
already refuses it (D26).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .errors import RyaError

log = logging.getLogger("rya.egress")

E_DENIED = "E_EGRESS_DENIED"
E_UNAVAILABLE = "E_EGRESS_UNAVAILABLE"

MODE_ENV = "RYA_EGRESS"          # "none" | "proxy" | "netpolicy"
PROXY_ENV = "RYA_EGRESS_PROXY"   # an operator's own forward proxy, if they have one

MODE_NONE = "none"          # no network restriction available (local driver)
MODE_PROXY = "proxy"        # the sandbox has no route; the broker fetches
MODE_NETPOLICY = "netpolicy"  # the substrate enforces (k8s NetworkPolicy, docker net)
MODES = (MODE_NONE, MODE_PROXY, MODE_NETPOLICY)

DEFAULT_PORTS = (443, 80)
MAX_BODY_BYTES = 8 * 1024 * 1024


# ---- the snapshot applied to a substrate -----------------------------------

@dataclass(frozen=True)
class NetworkPosture:
    """What a sandbox's network was actually configured to permit.

    ``etag`` is the guard policy's etag at the moment the posture was computed, and
    it is the field that makes divergence detectable: comparing it against the live
    policy's etag answers "is this sandbox enforcing the current allowlist" without
    having to diff rule sets.

    ``hosts`` are names, not addresses, deliberately. Resolving to IPs at snapshot
    time is what a ``NetworkPolicy`` ultimately needs, and it is also how an
    allowlist silently stops matching when a CDN rotates — so the names are the
    record of intent and the resolution is reported separately by
    :meth:`resolve_hosts`, where a change is visible.
    """

    mode: str = MODE_NONE
    hosts: Tuple[str, ...] = ()
    ports: Tuple[int, ...] = DEFAULT_PORTS
    etag: str = ""
    policy_version: str = ""

    @property
    def enforced(self) -> bool:
        """Whether the substrate is actually restricting anything.

        ``MODE_NONE`` means it is not — the `local` driver has no namespace to
        restrict — and callers must be able to say that plainly rather than reporting
        an unenforced posture as protection.
        """
        return self.mode != MODE_NONE

    def permits(self, url: str) -> Tuple[bool, str]:
        """Would the *network* have let this out? ``(allowed, reason)``.

        Evaluated on host and port only, because that is all a network layer can
        see. A path-scoped allow rule in the guard policy is therefore strictly
        finer-grained than the network can enforce, which is one of the two honest
        reasons the two verdicts differ (the other being staleness).
        """
        if not self.enforced:
            return True, "no network restriction is in force (local driver)"
        host, port = _host_port(url)
        if not host:
            return False, "no host in the request"
        if host not in self.hosts:
            return False, f"host '{host}' is not in the sandbox's egress allowlist"
        if self.ports and port not in self.ports:
            return False, f"port {port} is not in the sandbox's egress allowlist"
        return True, f"host '{host}' and port {port} are permitted"

    def resolve_hosts(self) -> Dict[str, List[str]]:
        """Names to addresses, for a substrate that needs CIDRs.

        Failures are recorded as an empty list rather than raised: a host that does
        not resolve should produce a *narrower* network rule and a visible gap, not a
        sandbox that fails to start.
        """
        out: Dict[str, List[str]] = {}
        for host in self.hosts:
            try:
                infos = socket.getaddrinfo(host, None)
            except OSError:
                out[host] = []
                continue
            addrs = sorted({str(i[4][0]) for i in infos})
            out[host] = [a for a in addrs if not _is_private(a)]
        return out

    def describe(self) -> dict:
        return {"mode": self.mode, "enforced": self.enforced,
                "hosts": list(self.hosts), "ports": list(self.ports),
                "policyEtag": self.etag, "policyVersion": self.policy_version}


def _host_port(url: str) -> Tuple[str, int]:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return "", 0
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, int(port)


def _is_private(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:  # pragma: no cover
        return True
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast)


def posture_from_policy(policy, *, mode: str = MODE_PROXY,
                        ports: Tuple[int, ...] = DEFAULT_PORTS) -> NetworkPosture:
    """Compute the network posture a guard policy implies.

    The hosts come from the policy's **allow** rules only. Deny rules are
    deliberately not translated: a network allowlist is already deny-by-default, so a
    deny rule is either redundant at this layer or finer-grained than it can express,
    and turning "deny this path on an allowed host" into a network rule would either
    lose the path or block the host. Path-scoped denies stay with ``guard.py``, which
    is the layer that can see a path — and that asymmetry is itself one of the
    documented reasons the two verdicts legitimately differ.
    """
    from .guard import GuardPolicy, resolve_policy

    gp = policy if isinstance(policy, GuardPolicy) else resolve_policy(policy)
    hosts: List[str] = []
    for rule in (gp.policy.get("rules") or []):
        if not isinstance(rule, dict) or rule.get("action") != "allow":
            continue
        host = host_of_pattern(str(rule.get("pattern") or ""))
        if host and host not in hosts:
            hosts.append(host)
    return NetworkPosture(mode=mode, hosts=tuple(hosts), ports=tuple(ports),
                          etag=gp.etag, policy_version=str(gp.version or ""))


def host_of_pattern(pattern: str) -> str:
    """The host a guard rule's pattern is about, or "" if it names none.

    Guard patterns are URL prefixes, globs or exact URLs, so the host is not always
    parseable — ``https://*.stripe.com/v1/*`` has no hostname as far as
    ``urlsplit`` is concerned. Taking the authority segment textually is what an
    operator meant by such a rule.

    A **wildcard in the host** returns "" rather than a partial host, and that
    matters: a network allowlist cannot express "any subdomain", so pretending it
    could would silently narrow the rule to nothing while looking like it worked. The
    honest outcome is that the network layer does not carry that rule and the
    divergence reconciler reports the disagreement — which is exactly the case the
    "path-scoped rule the network cannot express" reason exists for.
    """
    raw = (pattern or "").strip()
    if not raw:
        return ""
    authority = raw.split("://", 1)[-1].split("/", 1)[0]
    authority = authority.split("@")[-1].split(":")[0].lower()
    if not authority or any(c in authority for c in "*?["):
        return ""
    return authority


# ---- the mediated fetch ------------------------------------------------------

@dataclass
class Divergence:
    """One request where the policy and the network disagreed."""

    url: str
    method: str
    guard: str          # "allow" | "block"
    network: str
    guard_etag: str
    posture_etag: str
    reason: str
    at: float = field(default_factory=time.time)

    def describe(self) -> dict:
        return {"url": self.url, "method": self.method, "guard": self.guard,
                "network": self.network, "guardEtag": self.guard_etag,
                "postureEtag": self.posture_etag, "reason": self.reason,
                "at": self.at}


class EgressService:
    """The one route out of a sandbox, and the reconciler for the two verdicts.

    Deliberately not a general forward proxy. It speaks one verb over HTTP(S) with a
    body and headers, which is what a handler reaching an allowlisted API needs, and
    nothing else — no CONNECT tunnelling, no arbitrary protocols. A tunnel would make
    the mediated hole as wide as the network it replaced, which would leave the
    allowlist enforcing hostnames and nothing about what travels over them.
    """

    def __init__(self, *, posture: NetworkPosture, policy_source=None,
                 agent: str = "", proxy: str = "",
                 max_divergences: int = 128) -> None:
        self.posture = posture
        self.policy_source = policy_source
        self.agent = agent
        self.proxy = proxy
        self.max_divergences = max_divergences
        self._divergences: List[Divergence] = []
        self.allowed = 0
        self.denied = 0

    # -- the two verdicts
    def _guard_verdict(self, url: str, method: str) -> dict:
        from .guard import evaluate, resolve_policy, store_key_for

        source = self.policy_source
        if source is not None and hasattr(source, "policy_get"):
            gp = resolve_policy(source, key=store_key_for(source, self.agent))
        else:
            gp = resolve_policy(source)
        verdict = evaluate(url, method, gp)
        verdict["etag"] = gp.etag
        verdict["enforced"] = gp.enforced
        return verdict

    def check(self, url: str, method: str = "GET") -> dict:
        """Evaluate both layers. Returns the combined verdict; never raises.

        Separated from :meth:`fetch` so the reconciliation is testable without a
        network, and so an operator tool can ask "what would happen" — which is what
        ``guard.run_tests`` already does for the policy layer alone.
        """
        guard = self._guard_verdict(url, method)
        net_allowed, net_reason = self.posture.permits(url)
        # An UNCONFIGURED guard is a no-op by design (`guard.enforced` is False when
        # no policy exists anywhere), and treating that absence as "allow" here would
        # make an unconfigured deployment look like it had agreed to the request. It
        # is reported as "unset" so a divergence against it is not raised.
        guard_decision = guard["decision"] if guard.get("enforced") else "unset"
        network_decision = "allow" if net_allowed else "block"
        diverged = (guard_decision in ("allow", "block")
                    and guard_decision != network_decision)
        allowed = guard_decision != "block" and network_decision == "allow"
        out = {"allowed": allowed, "guard": guard_decision,
               "network": network_decision, "guardReason": guard.get("reason"),
               "networkReason": net_reason, "diverged": diverged,
               "guardEtag": guard.get("etag"), "postureEtag": self.posture.etag}
        if diverged:
            self._record(url, method, out)
        return out

    def _record(self, url: str, method: str, verdict: dict) -> None:
        reason = ("the sandbox's network snapshot predates the current guard policy"
                  if verdict.get("guardEtag") != self.posture.etag else
                  "the policy and the network agree on the allowlist but not on this "
                  "request — usually a path-scoped rule the network cannot express")
        self._divergences.append(Divergence(
            url=url, method=method, guard=verdict["guard"], network=verdict["network"],
            guard_etag=str(verdict.get("guardEtag") or ""),
            posture_etag=self.posture.etag, reason=reason))
        # Bounded, dropping the OLDEST. A divergence storm is a policy change that has
        # not propagated, and the most recent entries are the ones that describe the
        # current state.
        if len(self._divergences) > self.max_divergences:
            self._divergences = self._divergences[-self.max_divergences:]
        log.warning("egress divergence: guard=%s network=%s for %s %s (%s)",
                    verdict["guard"], verdict["network"], method, url, reason)

    def divergences(self) -> List[dict]:
        """What to alert on. Empty is the healthy state and the common one."""
        return [d.describe() for d in self._divergences]

    # -- the request
    def fetch(self, url: str, *, method: str = "GET", headers=None, body=None,
              timeout: float = 30.0) -> dict:
        verdict = self.check(url, method)
        if not verdict["allowed"]:
            self.denied += 1
            which = ("both the guard policy and the sandbox's network"
                     if verdict["guard"] == "block" and verdict["network"] == "block"
                     else "the guard policy" if verdict["guard"] == "block"
                     else "the sandbox's network allowlist")
            raise RyaError(
                E_DENIED,
                f"{method} {url} was refused by {which}: "
                f"{verdict['guardReason'] if verdict['guard'] == 'block' else verdict['networkReason']}",
                hint=("The two layers disagreed, so this fails closed. Recycle the "
                      "sandbox to pick up the current policy, or check "
                      "`rya guard divergences`."
                      if verdict["diverged"] else
                      "Add an allow rule to the guard policy; the sandbox's network "
                      "rules are derived from it when the sandbox starts."),
            )
        self.allowed += 1
        return self._request(url, method=method, headers=headers, body=body,
                             timeout=timeout)

    def _request(self, url: str, *, method: str, headers, body, timeout: float) -> dict:
        data = None
        if body is not None:
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method.upper(),
                                     headers={k: str(v) for k, v in (headers or {}).items()})
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
            if self.proxy else urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read(MAX_BODY_BYTES + 1)
                if len(raw) > MAX_BODY_BYTES:
                    raise RyaError(
                        E_DENIED,
                        f"The response from {url} exceeded {MAX_BODY_BYTES} bytes.",
                        hint="Mediated egress carries results, not streams.")
                return {"status": resp.status, "headers": dict(resp.headers),
                        "body": raw.decode(errors="replace")}
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode(errors="replace")[:4000]
            # An upstream status is DATA, not a failure of the egress layer. Raising
            # here would make a 404 from an allowlisted API indistinguishable from a
            # policy refusal, which is exactly the confusion the two error codes exist
            # to avoid.
            return {"status": exc.code, "headers": dict(exc.headers or {}),
                    "body": payload}
        except urllib.error.URLError as exc:
            raise RyaError(
                E_UNAVAILABLE, f"{method} {url} could not be reached: {exc.reason}",
                hint="The request passed both allowlists and the connection still "
                     "failed, so this is the upstream or DNS, not policy.") from exc

    def describe(self) -> dict:
        return {"posture": self.posture.describe(), "allowed": self.allowed,
                "denied": self.denied, "divergences": len(self._divergences),
                "proxy": self.proxy or None}


# ---- resolution -------------------------------------------------------------

def resolve_egress(*, store=None, agent: str = "",
                   env: Optional[Mapping[str, str]] = None,
                   posture: Optional[NetworkPosture] = None) -> EgressService:
    """The declared egress service. Same seam shape as the other four.

    Defaults to ``MODE_NONE`` — no network restriction — because that is the truth on
    a laptop and on the `local` driver, and a default that *claimed* enforcement
    would be the overclaim D24 exists to remove. The sandboxed drivers set
    ``RYA_EGRESS`` when they create a sandbox whose network they actually restricted.
    """
    env = env if env is not None else os.environ
    mode = (env.get(MODE_ENV) or MODE_NONE).strip().lower()
    if mode not in MODES:
        raise RyaError(
            E_UNAVAILABLE, f"No egress mode named '{mode}'.",
            hint=f"One of: {', '.join(MODES)}. '{MODE_NONE}' means the substrate "
                 "restricts nothing, which is the honest answer for the local driver.")
    if posture is None:
        posture = posture_from_policy(store, mode=mode) if mode != MODE_NONE \
            else NetworkPosture(mode=MODE_NONE)
    return EgressService(posture=posture, policy_source=store, agent=agent,
                         proxy=(env.get(PROXY_ENV) or "").strip())


def reconcile(services) -> dict:
    """Roll up divergences across several egress services.

    Exists because a deployment runs one of these per claimer, so "is any sandbox
    enforcing a stale allowlist" is a question about the fleet rather than about one
    process. The answer an operator wants is a count and one example, not a log.
    """
    rows: List[dict] = []
    for service in services:
        rows.extend(service.divergences())
    stale = [r for r in rows if r["guardEtag"] != r["postureEtag"]]
    return {"total": len(rows), "stale": len(stale),
            "postureEtags": sorted({r["postureEtag"] for r in rows}),
            "example": rows[-1] if rows else None,
            "action": ("recycle the sandboxes whose postureEtag is not the current "
                       "policy etag" if stale else
                       "no stale sandboxes; any remaining divergence is a "
                       "path-scoped rule the network cannot express")}
