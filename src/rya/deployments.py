"""Versions, environments, promote and rollback (PLATFORM_DESIGN D11, D12, §9).

§9's pipeline in one module::

    rya deploy
      ├─ validate manifest + readiness gate locally     (manifest/, readiness.py)
      ├─ bundle: source + lockfile + manifest + SDK     (bundles.py)
      ├─ upload; platform records an immutable version  (create_version)
      ├─ promote: set the environment's current version (promote)
      └─ roll out ... keep versions alive while runs are pinned to them
                                                        (retire / pinned_runs)

This is pure orchestration over the store's ``version_*`` / ``env_*`` API. It
holds no state and opens no files, which is deliberate: the api process, the CLI
and a future console all need the same decisions, and the decisions are the part
that must not fork.

Three invariants carry the design:

1. **A version is content, not a name.** ``version_create`` is idempotent on
   ``(agent, bundleHash)``, so deploying identical content twice is one version.
   That is what ``agentVersion`` — the author-typed ``manifest.version`` string at
   ``runtime/engine.py:146`` — could never give us (D12).
2. **An environment is a pointer.** Promote flips it, rollback flips it back
   (§9: "Rollback is a pointer flip"), and the prior pointer lands in
   ``history`` so the flip is reversible and auditable. A version is never "a
   prod version": D11 deletes ``environment:`` from the manifest precisely so one
   artifact can move *between* environments unchanged.
3. **Retirement fails closed.** §6: "the version is retained while any run is
   pinned to it". Replay reads the code that wrote the journal, so retiring a
   version out from under a live run destroys its ability to resume — hence
   ``E_VERSION_IN_USE`` and ``pinned_runs`` to explain the refusal.

**Run pinning contract.** ``resolve_for_run`` returns the version a run must
stamp on itself. The field names this module queries are ``run["versionId"]`` and
``run["bundleHash"]``; a run without ``versionId`` is unpinned by definition
(pre-D12) and is never counted as holding a version.
"""

from __future__ import annotations

from typing import Any

from .errors import RyaError
from .turns import TERMINAL_RUN_STATUSES

# States a version record can be in. "retired" is not "deleted": the record and
# its artifact are retained so an already-pinned run can still replay, but a
# retired version accepts no NEW runs and cannot be promoted.
VERSION_ACTIVE = "active"
VERSION_RETIRED = "retired"


def _require_environment_name(environment: str) -> str:
    name = (environment or "").strip()
    if not name:
        raise RyaError(
            "E_VALIDATION",
            "An environment name is required.",
            hint="Pass --env dev|staging|prod (§2: an environment holds one current version).",
        )
    if "/" in name:
        raise RyaError(
            "E_VALIDATION",
            f"Environment name '{name}' may not contain '/'.",
            hint="Use a flat name such as dev, staging or prod.",
        )
    return name


def _require_version(store, version_id: str) -> dict:
    version = store.version_get(version_id)
    if version is None:
        # D12: a run whose version was deleted fails closed with a stable code —
        # the same code an operator sees when they typo a version id.
        raise RyaError(
            "E_VERSION_NOT_FOUND",
            f"No deployment version '{version_id}'.",
            hint="List what exists with `rya versions --agent <agent> --json`, then retry.",
        )
    return version


