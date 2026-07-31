"""Content-hashed agent bundles — the deployment artifact (PLATFORM_DESIGN D12).

A *bundle* is what `rya deploy` uploads and what a worker loads: source +
``rya.agent.yaml`` + lockfile + the SDK version, per §2's first noun. Today the
only thing resembling a version is ``agentVersion`` (``runtime/engine.py:146``),
which is the author-typed ``manifest.version`` string — no hash, no immutability,
no uniqueness. Two different trees can claim ``0.1.0``; the same tree can claim
two things. D12 replaces that with a content hash, because **replay is only sound
against the code that wrote the journal** — a run pins a hash, not a promise.

The hash is sha256 over the sorted ``(relative_posix_path, sha256(file_bytes))``
pairs plus the SDK version. Three properties follow, and each one is load-bearing:

* **Stable across machines.** No mtimes, no inodes, no walk order, no absolute
  paths — only paths relative to the project root and file content.
* **Sensitive to everything that executes.** The manifest and the lockfile are
  ordinary members of the file set, so changing a tool permission or a dependency
  pin is a new version. The SDK version is folded in separately because it is not
  a file in the tree, yet it is code that runs (§2 lists it in the bundle).
* **Order-independent.** Sorting happens before hashing, so a filesystem that
  enumerates differently produces the same digest.

``pack`` writes a *byte-deterministic* ``.tar.gz`` (mtime 0, uid/gid 0, fixed
mode, sorted members). That is not cosmetic: if the same content could produce
two different archives, the archive could not be content-addressed and
``store_bundle``'s ``<hash[:2]>/<hash>.tar.gz`` layout would be a lie.

**``.env`` is never bundled.** D8 makes config and secrets per-environment
platform state, delivered per run; a bundled ``.env`` would be a secret welded
into an immutable artifact that is then copied to every worker. This is enforced
twice — as a default ignore pattern *and* as a hard veto a ``.ryaignore``
negation cannot undo.

Object storage lives behind one seam: ``store_bundle`` and ``load_bundle`` are
the only two functions that touch archive bytes, via ``_put_archive`` /
``_get_archive`` / ``_archive_exists``, and those three dispatch on a
:class:`BundleStore` — a local content-addressed directory (the default) or the
S3 arm (§5.3), which mirrors ``files_s3.py``: same verbs, a
``bundles/<hash>.tar.gz`` key, metadata staying in the store. Which arm is in
play is *declared* (D8) via :func:`resolve_bundle_store`, never sniffed from the
environment by the functions that move bytes.

The remote arm changes nothing about trust. ``load_bundle`` re-derives the
content hash after materialising and before any handler import, so a swapped
object in a bucket someone else can write to fails with ``E_BUNDLE_MISMATCH``
rather than executing — a bundle fetched over a network is precisely the case
where verifying beats trusting.
"""

from __future__ import annotations

import fnmatch
import gzip
import hashlib
import io
import json
import os
import tarfile
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import RyaError

# Bundle format tag. It is hashed first so that a future change to the digest
# algorithm cannot silently collide with a v1 hash already recorded on a version.
HASH_SCHEMA = "rya-bundle/v1"

MANIFEST_NAME = "rya.agent.yaml"

# A self-describing sidecar written *inside* the archive so an unpacked bundle
# can state its own hash without a store round-trip (a worker checks it before
# it trusts anything). It is excluded from the digest for the obvious reason:
# it contains the digest.
BUNDLE_META_NAME = ".rya-bundle.json"

# Lockfile candidates in preference order. §2 puts the lockfile in the bundle and
# §8 lists "pinned lockfiles" under supply chain: a bundle whose dependency
# resolution can drift is not immutable in any useful sense.
LOCKFILE_NAMES = ("uv.lock", "poetry.lock", "pdm.lock", "requirements.txt")

