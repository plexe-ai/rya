"""Which agents this deployment serves, and what each one declares (D21).

`build_app` used to answer both questions by reading one `rya.agent.yaml` off
disk at boot. That single `load_manifest` + `load_agent` pair *was* the one-agent
limit: every route resolved `manifest.name` regardless of the `{agent_id}` in its
path, so a second agent had nowhere to be. It also meant the control plane
imported tenant code just to learn a tool list, which D13→D17 forbids.

This module replaces both reads with a lookup against the store. An agent exists
because a **version** of it was published (`rya_versions`) or an **environment**
points at one (`rya_environments`); what it declares comes from the manifest
`create_version` persists on the version record (#8). No file, no import.

A mounted `rya.agent.yaml` still counts, in both modes, and is the **operator's**
agent rather than any tenant's. That is not a cross-tenant read: it is the
deployed-agent model the multi-tenant posture already ships — `docker compose up`
mounts one project and every workspace uses it (`architecture.md`, "one deployed
agent serves many isolated tenants"). Excluding it would empty the agent list for
every tenant that has not published anything of its own, which is all of them on
day one.

**A published version always beats the mounted tree**, per workspace and through
that workspace's own RLS-scoped store. So a tenant that publishes its own
`support` gets its own; a tenant that has not gets the operator's. The tree is
the fallback, never the override — see `resolve` for why that direction is
load-bearing rather than a preference.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from .errors import RyaError

if TYPE_CHECKING:  # pragma: no cover
    from .manifest.schema import Manifest

# Where an AgentRef's manifest came from. Surfaced on `/agents` so an operator can
# see why the tool list they are looking at differs from the one they published.
SOURCE_PROJECT = "project"    # the mounted rya.agent.yaml — the operator's agent
SOURCE_VERSION = "version"    # the manifest persisted on a version record
SOURCE_UNKNOWN = "unknown"    # the agent exists but no manifest is retrievable


@dataclass(frozen=True)
class AgentRef:
    """One agent this deployment serves.

    `version` is **only** what the deployment's environment points at, and it is
    the thing a queued run gets PINNED to. `declared_by` is where `manifest` came
    from, which may be a newer, unpromoted version.

    Keeping those two apart is load-bearing. Collapsing them — pinning to
    "whatever version is newest when nothing is promoted" — routes a run to a
    version no environment points at, so `queue.claim`'s version filter sends it
    to a worker that does not exist and the turn sits pending forever. An
    unpromoted deployment must enqueue UNPINNED, exactly as it did before Phase 2;
    what it must not do is invent a pin.
    """

    name: str
    manifest: "Manifest"
    source: str
    version: Optional[dict] = None
    environment: Optional[str] = None
    declared_by: Optional[dict] = None

    @property
    def version_id(self) -> Optional[str]:
        return (self.version or {}).get("id")

    @property
    def bundle_hash(self) -> Optional[str]:
        return (self.version or {}).get("bundleHash")

    def describe(self) -> dict:
        return {"name": self.name, "source": self.source,
                "versionId": self.version_id, "bundleHash": self.bundle_hash,
                "environment": self.environment,
                "declaredBy": (self.declared_by or {}).get("id"),
                "manifestAvailable": self.source != SOURCE_UNKNOWN}


def _placeholder_manifest(name: str) -> "Manifest":
    """A manifest for an agent whose real one is not retrievable.

    Two causes, both real and both outliving this release. A version published
    before #8 has no `manifest` field at all, and `version_create` dedupes on
    `(agent, bundleHash)` — so re-publishing identical bytes returns the old row
    untouched and never backfills it (`deployments.manifest_of`). And an
    environment can point at a version whose record has since been removed.

    Refusing to list such an agent would make it invisible to the console and
    un-rollback-able, which is worse than serving an empty tool list: promotion
    and rollback operate on version ids and do not need a manifest at all. So the
    agent stays addressable and `manifestAvailable` says the declaration is
    missing.
    """
    from .manifest.schema import Manifest

    return Manifest(name=name)


def _parse(raw: dict, *, name: str, version_id: Optional[str]) -> "Manifest":
    """Validate a stored manifest, naming the version when it will not parse.

    `create_version` stores the manifest exactly as the author shipped it, on
    purpose — the record reflects what was published rather than what a later
    schema makes of it. The cost lands here: a manifest written by a newer (or
    broken) SDK reaches validation for the first time on read. Say which version
    is unreadable, because the operator's fix is to re-publish or roll back that
    one, and neither is discoverable from a bare pydantic error.
    """
    from pydantic import ValidationError

    from .manifest.schema import Manifest

    try:
        return Manifest.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", []))
        raise RyaError(
            "E_AGENT_MANIFEST_INVALID",
            f"Version {version_id or '?'} of '{name}' has an unreadable manifest "
            f"at '{loc}': {first.get('msg')}",
            hint="Re-publish that agent, or roll the environment back to a version "
                 "whose manifest this build understands.",
        ) from None


def _project_manifest(root: Optional[Path]) -> Optional["Manifest"]:
    """The mounted `rya.agent.yaml`, or None if there is not one.

    A missing manifest is the normal state for a multi-agent deployment, not an
    error — which is the whole point of D21. A malformed one is still an error,
    but not one this call raises: `build_app` validates it at boot so a typo in
    the operator's own file surfaces at startup rather than on a request.
    """
    if root is None:
        return None
    from .manifest.loader import MANIFEST_NAME, load_manifest

    path = Path(root) / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        return load_manifest(path)
    except RyaError:
        return None


def names(store, root: Optional[Path] = None) -> List[str]:
    """Every agent this deployment serves, sorted.

    Uses the store's `agent_list()` — a `SELECT DISTINCT agent` over the two
    tables — rather than listing versions and reducing, because this runs on
    every unprefixed Rule-6 request (D28) and a full version scan per request is
    not a per-request cost worth paying.
    """
    found = set(store.agent_list())
    project = _project_manifest(root)
    if project is not None:
        found.add(project.name)
    return sorted(found)


def sole_agent(store, root: Optional[Path] = None) -> Optional[str]:
    """The one agent this deployment serves, or None if it serves zero or many.

    The whole basis of D28's Rule 6 fallback: an unprefixed agent-scoped route is
    only unambiguous while there is exactly one candidate.
    """
    found = names(store, root)
    return found[0] if len(found) == 1 else None


def _promoted(store, name: str, environment: Optional[str]):
    """`(pinned, environment, declaring)`.

    `pinned` is what the deployment's environment points at and nothing else —
    see `AgentRef`. `declaring` is the record to read the manifest from, which
    falls back to the newest active version so a published-but-unpromoted agent
    still reports its own tool list rather than an empty placeholder. Reporting
    declarations from an unpromoted version is a display choice; pinning a run to
    one is a routing bug.
    """
    from . import deployments

    pinned = deployments.current_version(store, environment, name) if environment else None
    if pinned is not None:
        return pinned, environment, pinned
    active = deployments.list_versions(store, agent=name, state=deployments.VERSION_ACTIVE)
    return None, None, (active[0] if active else None)


def resolve(store, name: str, root: Optional[Path] = None,
            environment: Optional[str] = None) -> AgentRef:
    """The agent named `name`, or `E_AGENT_NOT_FOUND`.

    **A published version always wins over the mounted tree.** The tempting rule
    is the other way round — the tree is what a single-tenant inline worker would
    execute, so reporting it reports what runs — but `AgentRef.version` is what a
    queued run gets PINNED to, and a project has no version. Preferring the tree
    would silently un-pin every run on a deployment that has both, which is the
    one thing D12 exists to prevent. So the tree is the fallback for an agent with
    nothing published (`rya dev`, a fresh project), not the default.

    `environment` is the deployment's own (`config.current_environment`), not a
    caller-supplied one: which version is live is a property of the deployment,
    and letting a request choose would make `/tools` answer for a version this
    deployment does not run.
    """
    from . import deployments

    if not name:
        raise RyaError("E_VALIDATION", "An agent name is required.",
                       hint="Address the agent explicitly: /agents/<agent_id>/…")

    project = _project_manifest(root)
    if project is not None and project.name != name:
        project = None  # the mounted tree is one agent's, not the deployment's

    pinned, env_name, declaring = _promoted(store, name, environment)
    common = {"name": name, "version": pinned, "environment": env_name,
              "declared_by": declaring}

    if declaring is not None:
        raw = deployments.manifest_of(declaring)
        if raw is not None:
            return AgentRef(manifest=_parse(raw, name=name, version_id=declaring.get("id")),
                            source=SOURCE_VERSION, **common)
        # Pre-#8 record: no persisted manifest. Where the mounted tree IS this
        # agent it is a better answer than an empty placeholder — same content,
        # locally readable — and the pin still comes from the version.
        if project is not None:
            return AgentRef(manifest=project, source=SOURCE_PROJECT, **common)
        return AgentRef(manifest=_placeholder_manifest(name),
                        source=SOURCE_UNKNOWN, **common)

    if project is not None:
        return AgentRef(manifest=project, source=SOURCE_PROJECT, **common)

    # An environment pointer with no surviving version record still means the
    # agent exists — it is exactly the state a rollback has to be able to
    # address — so check for one before refusing.
    if pinned is None and not store.env_list(agent=name):
        raise RyaError(
            "E_AGENT_NOT_FOUND",
            f"No agent named '{name}' is served here.",
            hint="Publish one with `rya deploy`, or GET /agents to see what is served.",
        )
    return AgentRef(manifest=_placeholder_manifest(name), source=SOURCE_UNKNOWN, **common)


def list_refs(store, root: Optional[Path] = None,
              environment: Optional[str] = None) -> List[AgentRef]:
    """Every agent, resolved. For `GET /agents` and the console's selector."""
    out = []
    for name in names(store, root):
        try:
            out.append(resolve(store, name, root, environment))
        except RyaError:
            # One agent with an unreadable manifest must not make the whole list
            # un-renderable — that would take the console down for every other
            # agent in the workspace.
            out.append(AgentRef(name=name, manifest=_placeholder_manifest(name),
                                source=SOURCE_UNKNOWN))
    return out


def project_root() -> Optional[Path]:
    """The mounted project, if the operator declared one. `RYA_PROJECT` in compose."""
    raw = os.environ.get("RYA_PROJECT")
    return Path(raw) if raw else None