# --------------------------------------------------------------------------- #
# versions
# --------------------------------------------------------------------------- #
def create_version(
    store,
    *,
    agent: str,
    bundle,
    environment: str | None = None,
    actor: str | None = None,
    metadata: dict | None = None,
    force: bool = False,
) -> dict:
    """Record ``bundle`` as an immutable version of ``agent``.

    Idempotent by content: a second call with the same bundle returns the same
    version id untouched (``version_create``'s contract), so ``rya deploy`` is
    safe to retry after a network failure and CI re-running on an unchanged tree
    does not litter the version list.

    ``metadata`` is the provenance slot (§8 "signed bundles, pinned lockfiles,
    provenance on the version") — git sha, CI run url, who built it.

    The bundle's ``files`` map is deliberately NOT persisted here: it is carried
    inside the archive's ``.rya-bundle.json`` instead. A version record is read
    on every run resolution, and a per-run read should not drag a few thousand
    path/digest pairs with it.

    If ``environment`` is given the new version is promoted into it as well — the
    one-shot convenience path. Note that a caller which needs to satisfy a
    promotion gate (§9) must NOT use it: evidence is attested against a version
    id, so the version has to exist before it can be attested, and the attestation
    has to exist before the promotion is checked. ``rya deploy`` therefore does the
    three steps explicitly — record, attest, promote — and leaves this argument
    for ungated environments.
    """
    if not agent:
        raise RyaError(
            "E_VALIDATION",
            "An agent name is required to record a version.",
            hint="It comes from `name:` in rya.agent.yaml; check the manifest in the bundle.",
        )
    if bundle.agent and bundle.agent != agent:
        # The manifest names the agent (D11: one manifest per agent), so a
        # mismatch means the caller is about to file this content in the wrong
        # namespace, where it would later be promotable into the wrong pointer.
        raise RyaError(
            "E_BUNDLE_MISMATCH",
            f"Bundle declares agent '{bundle.agent}' but the deploy targets '{agent}'.",
            hint="Deploy from the project whose rya.agent.yaml has `name: " + agent + "`.",
        )

    record: dict[str, Any] = {
        "agent": agent,
        "bundleHash": bundle.hash,
        "sdkVersion": bundle.sdkVersion,
        "entrypoint": bundle.entrypoint,
        "lockfile": bundle.lockfile,
        "sizeBytes": bundle.sizeBytes,
        "fileCount": bundle.fileCount,
        # The author-typed manifest version survives as a LABEL only. It is not
        # the identity (that is bundleHash) and nothing branches on it.
        "manifestVersion": bundle.manifest.get("version"),
        # The manifest ITSELF, unlike `files` above, IS persisted (D21).
        #
        # The size instinct that keeps `files` out applies here too and is
        # overruled: a manifest is a few KB against `files`' few thousand
        # path/digest pairs, and D21 makes it load-bearing rather than
        # informational. A manifest-free `api` learns what agents exist, what
        # tools they declare and what channels they expose from *this* field —
        # without it there is nothing to serve, and the api is back to reading a
        # local `rya.agent.yaml`, which is the one-agent limit.
        #
        # Stored unvalidated, exactly as `Bundle.manifest` holds it, so the
        # record reflects what the author shipped rather than what a later
        # schema version would make of it.
        "manifest": bundle.manifest,
        "createdBy": actor,
        "metadata": metadata or {},
    }
    version = store.version_create(record)
    if environment is not None:
        promote(store, environment=environment, agent=agent, version_id=version["id"],
                actor=actor, force=force)
        version = store.version_get(version["id"]) or version
    return version


def manifest_of(version: dict) -> dict | None:
    """The manifest a version shipped with, or ``None`` for records written
    before D21 persisted it.

    One accessor rather than ``version.get("manifest")`` at each call site,
    because the ``None`` is a real state with a real cause and it will outlive
    this release: ``version_create`` dedupes on ``(agent, bundleHash)`` and
    returns the existing row untouched, so re-publishing identical content does
    **not** backfill the manifest onto an old record. Only new content gets one.

    Callers that need a manifest for an old version must fall back to reading it
    out of the bundle archive (``.rya-bundle.json``), which is authoritative but
    costs an object fetch — the reason it is not the primary path.
    """
    manifest = version.get("manifest")
    return manifest if isinstance(manifest, dict) and manifest else None


def list_versions(store, agent: str | None = None, state: str | None = None) -> list[dict]:
    """Versions newest-first. A passthrough so callers never touch the store's
    shape directly — the console, the CLI and the api all go through here."""
    return store.version_list(agent=agent, state=state)