# gitignore-ish patterns, evaluated in order with **last match wins**, so a
# project's `.ryaignore` (appended after these) can re-include something a
# default excluded. Trailing `/` means "directory and everything under it".
DEFAULT_IGNORE: tuple[str, ...] = (
    ".git/",
    ".hg/",
    ".svn/",
    ".rya/",  # platform state, not source: runs, journal, queue, sealed keys
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    ".venv/",
    "venv/",
    "node_modules/",
    ".env",  # D8 — also hard-vetoed below; see _is_secret_env
    ".env.*",
    "!.env.example",  # the *template* is documentation and carries no secret
    "!.env.sample",
    "!.env.template",
    "*.log",
    "dist/",
    "build/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    "*.egg-info/",
    ".DS_Store",
    BUNDLE_META_NAME,
)

RYAIGNORE_NAME = ".ryaignore"

# Env files that must never enter a bundle whatever the ignore patterns say.
# Everything matching `.env` or `.env.<something>` is treated as secret material
# except these three, which are conventionally committed templates.
_ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})


def _sdk_version() -> str:
    from . import __version__

    return __version__


# --------------------------------------------------------------------------- #
# ignore rules
# --------------------------------------------------------------------------- #
def _is_secret_env(basename: str) -> bool:
    """True for ``.env`` / ``.env.local`` / ``.env.production`` and friends.

    D8: config is per-environment platform state. A ``.env`` inside an immutable,
    content-addressed, widely-copied artifact is a secret leak with a long tail,
    so this check runs *before* pattern matching and cannot be negated away.
    """
    return (basename == ".env" or basename.startswith(".env.")) and basename not in _ENV_TEMPLATES


def _pattern_hits(pattern: str, rel: str, is_dir: bool) -> bool:
    dir_only = pattern.endswith("/")
    pat = pattern.rstrip("/")
    if not pat:
        return False
    if dir_only and not is_dir:
        return False
    if "/" in pat:
        # Anchored at the project root, e.g. "src/generated/*".
        return fnmatch.fnmatch(rel, pat)
    # Unanchored patterns match a basename at any depth, like gitignore.
    return fnmatch.fnmatch(PurePosixPath(rel).name, pat)


def _ignored(rel: str, is_dir: bool, patterns: tuple[str, ...]) -> bool:
    """Last matching pattern wins; a leading ``!`` re-includes."""
    verdict = False
    for raw in patterns:
        negate = raw.startswith("!")
        pat = raw[1:] if negate else raw
        if _pattern_hits(pat, rel, is_dir):
            verdict = not negate
    return verdict


def read_ryaignore(project_root: Path) -> list[str]:
    """Parse ``.ryaignore`` — one glob per line, ``#`` comments, blanks skipped.

    Deliberately ``fnmatch``-based rather than a real gitignore implementation:
    the platform must not grow a dependency to decide what to hash, and the
    honest subset (basename globs, root-anchored globs, ``dir/``, ``!`` negation)
    covers what an agent project needs. Like gitignore, a negation inside an
    excluded directory does not resurrect it — the directory is pruned before its
    children are seen.
    """
    path = project_root / RYAIGNORE_NAME
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


# --------------------------------------------------------------------------- #
# the artifact
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Bundle:
    """An immutable, content-hashed deployment artifact (§2's first noun).

    Field names are camelCase where they are persisted onto a version record, so
    the record, the API payload and this object read identically.
    """

    hash: str
    files: dict[str, str]  # relative posix path -> sha256 of the file's bytes
    manifest: dict[str, Any]  # the parsed rya.agent.yaml, unvalidated
    sdkVersion: str
    lockfile: str | None  # relative path of the lockfile that was included
    sizeBytes: int
    entrypoint: str
    root: Path | None = None  # where the bytes live locally, if anywhere
    ignored: tuple[str, ...] = field(default_factory=tuple)

    @property
    def agent(self) -> str:
        """The agent name from the manifest — the version's namespace (D11)."""
        return str(self.manifest.get("name") or "")

    @property
    def fileCount(self) -> int:
        return len(self.files)

    def to_dict(self) -> dict:
        """JSON-safe summary for ``--json`` output and the version record."""
        return {
            "hash": self.hash,
            "agent": self.agent,
            "entrypoint": self.entrypoint,
            "sdkVersion": self.sdkVersion,
            "lockfile": self.lockfile,
            "sizeBytes": self.sizeBytes,
            "fileCount": self.fileCount,
            "manifestVersion": self.manifest.get("version"),
        }


