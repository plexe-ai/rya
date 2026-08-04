"""Promotion gates — readiness and evals as *admission* checks (PLATFORM_DESIGN §9).

§9: "The **readiness gate** (``readiness.py``) becomes a server-side admission
check rather than a client-side courtesy, and **evals** (``rya.evals.yaml``) can
gate promotion between staging and prod."

The distinction is the whole module. Today ``rya deploy`` runs readiness on the
client's machine and refuses locally — which stops an honest mistake and nothing
else, because the check and the decision to ship are the same process and a
``--force`` is one keystroke away. A gate moves the decision to the platform:
promotion into ``prod`` is refused unless *evidence* exists that the checks ran
and passed, against **this exact content**.

Three pieces:

1. **Gate policy** — per-environment requirements, stored as privileged platform
   state under the ``promotion`` policy key (D7: a bundle cannot write it, and
   every change lands in the append-only policy log).
2. **Attestations** — the evidence, filed against a *version id* by
   ``store.version_attest``. Because ``version_create`` is idempotent on
   ``(agent, bundleHash)``, a version id is a 1:1 handle on content, so evidence
   cannot be transplanted from one tree to another.
3. **The check** — :func:`require_promotion`, called by
   ``deployments.promote``, which refuses with ``E_PROMOTION_BLOCKED`` and names
   every unmet requirement plus the command that satisfies it.

Two deliberate asymmetries, both of which matter more than they look:

* **Rollback is never gated.** §9 calls rollback "a pointer flip", and it is the
  incident-response tool. A gate that refuses a rollback because the older
  version has no fresh eval attestation would hold an outage open — the failure
  mode is strictly worse than what the gate prevents. ``deployments.rollback``
  therefore skips the gate by construction, not by an operator remembering a
  flag.
* **An empty eval suite does not satisfy an eval gate.** A project with no
  ``rya.evals.yaml`` scores vacuously — zero cases, zero failures — and a gate
  that accepted that would be a checkbox rather than a control. ``hasEvals`` is
  checked explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from .errors import RyaError

# The privileged policy key holding gate configuration (see store.policy_set).
#
# The bare literal is the UNQUALIFIED key, correct only for a deployment serving
# one agent. `rya_policy` is keyed `(workspace_id, key)`, so before D28 two agents
# in one workspace shared one promotion gate — and "prod requires evals" is a
# statement about an agent's release process, not about a workspace.
POLICY_KEY = "promotion"


def policy_key(agent: Optional[str] = None) -> str:
    """The `rya_policy` key holding ``agent``'s promotion gate (D28)."""
    return f"{POLICY_KEY}:{agent}" if agent else POLICY_KEY


def gate_policy(store, agent: Optional[str]) -> Any:
    """``agent``'s gate policy, falling back to the pre-D28 unqualified row.

    Read-time fallback rather than a one-shot rename at upgrade, deliberately.
    A rename has to guess which agent an existing shared row belonged to, and it
    can only be right where the workspace has exactly one — precisely the case
    where the fallback is already correct. Where it has two, a rename would
    silently hand one agent's gate to whichever name sorted first and leave the
    other ungated, which for a promotion gate is a governance regression rather
    than an inconvenience.

    So the legacy row keeps enforcing for every agent that has not been given its
    own, and the first qualified write for an agent takes over for that agent
    alone. `None` (not `{}`) is the miss: an operator who deliberately cleared a
    gate must not have the legacy one resurrected under them.
    """
    if agent:
        own = store.policy_get(policy_key(agent))
        if own is not None:
            return own
    return store.policy_get(POLICY_KEY) or {}


# Attestation kinds. Kept as constants because they are matched on read.
ATTEST_READINESS = "readiness"
ATTEST_EVALS = "evals"
ATTEST_OVERRIDE = "override"