def pinned_runs(store, version_id: str) -> list[dict]:
    """Non-terminal runs still pinned to ``version_id``.

    Exposed rather than inlined into :func:`retire` because a refusal has to be
    explainable: "you cannot retire this" is a dead end, "these three runs are
    still on it" is an action. Terminal statuses come from
    ``turns.TERMINAL_RUN_STATUSES`` so there is exactly one definition of "done".
    """
    out = []
    for run in store.list_runs():
        if run.get("versionId") != version_id:
            continue
        if run.get("status") in TERMINAL_RUN_STATUSES:
            continue
        out.append(run)
    return out


def retire(store, version_id: str, *, force: bool = False) -> dict:
    """Mark a version retired: no new runs, no promotion, artifact still retained.

    Fails closed with ``E_VERSION_IN_USE`` when the version is an environment's
    current pointer, or when any non-terminal run is pinned to it (§6 "Version
    retirement", D12). Idempotent on an already-retired version.

    ``force=True`` is the operator override and it is not free: a pinned run that
    later resumes against a retired version has no guarantee its artifact is
    still there, so replay may fail closed. It exists because a leaked run that
    will never terminate must not pin a version forever.
    """
    version = _require_version(store, version_id)
    if version.get("state") == VERSION_RETIRED:
        return version

    if not force:
        for env in store.env_list(agent=version.get("agent")):
            if env.get("currentVersionId") == version_id:
                raise RyaError(
                    "E_VERSION_IN_USE",
                    f"Version {version_id} is the current version of environment "
                    f"'{env.get('name')}'.",
                    hint=f"Promote or roll back '{env.get('name')}' to another version first "
                    f"(`rya promote --env {env.get('name')} --version <id>`), then retire this one.",
                )
        live = pinned_runs(store, version_id)
        if live:
            ids = ", ".join(r["id"] for r in live[:5])
            more = f" (+{len(live) - 5} more)" if len(live) > 5 else ""
            raise RyaError(
                "E_VERSION_IN_USE",
                f"{len(live)} run(s) are still pinned to version {version_id}: {ids}{more}.",
                hint="Replay needs the code that wrote the journal (D12). Wait for those runs to "
                "reach a terminal status, cancel them, or override with --force.",
            )

    return store.version_set_state(version_id, VERSION_RETIRED) or version


# --------------------------------------------------------------------------- #
# environments — the current-version pointer (D11, §9)
# --------------------------------------------------------------------------- #
def promote(store, *, environment: str, agent: str, version_id: str, actor: str | None = None,
            gate: bool = True, force: bool = False) -> dict:
    """Point ``environment`` at ``version_id``. Returns the environment record.

    §9: deploys are atomic per environment — the pointer flips once, new runs go
    to the new version, and in-flight runs finish on theirs because they pinned a
    version id rather than "whatever is current".

    Refuses four ways, all fail-closed:

    * unknown version → ``E_VERSION_NOT_FOUND``
    * retired version → ``E_VERSION_RETIRED`` (retirement is one-way; re-deploy)
    * version belonging to another agent → ``E_BUNDLE_MISMATCH``. Not
      ``E_VERSION_NOT_FOUND``: the id *does* exist, and reporting it as missing
      would send a coding agent hunting for a typo instead of showing it that it
      crossed an agent boundary.
    * an unmet promotion gate → ``E_PROMOTION_BLOCKED`` (§9's server-side
      admission check; see ``gates.py``). Unconfigured environments resolve to an
      unenforced gate, so this refusal only appears once an operator asks for it.

    ``gate=False`` is for callers that are structurally not promotions —
    :func:`rollback` is the one that matters, and its docstring says why.
    """
    environment = _require_environment_name(environment)
    version = _require_version(store, version_id)

    if version.get("agent") != agent:
        raise RyaError(
            "E_BUNDLE_MISMATCH",
            f"Version {version_id} belongs to agent '{version.get('agent')}', "
            f"not '{agent}'.",
            hint=f"Promote a version of '{agent}' (`rya versions --agent {agent}`), or target the "
            f"environment of agent '{version.get('agent')}'.",
        )
    if version.get("state") == VERSION_RETIRED:
        raise RyaError(
            "E_VERSION_RETIRED",
            f"Version {version_id} is retired (at {version.get('retiredAt')}) and cannot be promoted.",
            hint="Retirement is deliberate and one-way. Re-run `rya deploy` from that source tree "
            "to record a fresh version, or promote an active one.",
        )

    if gate:
        # Imported here rather than at module scope so this module keeps its "pure
        # orchestration, no state" property from the docstring: gates.py reads
        # privileged policy, and only the promote path needs it.
        from .gates import require_promotion
        require_promotion(store, version=version, environment=environment,
                          actor=actor, force=force)

    # `env_set_current` pushes the PRIOR pointer onto history — that push is the
    # whole rollback mechanism, so promote must never bypass it.
    return store.env_set_current(environment, agent, version_id, actor=actor)