def content_hash(files: dict[str, str], *, sdk_version: str) -> str:
    """Digest a ``path -> file digest`` map. Exposed so the property is testable.

    Sorting here — not at the call site — is what makes the hash independent of
    walk order, so two checkouts of the same commit on two machines agree.
    """
    h = hashlib.sha256()
    h.update(HASH_SCHEMA.encode() + b"\n")
    h.update(b"sdk=" + sdk_version.encode() + b"\n")
    for rel in sorted(files):
        h.update(rel.encode() + b"\0" + files[rel].encode() + b"\n")
    return h.hexdigest()


def build_bundle(project_root: Path, *, ignore: list[str] | None = None) -> Bundle:
    """Walk ``project_root`` and produce a content-hashed :class:`Bundle`.

    ``ignore`` is appended after the defaults and after ``.ryaignore``, so a
    caller (CI, ``rya dev``) always has the last word.
    """
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise RyaError(
            "E_BUNDLE_NOT_FOUND",
            f"Project root '{root}' is not a directory.",
            hint="Point `rya deploy` at a directory containing rya.agent.yaml.",
        )

    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        # A bundle without a manifest has no agent name, so it has no version
        # namespace (D11: one manifest per agent) and cannot be promoted.
        raise RyaError(
            "E_MANIFEST_NOT_FOUND",
            f"No {MANIFEST_NAME} in {root}.",
            hint=f"Run `rya create <name>`, or deploy from the directory holding {MANIFEST_NAME}.",
        )

    import yaml

    try:
        manifest = yaml.safe_load(manifest_path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise RyaError(
            "E_MANIFEST_INVALID",
            f"{MANIFEST_NAME} is not valid YAML: {exc}",
            hint="Run `rya dev --check` for the precise line, then re-deploy.",
        ) from exc
    if not isinstance(manifest, dict):
        raise RyaError(
            "E_MANIFEST_INVALID",
            f"{MANIFEST_NAME} must be a YAML mapping.",
            hint="Run `rya dev --check` to validate the manifest before deploying.",
        )

    patterns = (*DEFAULT_IGNORE, *read_ryaignore(root), *(ignore or ()))

    files: dict[str, str] = {}
    size = 0
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        kept = []
        for d in sorted(dirnames):
            rel = d if rel_dir == "." else f"{rel_dir}/{d}"
            # Symlinked directories are skipped outright: they cannot be
            # reproduced in a deterministic tar and following them risks
            # escaping the project root entirely.
            if (Path(dirpath) / d).is_symlink() or _ignored(rel, True, patterns):
                continue
            kept.append(d)
        dirnames[:] = kept  # prune in place — os.walk contract

        for name in sorted(filenames):
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if _is_secret_env(name):
                continue
            if _ignored(rel, False, patterns):
                continue
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            data = path.read_bytes()
            files[rel] = hashlib.sha256(data).hexdigest()
            size += len(data)

    entrypoint = str(manifest.get("entrypoint") or "src/agent.py")
    if entrypoint not in files:
        # Fail at build time rather than at worker start (§6 "fails closed on a
        # manifest mismatch"): the operator who can fix it is the one deploying.
        raise RyaError(
            "E_ENTRYPOINT_NOT_FOUND",
            f"Manifest entrypoint '{entrypoint}' is not in the bundle.",
            hint=f"Create {entrypoint}, fix `entrypoint:` in {MANIFEST_NAME}, "
            f"or remove the ignore rule that excluded it.",
        )

    lockfile = next((n for n in LOCKFILE_NAMES if n in files), None)
    sdk_version = _sdk_version()
    return Bundle(
        hash=content_hash(files, sdk_version=sdk_version),
        files=files,
        manifest=manifest,
        sdkVersion=sdk_version,
        lockfile=lockfile,
        sizeBytes=size,
        entrypoint=entrypoint,
        root=root,
        ignored=patterns,
    )


# --------------------------------------------------------------------------- #
# packing — byte-deterministic archives
# --------------------------------------------------------------------------- #
def _meta_bytes(bundle: Bundle) -> bytes:
    """The in-archive sidecar. Sorted keys, no timestamps: part of the bytes we
    promise are reproducible, so nothing here may vary between two packs."""
    doc = {**bundle.to_dict(), "files": bundle.files, "schema": HASH_SCHEMA}
    return (json.dumps(doc, sort_keys=True, indent=2) + "\n").encode()


def _tarinfo(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    # Everything that could carry environment into the archive is zeroed. The
    # digest covers content only, so the archive must too — otherwise one hash
    # could map to many archives and content-addressing breaks.
    info.mtime = 0
    info.mode = 0o644
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.type = tarfile.REGTYPE
    return info


def pack(bundle: Bundle, dest: Path) -> Path:
    """Write ``bundle`` as a deterministic ``.tar.gz``. Returns the archive path.

    If ``dest`` is an existing directory the archive lands inside it as
    ``<hash>.tar.gz``. Identical content packs to byte-identical archives (see
    ``tests/test_bundles.py``) — that is what makes the hash trustworthy as an
    address rather than merely as a label.
    """
    if bundle.root is None:
        raise RyaError(
            "E_BUNDLE_NOT_FOUND",
            f"Bundle {bundle.hash[:12]} has no local root to read bytes from.",
            hint="Build it with `build_bundle(project_root)` before packing, "
            "or fetch it with `load_bundle(hash, archive_root, dest)`.",
        )
    dest = Path(dest)
    archive = dest / f"{bundle.hash}.tar.gz" if dest.is_dir() else dest
    archive.parent.mkdir(parents=True, exist_ok=True)

    buf = io.BytesIO()
    # filename="" and mtime=0 keep the gzip header free of the output path and
    # the clock; Python already writes a constant OS byte.
    # GNU_FORMAT is pinned rather than left to the default, which has changed
    # across Python versions — the archive bytes must not depend on that.
    with (
        gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=9) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tf,
    ):
        for rel in sorted(bundle.files):
            data = (bundle.root / rel).read_bytes()
            tf.addfile(_tarinfo(rel, len(data)), io.BytesIO(data))
        meta = _meta_bytes(bundle)
        tf.addfile(_tarinfo(BUNDLE_META_NAME, len(meta)), io.BytesIO(meta))
    archive.write_bytes(buf.getvalue())
    return archive


def _check_member(member: tarfile.TarInfo) -> None:
    """Reject the classic tar hazards before a single byte is written.

    An archive is untrusted input even when it came from our own store: the
    worker unpacks it as the platform, so a traversal or a symlink out of the
    tree is a platform compromise, not a tenant one (D13 contains a *buggy*
    tenant, not a hostile one — so this boundary has to hold on its own).
    """
    name = member.name
    hint = "Re-create the bundle with `rya deploy`; a bundle only ever contains regular files."

    def reject(why: str) -> None:
        raise RyaError("E_VALIDATION", f"Unsafe bundle member '{name}': {why}.", hint=hint)

    if not name or name in (".", "./"):
        reject("empty name")
    if name.startswith(("/", "\\")) or PurePosixPath(name).is_absolute():
        reject("absolute path")
    if len(name) > 1 and name[1] == ":":
        reject("drive-qualified path")
    parts = PurePosixPath(name).parts
    if ".." in parts:
        reject("'..' traversal")
    if member.issym() or member.islnk():
        reject("symlink or hard link")
    if not (member.isfile() or member.isdir()):
        reject("not a regular file or directory")


def unpack(archive: Path, dest: Path, *, max_total_bytes: int | None = None) -> Path:
    """Extract a bundle archive into ``dest`` (created if absent). Returns ``dest``.

    Members are validated *first* and extracted by hand — no ``extractall`` — so
    a hostile member cannot land before the archive is rejected.

    ``max_total_bytes`` caps the *uncompressed* total and is checked before any
    byte is written. A local ``rya deploy`` unpacks an archive it just built, so
    it leaves this unset; the publish endpoint accepts archives from the network,
    where gzip's compression ratio is an amplification factor and "the upload was
    under the limit" says nothing about what it expands to.
    """
    archive = Path(archive)
    if not archive.is_file():
        raise RyaError(
            "E_BUNDLE_NOT_FOUND",
            f"Bundle archive '{archive}' does not exist.",
            hint="Check the archive root, or re-upload the bundle with `rya deploy`.",
        )
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    resolved_dest = dest.resolve()

    with tarfile.open(archive, "r:gz") as tf:
        members = tf.getmembers()
        for m in members:
            _check_member(m)
        if max_total_bytes is not None:
            total = sum(max(0, int(m.size or 0)) for m in members)
            if total > max_total_bytes:
                raise RyaError(
                    "E_VALIDATION",
                    f"Bundle expands to {total} bytes, over the {max_total_bytes}-byte limit.",
                    hint="Trim the project (a `.ryaignore` excludes data and fixtures from the "
                    "bundle), or raise the limit on the platform.",
                )
        for m in members:
            target = dest / m.name
            # Belt and braces after the name checks: the realised path must still
            # be inside dest (catches anything the string checks missed).
            if not target.resolve().is_relative_to(resolved_dest):
                raise RyaError(
                    "E_VALIDATION",
                    f"Unsafe bundle member '{m.name}': escapes the destination directory.",
                    hint="Re-create the bundle with `rya deploy`.",
                )
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(m)
            target.write_bytes(b"" if src is None else src.read())
    return dest


def verify(path_or_archive: Path, expected_hash: str) -> None:
    """Recompute the content hash and raise ``E_BUNDLE_MISMATCH`` on drift.

    Accepts a project directory or a ``.tar.gz``. The archive form unpacks to a
    temp dir and rebuilds, because trusting the sidecar would make it a
    self-certifying artifact — the whole point of D12 is that the *content*
    proves the version, not a field inside it.
    """
    path = Path(path_or_archive)
    if path.is_dir():
        actual = build_bundle(path).hash
    else:
        with tempfile.TemporaryDirectory(prefix="rya-verify-") as td:
            unpack(path, Path(td))
            actual = build_bundle(Path(td)).hash
    if actual != expected_hash:
        raise RyaError(
            "E_BUNDLE_MISMATCH",
            f"Bundle content hash {actual[:12]} does not match expected {expected_hash[:12]}.",
            hint="The artifact was modified after it was recorded. Re-deploy to create a new "
            "immutable version instead of editing one — versions are immutable by design (D12).",
        )


# --------------------------------------------------------------------------- #
# the archive store: a local directory or an object store (§5.3)
# --------------------------------------------------------------------------- #
# Declared, not ambient (D8): the resolver below is handed an env mapping and is
# the ONLY thing here that reads one. Names mirror `files_s3.py`'s
# RYA_FILES_S3_BUCKET so an operator configures both offloads the same way.
S3_BUCKET_ENV = "RYA_BUNDLES_S3_BUCKET"
S3_PREFIX_ENV = "RYA_BUNDLES_S3_PREFIX"
S3_REGION_ENV = "RYA_BUNDLES_S3_REGION"
# S3-compatible stores that are not S3: MinIO, Ceph, R2. Empty means real AWS,
# which must keep boto3's own endpoint resolution — see `_s3_client`.
S3_ENDPOINT_ENV = "RYA_BUNDLES_S3_ENDPOINT"

DEFAULT_S3_PREFIX = "bundles"

# S3 error codes that mean "the store answered, and the object is not there".
# Everything else — no credentials, no such bucket, DNS, timeouts — is the store
# itself failing, which is a different operator fix and so a different code.
_S3_ABSENT_CODES = frozenset({"404", "NoSuchKey"})


def default_archive_root(project_root: Path) -> Path:
    """Where bundle archives live for a local/self-hosted deployment."""
    return Path(project_root) / ".rya" / "bundles"


def bundle_archive_path(bundle_hash: str, archive_root: Path) -> Path:
    """``<archive_root>/<hash[:2]>/<hash>.tar.gz`` — two-level fan-out so a
    workspace with thousands of versions does not get one giant directory."""
    return Path(archive_root) / bundle_hash[:2] / f"{bundle_hash}.tar.gz"


def bundle_archive_key(bundle_hash: str, prefix: str = DEFAULT_S3_PREFIX) -> str:
    """``<prefix>/<hash>.tar.gz`` — the object-store address of an archive.

    Flat rather than fanned out like the local layout: a bucket has no directory
    to get slow, and a flat key makes ``s3://bucket/bundles/<hash>.tar.gz``
    something an operator can paste into `aws s3` from a version record.
    """
    return f"{prefix.strip('/')}/{bundle_hash}.tar.gz" if prefix.strip("/") else f"{bundle_hash}.tar.gz"


@dataclass(frozen=True)
class BundleStore:
    """Where bundle archives live. Resolved once, then passed as data.

    Two arms, deliberately not a plugin point: a ``local`` content-addressed
    directory and ``s3``. Anything else is a new arm on the same three functions
    below, not a registry.
    """

    kind: str                    # "local" | "s3"
    root: Path | None = None     # local: the archive root
    bucket: str = ""             # s3
    prefix: str = DEFAULT_S3_PREFIX
    region: str = ""             # "" = boto3's own resolution chain
    endpoint: str = ""           # "" = real AWS; set for MinIO/Ceph/R2

    @property
    def is_local(self) -> bool:
        return self.kind == "local"

    def describe(self) -> str:
        """A location an operator can act on — it lands in error messages."""
        if self.is_local:
            return str(self.root)
        base = f"s3://{self.bucket}/{self.prefix.strip('/')}"
        # Appended only when set: an operator debugging a real-AWS deployment
        # should not have to read past an empty "@".
        return f"{base} @ {self.endpoint}" if self.endpoint else base


def resolve_bundle_store(
    project_root: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    root: Path | None = None,
) -> BundleStore:
    """Resolve the declared bundle store, defaulting to the local directory.

    ``env`` is the declared environment (D8). It is omitted only by callers that
    have not been re-pointed at an explicit ``RunConfig`` yet, in which case the
    one sanctioned ambient shim — ``config.legacy_env()`` — supplies it.

    An explicit ``root`` wins over a declared bucket: a caller that names a
    directory has declared a local store, and silently uploading to S3 instead
    would be exactly the ambient surprise D8 exists to stop.
    """
    if root is not None:
        return BundleStore(kind="local", root=Path(root))
    if env is None:
        from .config import legacy_env

        env = legacy_env()
    bucket = (env.get(S3_BUCKET_ENV) or "").strip()
    if bucket:
        return BundleStore(
            kind="s3",
            bucket=bucket,
            prefix=(env.get(S3_PREFIX_ENV) or DEFAULT_S3_PREFIX).strip(),
            region=(env.get(S3_REGION_ENV) or "").strip(),
            endpoint=(env.get(S3_ENDPOINT_ENV) or "").strip(),
        )
    if project_root is None:
        raise RyaError(
            "E_BUNDLE_STORE",
            "No bundle store is declared: neither an archive root nor "
            f"{S3_BUCKET_ENV}.",
            hint=f"Pass an archive root, or set {S3_BUCKET_ENV} to the bucket "
            "holding this workspace's bundle archives.",
        )
    return BundleStore(kind="local", root=default_archive_root(project_root))


def _as_store(archive_root: BundleStore | Path | str) -> BundleStore:
    """Accept a plain path where an archive root was always accepted.

    ``store_bundle(bundle, path)`` predates the object store and is what the CLI
    and every existing caller pass; a path keeps meaning "the local store rooted
    here" rather than becoming a second way to spell a resolver call.
    """
    if isinstance(archive_root, BundleStore):
        return archive_root
    return BundleStore(kind="local", root=Path(archive_root))


# --------------------------------------------------------------------------- #
# the object-store arm — boto3 is optional, mirroring files_s3.py
# --------------------------------------------------------------------------- #
def _s3_client(store: BundleStore):
    """The boto3 S3 client for ``store``.

    boto3 is an optional dependency, so a missing one is reported as the
    configuration error it is. An ImportError traceback out of a worker's cold
    start tells an operator nothing about the bucket they just configured.
    """
    try:
        import boto3
    except ImportError as exc:
        raise RyaError(
            "E_BUNDLE_STORE",
            f"{S3_BUCKET_ENV} is set to '{store.bucket}' but boto3 is not installed.",
            hint="Install boto3 (`pip install 'rya[s3]'`) on every "
            f"api and worker image, or unset {S3_BUCKET_ENV} to keep bundle archives local.",
        ) from exc
    kwargs: dict = {"region_name": store.region or None}
    if store.endpoint:
        # Path-style addressing has to be passed HERE, not configured ambiently:
        # botocore gives `s3.addressing_style` no environment variable at all
        # (its DEFAULT_S3_CONFIG_VARS entry has an empty env slot), so the only
        # ways to set it are ~/.aws/config or this Config object. Left at the
        # default, the endpoint ruleset templates a custom endpoint as
        # `{scheme}://{Bucket}.{authority}` for any non-IP host — so bucket
        # `rya-bundles` at http://minio:9000 becomes http://rya-bundles.minio:9000
        # and fails to resolve. Real AWS wants the virtual-host form, which is
        # why this branch is keyed on an endpoint being declared at all.
        from botocore.config import Config

        kwargs["endpoint_url"] = store.endpoint
        kwargs["config"] = Config(s3={"addressing_style": "path"})
    try:
        return boto3.client("s3", **kwargs)
    except Exception as exc:  # bad region, unresolvable credentials chain, ...
        raise _store_unreachable(store, "connect to", exc) from exc


def _s3_error_code(exc: Exception) -> str:
    """The S3 error code off a botocore ``ClientError`` without importing botocore.

    Duck-typed on the ``response`` attribute for the same reason boto3 itself is
    a deferred import: this module must import on an image that has neither.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str((response.get("Error") or {}).get("Code") or "")
    return ""


def _store_unreachable(store: BundleStore, action: str, exc: Exception) -> RyaError:
    """E_BUNDLE_STORE — the store failed, as distinct from the artifact being absent.

    ``files_s3.py`` collapses both into ``None``; here they must stay apart,
    because "re-deploy to re-upload the artifact" and "fix the bucket/credentials"
    are different jobs for different people.
    """
    code = _s3_error_code(exc)
    return RyaError(
        "E_BUNDLE_STORE",
        f"Cannot {action} bundle store {store.describe()}: "
        f"{f'[{code}] ' if code else ''}{exc}",
        hint=f"Check that the bucket exists, that {S3_BUCKET_ENV}/{S3_REGION_ENV} name the "
        "right one, and that this process's AWS credentials can read and write it "
        f"(`aws s3 ls {store.describe()}/`). Bundle archives are not cached locally, "
        "so a worker cannot start while the store is unreachable.",
    )


# The object-store seam. These three functions are the ONLY places bundle bytes
# are read, written or probed for; every arm is a branch here and nothing else in
# this module — or in worker.py — learns which store it is talking to.
def _put_archive(bundle_hash: str, data: bytes, store: BundleStore) -> Path | str:
    if store.is_local:
        path = bundle_archive_path(bundle_hash, store.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path
    key = bundle_archive_key(bundle_hash, store.prefix)
    client = _s3_client(store)
    try:
        client.put_object(Bucket=store.bucket, Key=key, Body=data,
                          ContentType="application/gzip")
    except Exception as exc:
        raise _store_unreachable(store, "write to", exc) from exc
    return f"s3://{store.bucket}/{key}"


def _get_archive(bundle_hash: str, store: BundleStore) -> bytes | None:
    """The archive's bytes, or None when the store is reachable and has no such
    object. A store that cannot be reached raises instead of returning None."""
    if store.is_local:
        path = bundle_archive_path(bundle_hash, store.root)
        return path.read_bytes() if path.is_file() else None
    key = bundle_archive_key(bundle_hash, store.prefix)
    client = _s3_client(store)
    try:
        return client.get_object(Bucket=store.bucket, Key=key)["Body"].read()
    except Exception as exc:
        if _s3_error_code(exc) in _S3_ABSENT_CODES:
            return None
        raise _store_unreachable(store, "read from", exc) from exc


def _archive_exists(bundle_hash: str, store: BundleStore) -> bool:
    """Cheap existence probe for ``store_bundle``'s idempotency.

    A HEAD, never a GET: re-deploying an unchanged tree is the common case, and
    it must not pull a whole archive body across the network to discover there is
    nothing to do.
    """
    if store.is_local:
        return bundle_archive_path(bundle_hash, store.root).is_file()
    key = bundle_archive_key(bundle_hash, store.prefix)
    client = _s3_client(store)
    try:
        client.head_object(Bucket=store.bucket, Key=key)
        return True
    except Exception as exc:
        if _s3_error_code(exc) in _S3_ABSENT_CODES:
            return False
        raise _store_unreachable(store, "read from", exc) from exc


def store_bundle(bundle: Bundle, archive_root: BundleStore | Path) -> Path | str:
    """Write ``bundle`` into the content-addressed archive store. Idempotent.

    ``archive_root`` is a :class:`BundleStore` or, as before, a local directory.
    Returns the local path or the ``s3://`` URI the archive now lives at.

    An existing archive is left alone rather than rewritten: the address *is* the
    content, so a second write would be the same bytes, and skipping it keeps
    ``rya deploy`` cheap to retry (which pairs with ``version_create``'s
    idempotency on ``(agent, bundleHash)``).
    """
    store = _as_store(archive_root)
    if _archive_exists(bundle.hash, store):
        if store.is_local:
            return bundle_archive_path(bundle.hash, store.root)
        return f"s3://{store.bucket}/{bundle_archive_key(bundle.hash, store.prefix)}"
    with tempfile.TemporaryDirectory(prefix="rya-pack-") as td:
        archive = pack(bundle, Path(td) / f"{bundle.hash}.tar.gz")
        data = archive.read_bytes()
    return _put_archive(bundle.hash, data, store)


def load_bundle(bundle_hash: str, archive_root: BundleStore | Path, dest: Path) -> Path:
    """Materialise a stored bundle into ``dest`` and verify it. Returns ``dest``.

    This is the worker's entry point (§11 item 8: "load a pinned version, report
    its content hash"). It verifies rather than trusts, so a corrupted or swapped
    archive fails with ``E_BUNDLE_MISMATCH`` *before* any handler code is
    imported — ``load_agent`` mutates ``sys.path`` and never unloads
    (``runtime/engine.py:79-84``), so there is no second chance after the import.
    That ordering is what makes the object-store arm safe to point at a bucket
    the platform does not exclusively own.
    """
    store = _as_store(archive_root)
    data = _get_archive(bundle_hash, store)
    if data is None:
        raise RyaError(
            "E_BUNDLE_NOT_FOUND",
            f"No bundle archive for hash {bundle_hash[:12]} in {store.describe()}.",
            hint="The store is reachable but the version's artifact is missing. Re-run "
            "`rya deploy` from the source tree to re-upload it, or point the worker at "
            "the archive root / bucket the artifact was uploaded to.",
        )
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / f"{bundle_hash}.tar.gz"
    archive.write_bytes(data)
    try:
        unpack(archive, dest)
    finally:
        archive.unlink(missing_ok=True)
    verify(dest, bundle_hash)
    return dest