# Recognised gate fields, wire name -> attribute name. A key outside this map is
# an ERROR rather than something to ignore: a policy with `requireEval: true`
# (singular, a plausible typo) that silently resolved to an unenforced gate would
# be a governance control that reports itself as configured while enforcing
# nothing. Fail closed on what we do not understand.
_GATE_FIELDS = {
    "requireReadiness": "require_readiness",
    "requireEvals": "require_evals",
    "minEvalScore": "min_eval_score",
    "allowWarnings": "allow_warnings",
    "requireActor": "require_actor",
    "requireProvenance": "require_provenance",
}


@dataclass(frozen=True)
class PromotionGate:
    """The resolved requirements for promoting into one environment.

    Every field defaults to "not required", so an unconfigured platform behaves
    exactly as it did before gates existed — promotion is opt-in governance, not
    a breaking change to everyone's deploy.
    """

    environment: str
    require_readiness: bool = False
    require_evals: bool = False
    min_eval_score: float = 1.0
    allow_warnings: bool = True
    require_actor: bool = False
    require_provenance: tuple[str, ...] = ()
    source: str = "default"

    @property
    def enforced(self) -> bool:
        """Whether this gate demands anything at all."""
        return bool(self.require_readiness or self.require_evals
                    or self.require_actor or self.require_provenance)

    def describe(self) -> dict:
        return {
            "environment": self.environment,
            "enforced": self.enforced,
            "source": self.source,
            "requireReadiness": self.require_readiness,
            "requireEvals": self.require_evals,
            "minEvalScore": self.min_eval_score,
            "allowWarnings": self.allow_warnings,
            "requireActor": self.require_actor,
            "requireProvenance": list(self.require_provenance),
        }


@dataclass
class GateResult:
    """The outcome of evaluating a gate, check by check.

    Carries the passing checks too, not just the failures: "prod required
    readiness and evals, both satisfied by attestations X and Y" is the audit
    record that makes a gate worth having (§12 risk 7).
    """

    allowed: bool
    gate: PromotionGate
    checks: list[dict] = field(default_factory=list)

    @property
    def blocked(self) -> list[dict]:
        return [c for c in self.checks if not c["ok"]]

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "gate": self.gate.describe(), "checks": self.checks}


def _coerce(spec: Mapping[str, Any], environment: str, source: str) -> PromotionGate:
    unknown = [k for k in spec if k not in _GATE_FIELDS]
    if unknown:
        raise RyaError(
            "E_VALIDATION",
            f"Promotion gate for '{environment}' has unrecognised key(s): {', '.join(sorted(unknown))}.",
            hint="Valid keys: " + ", ".join(sorted(_GATE_FIELDS)) + ". A gate is refused rather "
            "than partially applied, because a mistyped requirement that silently did nothing "
            "would be worse than no gate at all.",
        )
    kwargs: dict[str, Any] = {}
    for wire, attr in _GATE_FIELDS.items():
        if wire not in spec:
            continue
        value = spec[wire]
        if attr == "require_provenance":
            if not isinstance(value, (list, tuple)):
                raise RyaError("E_VALIDATION",
                               f"`requireProvenance` for '{environment}' must be a list of "
                               f"metadata keys, got {type(value).__name__}.",
                               hint='e.g. ["gitSha", "ciRunUrl"] — the provenance slot filled by '
                               "`rya deploy --metadata gitSha=…`.")
            kwargs[attr] = tuple(str(v) for v in value)
        elif attr == "min_eval_score":
            try:
                kwargs[attr] = float(value)
            except (TypeError, ValueError):
                raise RyaError("E_VALIDATION",
                               f"`minEvalScore` for '{environment}' must be a number 0..1, "
                               f"got {value!r}.") from None
            if not 0.0 <= kwargs[attr] <= 1.0:
                raise RyaError("E_VALIDATION",
                               f"`minEvalScore` for '{environment}' must be between 0 and 1, "
                               f"got {kwargs[attr]}.")
        else:
            kwargs[attr] = bool(value)
    return PromotionGate(environment=environment, source=source, **kwargs)