def current_version(store, environment: str, agent: str) -> dict | None:
    """The version an environment currently points at, or ``None`` if nothing has
    been promoted yet.

    A pointer that resolves to no record is *not* ``None`` — it is corruption, and
    D12 says fail closed rather than silently behave like an empty environment.
    """
    env = store.env_get(environment, agent)
    if env is None:
        return None
    version_id = env.get("currentVersionId")
    if not version_id:
        return None
    return _require_version(store, version_id)


def resolve_for_run(store, environment: str, agent: str) -> dict:
    """The version a new run must pin itself to. Raises rather than returning None.

    Callers stamp ``versionId`` / ``bundleHash`` / ``environment`` from the
    returned record onto the run, which is what lets an in-flight run keep
    executing on its own version across a deploy (§9) and what makes
    :func:`pinned_runs` answerable.
    """
    environment = _require_environment_name(environment)
    env = store.env_get(environment, agent)
    if env is None or not env.get("currentVersionId"):
        raise RyaError(
            "E_ENVIRONMENT_NOT_FOUND",
            f"No version is promoted to environment '{environment}' for agent '{agent}'.",
            hint=f"Run `rya deploy --env {environment}` to bundle the project and promote it, "
            f"or `rya promote --env {environment} --version <id>` to point at an existing version.",
        )
    version = _require_version(store, env["currentVersionId"])
    if version.get("state") == VERSION_RETIRED:
        # Defence in depth: `retire` refuses an environment's current version, so
        # reaching this means the pointer was moved to a retired version by some
        # other path. Starting a run on it would create a run pinned to code we
        # have already promised to stop retaining.
        raise RyaError(
            "E_VERSION_RETIRED",
            f"Environment '{environment}' points at retired version {version['id']}.",
            hint=f"Promote an active version into '{environment}' before starting new runs "
            f"(`rya versions --agent {agent} --state active`).",
        )
    return version


def rollback(
    store,
    *,
    environment: str,
    agent: str,
    actor: str | None = None,
    to_version_id: str | None = None,
) -> dict:
    """Flip the pointer back. §9: "Rollback is a pointer flip."

    Defaults to the most recent entry in the environment's ``history`` — the
    version that was current immediately before the last promote. Rollback is
    itself recorded as a promote, so it is reversible in turn (which does mean a
    bare ``rollback`` twice in a row ping-pongs between two versions; pass
    ``to_version_id`` to land somewhere specific).

    Validation is delegated to :func:`promote`, so rolling back to a retired or
    foreign version fails with exactly the same codes as promoting to one — with
    one deliberate exception: **rollback is never gated.** A promotion gate (§9,
    ``gates.py``) exists to stop unvetted code going forward; applying it to a
    rollback would let a missing eval attestation hold an outage open, which is a
    strictly worse failure than the one the gate prevents. The target version was
    current at some point, so it already passed whatever gate existed then. This
    is enforced by construction here rather than by an operator remembering a
    flag under pressure.
    """
    environment = _require_environment_name(environment)
    env = store.env_get(environment, agent)
    if env is None or not env.get("currentVersionId"):
        raise RyaError(
            "E_ENVIRONMENT_NOT_FOUND",
            f"Environment '{environment}' for agent '{agent}' has no current version to roll back from.",
            hint=f"Run `rya deploy --env {environment}` first.",
        )

    target = to_version_id
    if target is None:
        prior = env.get("history") or []
        if not prior:
            raise RyaError(
                "E_VERSION_NOT_FOUND",
                f"Environment '{environment}' has no previous version to roll back to "
                f"(it has only ever pointed at {env['currentVersionId']}).",
                hint=f"Pick an explicit target: `rya versions --agent {agent} --json`, then "
                f"`rya rollback --env {environment} --version <id>`.",
            )
        target = prior[-1]["versionId"]

    if target == env.get("currentVersionId"):
        raise RyaError(
            "E_VALIDATION",
            f"Version {target} is already current for environment '{environment}'.",
            hint="Nothing to roll back. Check `rya history --env " + environment + "`.",
        )
    return promote(store, environment=environment, agent=agent, version_id=target, actor=actor,
                   gate=False)


