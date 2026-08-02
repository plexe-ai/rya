"""Credential inventory — proving what a process holds (Phase 4 exit criterion 1).

The exit criterion is "a credential inventory proves the tenant process environment
and memory contain **no** DB DSN, seal key, provider key or bucket credential". This
module is that inventory, and it is the same list the scrub uses, on purpose: a
scrubber and an auditor working from two lists is how a credential ends up removed
in one build and reported clean in the next.

**The distinction that makes this useful rather than noisy.** Not every secret in a
tenant process is a D18 violation. D18 removes the *platform's* credentials — the
database DSN, the seal key, the pooled provider key, the object-store credential.
A secret the **tenant declared for its own handler** is theirs; `ctx.secrets.get`
exists to hand it over, and reporting it would make the inventory fail on every
deployment and therefore be ignored. So findings are classified:

``platform``
    A credential belonging to the deployment. In a mediated process this is a
    **violation**.
``tenant``
    A value the tenant supplied. Reported as present, never as a violation.
``ambiguous``
    Looks like a credential by shape and is not on either list. Reported for a human,
    because the alternative — silence — is how the next ``FOO_API_KEY`` gets missed.

**What this cannot prove.** Freed heap. A string that was in ``os.environ`` before
the scrub, or that arrived in a ``RunConfig`` and was replaced, may still be resident
in this process's memory, and CPython offers no way to overwrite it. So the inventory
proves a property of the *reachable* state, and the untrusted posture does not rely
on the scrub for its guarantee — it relies on the sandbox's environment being
constructed without the values in the first place, which is why
``require_untrusted_posture`` demands a driver that can do that. Stated here rather
than in a doc because this is the module someone will read when they want to know how
strong the claim is.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional

# ---- the platform's credentials, by exact name ------------------------------
# Grouped so a finding can say WHICH of the four things the criterion names it is.

DSN_VARS = ("RYA_DATABASE_URL", "DATABASE_URL", "RYA_ADMIN_DATABASE_URL",
            "RYA_APP_DATABASE_URL", "RYA_WORKER_DATABASE_URL", "PGPASSWORD")
SEAL_VARS = ("RYA_SECRET_KEY", "RYA_KMS_KEY_ID")
PROVIDER_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "RYA_PLATFORM_TOKEN",
                 "RYA_LLM_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
                 "AZURE_OPENAI_API_KEY", "COHERE_API_KEY", "MISTRAL_API_KEY")
BUCKET_VARS = ("AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN",
               "RYA_S3_SECRET_KEY", "RYA_S3_ACCESS_KEY", "MINIO_SECRET_KEY",
               "MINIO_ROOT_PASSWORD")
# Not a credential to a provider or a bucket, but authority over the control plane —
# which is worse. An admin token in a tenant process can raise the tenant's own quota.
ADMIN_VARS = ("RYA_ADMIN_TOKEN", "RYA_API_KEY", "RYA_USER_TOKEN_SECRET")
# D32's control surface. Not a credential to anything outside the sandbox — the
# template host holds nothing and can grant nothing — so the harm is bounded to
# availability: a tenant that can drive the host can stop its own sandbox serving,
# import bundles into it, or evict a sibling agent's warm interpreter. That is a
# cross-agent effect within one tenant, which is exactly the class D22 and D33 close
# elsewhere, so it is scrubbed on the same pass as the real credentials rather than
# being left in the ambiguous bucket its name would otherwise put it in.
HOST_VARS = ("RYA_TEMPLATE_HOST_TOKEN",)

PLATFORM_GROUPS: Dict[str, tuple] = {
    "dsn": DSN_VARS,
    "sealKey": SEAL_VARS,
    "providerKey": PROVIDER_VARS,
    "bucketCredential": BUCKET_VARS,
    "adminToken": ADMIN_VARS,
    "templateHostToken": HOST_VARS,
}

PLATFORM_VARS = frozenset(v for group in PLATFORM_GROUPS.values() for v in group)

# Variables a mediated process legitimately holds. Named explicitly so the scrub does
# not remove the thing that makes mediation work, and so the inventory does not report
# it. RYA_BROKER_CAPABILITY is a credential — a short-lived, run-scoped, unforgeable
# one, which is the whole point of it being different from a DSN.
MEDIATION_VARS = frozenset({
    "RYA_BROKER_SOCKET", "RYA_BROKER_CAPABILITY", "RYA_BROKER",
    "RYA_ENVIRONMENT", "RYA_WORKSPACE", "RYA_PROJECT", "RYA_AGENT",
    # D32: a path, not a secret. The token that makes it usable is on the platform
    # list above, so a template that keeps this one after the scrub knows where the
    # host is and cannot ask it for anything.
    "RYA_TEMPLATE_HOST",
})

# Shape-based detection for the ambiguous bucket. Deliberately conservative: a false
# positive here costs an operator ten seconds of reading, and a false negative is a
# credential nobody knew was there.
_SUSPICIOUS_NAME = re.compile(
    r"(SECRET|PASSWORD|PASSWD|TOKEN|API_?KEY|PRIVATE_?KEY|CREDENTIAL|DSN)",
    re.IGNORECASE)
# `postgres://user:pass@host` and friends — a connection string is a credential even
# when the variable is called something innocuous.
_DSN_SHAPE = re.compile(r"^[a-z0-9+]+://[^/\s:@]+:[^/\s@]+@", re.IGNORECASE)

CLASS_PLATFORM = "platform"
CLASS_TENANT = "tenant"
CLASS_AMBIGUOUS = "ambiguous"
CLASS_MEDIATION = "mediation"


@dataclass
class Finding:
    """One credential-shaped thing, where it was, and whether it is a problem."""

    name: str
    where: str                  # "env" | "config.secrets" | "config.routes" | "store"
    classification: str
    group: str = ""
    detail: str = ""

    @property
    def violation(self) -> bool:
        return self.classification == CLASS_PLATFORM

    def describe(self) -> dict:
        return {"name": self.name, "where": self.where, "class": self.classification,
                "group": self.group or None, "detail": self.detail or None,
                "violation": self.violation}


@dataclass
class Inventory:
    """Everything found, and the one question a caller actually asks."""

    findings: List[Finding] = field(default_factory=list)
    mediated: bool = False

    @property
    def violations(self) -> List[Finding]:
        return [f for f in self.findings if f.violation]

    @property
    def clean(self) -> bool:
        """True when this process holds none of the platform's credentials.

        The property the exit criterion asks for, and it is deliberately independent
        of ``mediated``: a *trusted* worker holding a DSN is correct and this still
        reports it, because "which posture is this process in" is a separate question
        from "what does it hold". Conflating them would make the inventory unable to
        show the difference between the two postures, which is most of its value.
        """
        return not self.violations

    def describe(self) -> dict:
        return {"mediated": self.mediated, "clean": self.clean,
                "violations": [f.describe() for f in self.violations],
                "findings": [f.describe() for f in self.findings],
                "counts": {c: sum(1 for f in self.findings if f.classification == c)
                           for c in (CLASS_PLATFORM, CLASS_TENANT,
                                     CLASS_AMBIGUOUS, CLASS_MEDIATION)}}


def _group_of(name: str) -> str:
    for group, names in PLATFORM_GROUPS.items():
        if name in names:
            return group
    return ""


def classify(name: str, value: Any = None, *,
             tenant_names: Iterable[str] = ()) -> tuple:
    """``(classification, group)`` for one variable.

    Order matters and it is the security-relevant part. The platform list is checked
    **first**, so a tenant cannot launder a platform credential into the tenant class
    by declaring a secret of the same name in its own manifest.
    """
    if name in PLATFORM_VARS:
        return CLASS_PLATFORM, _group_of(name)
    if name in MEDIATION_VARS:
        return CLASS_MEDIATION, ""
    if isinstance(value, str) and _DSN_SHAPE.match(value):
        # A connection string is a DSN whatever it is called. This is the rule that
        # catches the variable nobody added to the list above.
        return CLASS_PLATFORM, "dsn"
    if name in set(tenant_names):
        return CLASS_TENANT, ""
    if _SUSPICIOUS_NAME.search(name):
        return CLASS_AMBIGUOUS, ""
    return "", ""


def inspect_environment(env: Optional[Mapping[str, str]] = None, *,
                        tenant_names: Iterable[str] = ()) -> List[Finding]:
    env = env if env is not None else os.environ
    out: List[Finding] = []
    for name, value in sorted(env.items()):
        cls, group = classify(name, value, tenant_names=tenant_names)
        if not cls:
            continue
        out.append(Finding(name=name, where="env", classification=cls, group=group))
    return out


def inspect_config(config) -> List[Finding]:
    """Walk a ``RunConfig`` for credentials the tenant process can read.

    ``routes[*].api_key`` is the D30 one and the reason this function exists: a
    provider key does not have to be in the environment to be in the process, and
    ``resolve_run_config`` puts it on the route object. An inventory that only read
    ``os.environ`` would report a mediated process clean while it held the pooled key
    on an attribute.
    """
    out: List[Finding] = []
    if config is None:
        return out
    for name in sorted(dict(getattr(config, "secrets", None) or {})):
        out.append(Finding(name=name, where="config.secrets",
                           classification=CLASS_TENANT,
                           detail="declared by the tenant; ctx.secrets.get exists to serve it"))
    for route_name, route in sorted((dict(getattr(config, "routes", None) or {})).items()):
        if getattr(route, "api_key", ""):
            out.append(Finding(name=f"routes[{route_name or 'default'}].api_key",
                               where="config.routes", classification=CLASS_PLATFORM,
                               group="providerKey",
                               detail="a resolved ModelRoute carries its credential; "
                                      "under D30 the pooled key must stay broker-side"))
        if getattr(route, "base_url", ""):
            out.append(Finding(name=f"routes[{route_name or 'default'}].base_url",
                               where="config.routes", classification=CLASS_AMBIGUOUS,
                               detail="an endpoint, not a secret — reported because a "
                                      "governance URL is an egress target"))
    return out


def inspect_store(store) -> List[Finding]:
    """Whether this process holds a live database credential.

    Asks the store what it is rather than sniffing for a DSN string, because the
    honest answer is structural: a ``BrokerStore`` has a socket, a ``PostgresStore``
    has a connection. ``describe()`` is the method every store already implements for
    exactly this kind of question.
    """
    out: List[Finding] = []
    if store is None:
        return out
    try:
        info = store.describe() or {}
    except Exception:  # noqa: BLE001 - an inventory must not fail on a bad store
        info = {}
    kind = str(info.get("kind") or type(store).__name__)
    if kind == "broker":
        return out
    if getattr(store, "dsn", None) or kind.lower().startswith("postgres"):
        out.append(Finding(name="store.dsn", where="store",
                           classification=CLASS_PLATFORM, group="dsn",
                           detail=f"a live {kind} connection in this process"))
    return out


def take_inventory(*, env: Optional[Mapping[str, str]] = None, config=None,
                   store=None, mediated: Optional[bool] = None,
                   tenant_names: Iterable[str] = ()) -> Inventory:
    """The whole picture for one process. What ``rya doctor credentials`` reports."""
    env = env if env is not None else os.environ
    if mediated is None:
        mediated = bool(env.get("RYA_BROKER_SOCKET"))
    findings = (inspect_environment(env, tenant_names=tenant_names)
                + inspect_config(config) + inspect_store(store))
    return Inventory(findings=findings, mediated=bool(mediated))


def scrub_environment(env: MutableMapping[str, str]) -> List[str]:
    """Remove the platform's credentials in place. Returns the names removed.

    Called by the template process before it imports the tenant's bundle. Uses the
    same classification as the audit, so the two cannot disagree — the failure mode
    this closes is a variable added to one list and not the other, which is silent in
    both directions.

    Ambiguous names are **not** removed. A tenant that named its own secret
    ``STRIPE_API_KEY`` would otherwise find it missing at runtime with no explanation,
    and a shape-based heuristic is not a good enough reason to break a handler. They
    are reported by the inventory instead, where a human decides.
    """
    removed: List[str] = []
    for name in list(env.keys()):
        cls, _group = classify(name, env.get(name))
        if cls == CLASS_PLATFORM:
            env.pop(name, None)
            removed.append(name)
    return sorted(removed)