def resolve_gate(store, environment: str, agent: Optional[str] = None) -> PromotionGate:
    """The gate for ``environment``. Never ``None`` — an unconfigured platform
    resolves to an unenforced gate.

    Policy shape::

        {"default":      {"requireReadiness": true},
         "environments": {"prod": {"requireEvals": true, "minEvalScore": 1.0}}}

    An environment entry is merged OVER ``default`` rather than replacing it, so
    "readiness everywhere, evals additionally on prod" is expressible without
    restating the common requirements per environment.
    """
    policy = gate_policy(store, agent)
    if not isinstance(policy, Mapping):
        # Fail closed: privileged state we cannot parse is not "no requirements".
        raise RyaError(
            "E_PROMOTION_BLOCKED",
            f"The promotion gate policy is malformed ({type(policy).__name__}, expected an object).",
            hint="Inspect it with `rya policy get promotion --json` and re-set it. Promotion is "
            "refused while the gate cannot be read, rather than proceeding ungated.",
        )
    default = policy.get("default") or {}
    per_env = (policy.get("environments") or {}).get(environment) or {}
    if not isinstance(default, Mapping) or not isinstance(per_env, Mapping):
        raise RyaError(
            "E_PROMOTION_BLOCKED",
            f"The promotion gate policy for '{environment}' is malformed.",
            hint="`default` and `environments.<name>` must both be objects of gate requirements.",
        )
    merged = {**default, **per_env}
    source = "policy" if merged else "default"
    return _coerce(merged, environment, source)


def set_gate(store, policy: Mapping[str, Any] | None, *, actor: Optional[str] = None,
             agent: Optional[str] = None) -> dict:
    """Write the gate policy, validating it first.

    Validation happens here rather than at read time so a bad gate is rejected by
    the operator who typed it, instead of surfacing later as a refused deploy
    nobody can explain. Passing ``None`` clears the policy.
    """
    if policy is None:
        return store.policy_set(policy_key(agent), None, actor=actor)
    if not isinstance(policy, Mapping):
        raise RyaError("E_VALIDATION", "The promotion policy must be an object.",
                       hint='Shape: {"default": {...}, "environments": {"prod": {...}}}')
    unknown = [k for k in policy if k not in ("default", "environments")]
    if unknown:
        raise RyaError("E_VALIDATION",
                       f"Unrecognised promotion policy key(s): {', '.join(sorted(unknown))}.",
                       hint='Only "default" and "environments" are valid at the top level.')
    # Coerce every entry so a malformed requirement is rejected on write.
    _coerce(policy.get("default") or {}, "default", "policy")
    for name, spec in (policy.get("environments") or {}).items():
        _coerce(spec or {}, str(name), "policy")
    return store.policy_set(policy_key(agent), dict(policy), actor=actor)


# --------------------------------------------------------------------------- #
# attestations — the evidence a check ran against this exact content
# --------------------------------------------------------------------------- #
def _latest(store, version_id: str, kind: str) -> Optional[dict]:
    """The most recent attestation of ``kind``.

    Latest-wins on the read path even though storage is append-only: re-running a
    failed check after a fix must be able to unblock a promotion. The superseded
    records stay on disk, which is what keeps "this passed only on the third
    attempt" auditable.
    """
    records = store.version_attestations(version_id, kind=kind)
    return records[-1] if records else None


def attest_readiness(store, version: Mapping[str, Any], report: Mapping[str, Any], *,
                     actor: Optional[str] = None) -> dict:
    """File a readiness report against a version.

    Only the summary is stored, not the whole report: the blocks and warnings are
    reproducible from the bundle, and an attestation is read on every promotion.
    """
    summary = report.get("summary") or {}
    return store.version_attest(version["id"], {
        "kind": ATTEST_READINESS,
        "ok": bool(report.get("ready")),
        "actor": actor,
        "bundleHash": version.get("bundleHash"),
        "blocks": int(summary.get("blocks") or 0),
        "warnings": int(summary.get("warnings") or 0),
        "blockCodes": [b.get("code") for b in (report.get("blocks") or [])],
        "warningCodes": [w.get("code") for w in (report.get("warnings") or [])],
    })