def list_environments(store, agent: str | None = None) -> list[dict]:
    """All environment pointers, for the console's workspace → project →
    environment → version tree (§11 item 12)."""
    return store.env_list(agent=agent)


def history(store, environment: str, agent: str) -> list[dict]:
    """The promote/rollback audit view for an environment, newest first.

    Entry shape::

        {"versionId", "bundleHash", "manifestVersion", "state", "current",
         "at", "actor", "version": <full record or None>}

    ``version`` is resolved rather than left as an id because the reader — a
    console page or a coding agent asking "what is on prod and where did it come
    from" — always needs the hash next to the pointer. §12 risk 7: for a
    governance product, "who changed this" is a feature.
    """
    environment = _require_environment_name(environment)
    env = store.env_get(environment, agent)
    if env is None:
        raise RyaError(
            "E_ENVIRONMENT_NOT_FOUND",
            f"No environment '{environment}' for agent '{agent}'.",
            hint=f"Environments are created by promotion: `rya deploy --env {environment}`.",
        )

    def entry(version_id: str, at: str | None, actor: str | None, current: bool) -> dict:
        record = store.version_get(version_id)
        return {
            "versionId": version_id,
            "bundleHash": (record or {}).get("bundleHash"),
            "manifestVersion": (record or {}).get("manifestVersion"),
            "state": (record or {}).get("state"),
            "current": current,
            "at": at,
            "actor": actor,
            "version": record,
        }

    out = []
    if env.get("currentVersionId"):
        out.append(entry(env["currentVersionId"], env.get("updatedAt"), env.get("actor"), True))
    for item in reversed(env.get("history") or []):
        out.append(entry(item["versionId"], item.get("replacedAt"), item.get("actor"), False))
    return out


def describe_environment(store, environment: str, agent: str) -> dict:
    """One-shot status for `rya status` / the console: pointer, version, drift.

    ``pinnedRuns`` counts runs still on OLDER versions, which is the number that
    answers "can I retire anything yet?" during a rollout (§9's drain step).
    """
    environment = _require_environment_name(environment)
    env = store.env_get(environment, agent)
    if env is None:
        raise RyaError(
            "E_ENVIRONMENT_NOT_FOUND",
            f"No environment '{environment}' for agent '{agent}'.",
            hint=f"Run `rya deploy --env {environment}` to create it.",
        )
    version = current_version(store, environment, agent)
    stale = {}
    for other in store.version_list(agent=agent):
        if version is not None and other["id"] == version["id"]:
            continue
        count = len(pinned_runs(store, other["id"]))
        if count:
            stale[other["id"]] = count
    return {
        "name": env.get("name"),
        "agent": agent,
        "currentVersionId": env.get("currentVersionId"),
        "currentVersion": version,
        "updatedAt": env.get("updatedAt"),
        "actor": env.get("actor"),
        "historyDepth": len(env.get("history") or []),
        "pinnedRuns": stale,
    }