def attest_evals(store, version: Mapping[str, Any], result: Mapping[str, Any], *,
                 actor: Optional[str] = None) -> dict:
    """File an eval run against a version."""
    return store.version_attest(version["id"], {
        "kind": ATTEST_EVALS,
        "ok": bool(result.get("ok")),
        "actor": actor,
        "bundleHash": version.get("bundleHash"),
        "total": int(result.get("total") or 0),
        "passed": int(result.get("passed") or 0),
        "failed": int(result.get("failed") or 0),
        "score": result.get("score"),
        "hasEvals": bool(result.get("hasEvals")),
        "failedCases": [r.get("id") for r in (result.get("results") or []) if not r.get("pass")],
    })


def attestations(store, version_id: str) -> list[dict]:
    """Every attestation for a version, oldest first — the audit view."""
    return store.version_attestations(version_id)


# --------------------------------------------------------------------------- #
# the check
# --------------------------------------------------------------------------- #
def _check(name: str, ok: bool, detail: str, fix: str) -> dict:
    return {"check": name, "ok": ok, "detail": detail, "fix": fix}


def check_promotion(store, *, version: Mapping[str, Any], environment: str,
                    actor: Optional[str] = None) -> GateResult:
    """Evaluate the gate for promoting ``version`` into ``environment``.

    Returns a result rather than raising, so a console or a ``--dry-run`` can
    show an operator what is missing before they try. :func:`require_promotion`
    is the enforcing wrapper.
    """
    # D28 Rule 1: the agent is DERIVED from the version row rather than passed.
    # A caller that could name a different one would be able to evaluate the
    # wrong agent's gate against this version.
    gate = resolve_gate(store, environment, agent=version.get("agent"))
    version_id = version.get("id") or "?"
    checks: list[dict] = []

    if gate.require_readiness:
        att = _latest(store, version_id, ATTEST_READINESS)
        if att is None:
            checks.append(_check(
                "readiness", False,
                f"No readiness attestation for version {version_id}.",
                # Deliberately names only what exists. `rya publish` reaches this
                # check often — the control plane cannot import a bundle, so it
                # cannot evaluate readiness and files nothing — and there is no
                # out-of-band `rya attest` command to point at.
                "Run `rya deploy --env <name> --actor you` from a machine with store "
                "access; it evaluates readiness and files the attestation. A version "
                "published over HTTP carries no readiness evidence and cannot be "
                "promoted into a readiness-gated environment.",
            ))
        elif att.get("bundleHash") and att.get("bundleHash") != version.get("bundleHash"):
            # Should be unreachable — attestations are keyed by version id and a
            # version id is 1:1 with a bundle hash — so reaching it means the
            # records were edited out of band. Refuse rather than trust.
            checks.append(_check(
                "readiness", False,
                f"The readiness attestation is for bundle {str(att.get('bundleHash'))[:12]}, but "
                f"version {version_id} is {str(version.get('bundleHash'))[:12]}.",
                "The attestation records do not match the version ledger. Re-attest and "
                "investigate how they diverged.",
            ))
        elif not att.get("ok"):
            checks.append(_check(
                "readiness", False,
                f"Readiness failed with {att.get('blocks')} blocker(s): "
                f"{', '.join(c for c in (att.get('blockCodes') or []) if c) or 'unknown'}.",
                "Fix the blockers (`rya deploy --check --json` lists each with its fix), then "
                "re-deploy so a passing readiness attestation is recorded.",
            ))
        elif not gate.allow_warnings and int(att.get("warnings") or 0) > 0:
            checks.append(_check(
                "readiness", False,
                f"Readiness passed but with {att.get('warnings')} warning(s): "
                f"{', '.join(c for c in (att.get('warningCodes') or []) if c)}.",
                f"'{environment}' is configured with allowWarnings=false. Resolve the warnings, "
                "or relax the gate.",
            ))
        else:
            checks.append(_check("readiness", True,
                                 f"Readiness passed ({att.get('warnings')} warning(s)).", ""))

    if gate.require_evals:
        att = _latest(store, version_id, ATTEST_EVALS)
        if att is None:
            checks.append(_check(
                "evals", False,
                f"No eval attestation for version {version_id}.",
                f"Run `rya eval --attest --version {version_id}` against this version, then "
                "promote.",
            ))
        elif not att.get("hasEvals"):
            # The vacuous pass. An eval suite with no cases is not a green suite.
            checks.append(_check(
                "evals", False,
                "The eval run found no cases, so it proves nothing.",
                "Add cases to rya.evals.yaml (`rya eval` runs them) — a gate satisfied by an "
                "empty suite is a checkbox, not a control.",
            ))
        elif not att.get("ok"):
            failed = ", ".join(c for c in (att.get("failedCases") or []) if c)
            checks.append(_check(
                "evals", False,
                f"{att.get('failed')}/{att.get('total')} eval case(s) failed"
                + (f": {failed}" if failed else "") + ".",
                "Fix the agent or the expectations, re-run `rya eval --attest`, then promote.",
            ))
        elif att.get("score") is not None and float(att["score"]) < gate.min_eval_score:
            checks.append(_check(
                "evals", False,
                f"Eval score {att['score']} is below the {gate.min_eval_score} required by "
                f"'{environment}'.",
                "Raise the passing rate, or lower minEvalScore for this environment.",
            ))
        else:
            checks.append(_check("evals", True,
                                 f"{att.get('passed')}/{att.get('total')} eval case(s) passed "
                                 f"(score {att.get('score')}).", ""))

    if gate.require_actor:
        ok = bool((actor or "").strip())
        checks.append(_check(
            "actor", ok,
            f"Promotion attributed to '{actor}'." if ok else "No actor was supplied.",
            "" if ok else "Pass --actor <who> (§12 risk 7: for a governance product, "
            "'who promoted this' is a feature).",
        ))

    if gate.require_provenance:
        metadata = version.get("metadata") or {}
        missing = [k for k in gate.require_provenance if not (metadata or {}).get(k)]
        checks.append(_check(
            "provenance", not missing,
            f"Missing provenance: {', '.join(missing)}." if missing
            else f"Provenance present: {', '.join(gate.require_provenance)}.",
            "" if not missing else "Record it at deploy time: `rya deploy --metadata "
            + ",".join(f"{k}=…" for k in missing) + "`.",
        ))

    return GateResult(allowed=all(c["ok"] for c in checks), gate=gate, checks=checks)


def require_promotion(store, *, version: Mapping[str, Any], environment: str,
                      actor: Optional[str] = None, force: bool = False) -> GateResult:
    """Enforce the gate. Raises ``E_PROMOTION_BLOCKED`` listing every failure.

    ``force=True`` is the operator override and it is *recorded* rather than
    silent: an override that leaves no trace turns the gate into decoration. The
    override attestation names the actor and every requirement that was bypassed.
    """
    result = check_promotion(store, version=version, environment=environment, actor=actor)
    if result.allowed or not result.gate.enforced:
        return result

    if force:
        store.version_attest(version.get("id") or "?", {
            "kind": ATTEST_OVERRIDE,
            "ok": True,
            "actor": actor,
            "environment": environment,
            "bundleHash": version.get("bundleHash"),
            "bypassed": [c["check"] for c in result.blocked],
            "detail": [c["detail"] for c in result.blocked],
        })
        return result

    failures = result.blocked
    lines = "; ".join(f"{c['check']}: {c['detail']}" for c in failures)
    fixes = " ".join(c["fix"] for c in failures if c["fix"])
    raise RyaError(
        "E_PROMOTION_BLOCKED",
        f"Promotion into '{environment}' blocked by {len(failures)} unmet requirement(s). {lines}",
        hint=((fixes + " ") if fixes else "")
        + f"Inspect the gate with `rya gate show --env {environment} --json`, or override with "
        "--force (which is recorded against the version).",
    )
