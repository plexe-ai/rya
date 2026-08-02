"""Content-hashed bundles — PLATFORM_DESIGN D12 / §9's second pipeline step.

The properties under test are the ones the rest of the deployment pipeline
assumes without checking: the hash is a function of content alone, the archive is
a function of the hash alone, and an archive is untrusted input.
"""

import gzip
import io
import os
import tarfile
from pathlib import Path

import pytest

from rya.bundles import (
    BUNDLE_META_NAME,
    Bundle,
    BundleStore,
    build_bundle,
    bundle_archive_key,
    bundle_archive_path,
    content_hash,
    load_bundle,
    pack,
    store_bundle,
    unpack,
    verify,
)
from rya.cli import scaffold
from rya.errors import RyaError


def _scaffold(path: Path, name: str) -> Path:
    """A throwaway project. D20's tests need SAME content in two tenants, so they
    build from one scaffold rather than reusing the shared `project` fixture."""
    path.mkdir(parents=True, exist_ok=True)
    scaffold.write_project(path, name, template="demo")
    return path


# --------------------------------------------------------------------------- #
# the hash
# --------------------------------------------------------------------------- #
def test_hash_is_stable_across_builds(project):
    a = build_bundle(project)
    b = build_bundle(project)
    assert a.hash == b.hash
    assert a.files == b.files
    assert len(a.hash) == 64


def test_hash_ignores_mtime(project):
    before = build_bundle(project).hash
    # Stable "across machines, checkout order and mtimes" — a fresh checkout
    # rewrites every timestamp and must still deploy to the same version.
    os.utime(project / "src" / "agent.py", (0, 0))
    assert build_bundle(project).hash == before


def test_hash_changes_when_any_file_changes(project):
    before = build_bundle(project).hash
    (project / "src" / "tools.py").write_text("# nudged\n")
    assert build_bundle(project).hash != before


def test_hash_changes_when_manifest_changes(project):
    # The manifest is an ordinary member of the file set, so tightening a tool
    # permission is a new immutable version rather than a mutation of one.
    before = build_bundle(project).hash
    manifest = project / "rya.agent.yaml"
    manifest.write_text(manifest.read_text() + "\n# comment\n")
    assert build_bundle(project).hash != before


def test_hash_changes_when_lockfile_changes(project):
    (project / "uv.lock").write_text("version = 1\n")
    with_lock = build_bundle(project)
    assert with_lock.lockfile == "uv.lock"
    (project / "uv.lock").write_text("version = 2\n")
    assert build_bundle(project).hash != with_lock.hash


def test_hash_is_independent_of_walk_order(project):
    bundle = build_bundle(project)
    shuffled = dict(reversed(list(bundle.files.items())))
    assert list(shuffled) != list(bundle.files)  # genuinely a different order
    assert content_hash(shuffled, sdk_version=bundle.sdkVersion) == bundle.hash


def test_hash_covers_the_sdk_version(project):
    bundle = build_bundle(project)
    # §2 lists the SDK version inside the bundle: it is code that runs, so it is
    # part of the artifact's identity even though it is not a file in the tree.
    assert content_hash(bundle.files, sdk_version="99.0.0") != bundle.hash


def test_bundle_metadata(project):
    bundle = build_bundle(project)
    assert isinstance(bundle, Bundle)
    assert bundle.agent == "test-agent"
    assert bundle.entrypoint == "src/agent.py"
    assert bundle.entrypoint in bundle.files
    assert "rya.agent.yaml" in bundle.files
    assert bundle.sizeBytes > 0
    assert bundle.fileCount == len(bundle.files)
    assert bundle.to_dict()["hash"] == bundle.hash


# --------------------------------------------------------------------------- #
# what is and is not bundled
# --------------------------------------------------------------------------- #
def test_env_is_never_bundled(project):
    # D8: config and secrets are per-environment platform state. A .env inside an
    # immutable artifact copied to every worker is a secret leak with a long tail.
    assert (project / ".env").is_file()
    files = build_bundle(project).files
    assert ".env" not in files
    assert ".env.example" in files  # the template carries no secret


def test_ryaignore_cannot_reinclude_env(project):
    (project / ".ryaignore").write_text("!.env\n")
    assert ".env" not in build_bundle(project).files


def test_secret_env_variants_are_never_bundled(project):
    (project / ".env.production").write_text("OPENAI_API_KEY=sk-live\n")
    (project / ".env.local").write_text("X=1\n")
    files = build_bundle(project).files
    assert ".env.production" not in files
    assert ".env.local" not in files


def test_platform_state_and_caches_are_excluded(project):
    (project / ".rya" / "runs").mkdir(parents=True)
    (project / ".rya" / "runs" / "run_x.json").write_text("{}")
    (project / "src" / "__pycache__").mkdir()
    (project / "src" / "__pycache__" / "agent.cpython-312.pyc").write_bytes(b"\x00")
    (project / "debug.log").write_text("noise")
    (project / ".git").mkdir()
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main")

    files = build_bundle(project).files
    assert not [f for f in files if f.startswith((".rya/", ".git/"))]
    assert not [f for f in files if "__pycache__" in f or f.endswith((".pyc", ".log"))]


def test_ryaignore_is_honoured(project):
    before = build_bundle(project)
    assert "src/tools.py" in before.files
    (project / ".ryaignore").write_text("# drop generated code\nsrc/tools.py\n*.md\n")
    after = build_bundle(project)
    assert "src/tools.py" not in after.files
    assert "README.md" not in after.files
    assert after.hash != before.hash


def test_explicit_ignore_argument_wins(project):
    files = build_bundle(project, ignore=["tests/"]).files
    assert not [f for f in files if f.startswith("tests/")]


def test_missing_manifest_is_a_manifest_error(tmp_path):
    (tmp_path / "src").mkdir()
    with pytest.raises(RyaError) as exc:
        build_bundle(tmp_path)
    assert exc.value.code == "E_MANIFEST_NOT_FOUND"


def test_missing_entrypoint_fails_at_build_time(project):
    (project / "src" / "agent.py").unlink()
    with pytest.raises(RyaError) as exc:
        build_bundle(project)
    assert exc.value.code == "E_ENTRYPOINT_NOT_FOUND"
    assert exc.value.hint


# --------------------------------------------------------------------------- #
# packing
# --------------------------------------------------------------------------- #
def test_pack_is_byte_deterministic(project, tmp_path):
    bundle = build_bundle(project)
    out = tmp_path / "out"
    out.mkdir()
    first = pack(bundle, out / "one.tar.gz")
    second = pack(bundle, out / "two.tar.gz")
    # If the same content could produce two archives, the hash would not be an
    # address and store_bundle's <hash>.tar.gz layout would be a lie.
    assert first.read_bytes() == second.read_bytes()


def test_pack_zeroes_metadata(project, tmp_path):
    archive = pack(build_bundle(project), tmp_path / "b.tar.gz")
    with tarfile.open(archive, "r:gz") as tf:
        members = tf.getmembers()
    assert members  # sanity
    for m in members:
        assert m.mtime == 0
        assert m.uid == 0 and m.gid == 0
        assert m.uname == "" and m.gname == ""
        assert m.isfile()
    names = [m.name for m in members]
    assert names[:-1] == sorted(names[:-1])
    assert names[-1] == BUNDLE_META_NAME


def test_pack_unpack_roundtrip_preserves_the_hash(project, tmp_path):
    bundle = build_bundle(project)
    archive = pack(bundle, tmp_path / "b.tar.gz")
    dest = unpack(archive, tmp_path / "unpacked")
    rebuilt = build_bundle(dest)
    assert rebuilt.hash == bundle.hash
    assert rebuilt.files == bundle.files
    verify(archive, bundle.hash)
    verify(dest, bundle.hash)


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
def test_verify_raises_on_a_tampered_file(project):
    bundle = build_bundle(project)
    verify(project, bundle.hash)
    (project / "src" / "agent.py").write_text("# swapped out from under the version\n")
    with pytest.raises(RyaError) as exc:
        verify(project, bundle.hash)
    assert exc.value.code == "E_BUNDLE_MISMATCH"


def test_verify_raises_on_a_tampered_archive(project, tmp_path):
    bundle = build_bundle(project)
    archive = pack(bundle, tmp_path / "b.tar.gz")
    # Repack with one file altered but keep the sidecar's advertised hash: the
    # sidecar is never trusted, the content is recomputed.
    tampered = build_bundle(project)
    (project / "src" / "models.py").write_text("# nope\n")
    swapped = pack(build_bundle(project), tmp_path / "c.tar.gz")
    assert swapped.read_bytes() != archive.read_bytes()
    with pytest.raises(RyaError) as exc:
        verify(swapped, tampered.hash)
    assert exc.value.code == "E_BUNDLE_MISMATCH"


# --------------------------------------------------------------------------- #
# tarball hazards — an archive is untrusted input even from our own store
# --------------------------------------------------------------------------- #
def _hostile_archive(path, info, payload=b"x"):
    buf = io.BytesIO()
    with (
        gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tf,
    ):
        if info.isfile():
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        else:
            tf.addfile(info)
    path.write_bytes(buf.getvalue())
    return path


def test_unpack_refuses_traversal(tmp_path):
    info = tarfile.TarInfo("../escaped.txt")
    archive = _hostile_archive(tmp_path / "evil.tar.gz", info)
    with pytest.raises(RyaError) as exc:
        unpack(archive, tmp_path / "dest")
    assert exc.value.code == "E_VALIDATION"
    assert not (tmp_path / "escaped.txt").exists()


def test_unpack_refuses_absolute_paths(tmp_path):
    info = tarfile.TarInfo("/tmp/rya-absolute.txt")
    archive = _hostile_archive(tmp_path / "abs.tar.gz", info)
    with pytest.raises(RyaError) as exc:
        unpack(archive, tmp_path / "dest")
    assert exc.value.code == "E_VALIDATION"


def test_unpack_refuses_symlinks(tmp_path):
    info = tarfile.TarInfo("secrets")
    info.type = tarfile.SYMTYPE
    info.linkname = "/etc/passwd"
    archive = _hostile_archive(tmp_path / "link.tar.gz", info)
    with pytest.raises(RyaError) as exc:
        unpack(archive, tmp_path / "dest")
    assert exc.value.code == "E_VALIDATION"
    assert not (tmp_path / "dest" / "secrets").exists()


def test_unpack_refuses_device_files(tmp_path):
    info = tarfile.TarInfo("dev/null")
    info.type = tarfile.CHRTYPE
    archive = _hostile_archive(tmp_path / "dev.tar.gz", info)
    with pytest.raises(RyaError) as exc:
        unpack(archive, tmp_path / "dest")
    assert exc.value.code == "E_VALIDATION"


def test_unpack_rejects_a_missing_archive(tmp_path):
    with pytest.raises(RyaError) as exc:
        unpack(tmp_path / "nope.tar.gz", tmp_path / "dest")
    assert exc.value.code == "E_BUNDLE_NOT_FOUND"


def test_unpack_caps_the_uncompressed_total(tmp_path):
    """Gzip's ratio is an amplification factor, so a small upload says nothing
    about what it expands to. The cap is checked before any byte is written."""
    info = tarfile.TarInfo("big.bin")
    archive = _hostile_archive(tmp_path / "bomb.tar.gz", info, payload=b"\0" * 5000)
    dest = tmp_path / "dest"

    with pytest.raises(RyaError) as exc:
        unpack(archive, dest, max_total_bytes=1000)
    assert exc.value.code == "E_VALIDATION"
    assert "5000" in exc.value.message
    assert not (dest / "big.bin").exists()  # nothing landed


def test_unpack_allows_a_total_within_the_cap(tmp_path):
    info = tarfile.TarInfo("small.bin")
    archive = _hostile_archive(tmp_path / "ok.tar.gz", info, payload=b"\0" * 100)
    dest = unpack(archive, tmp_path / "dest", max_total_bytes=1000)
    assert (dest / "small.bin").read_bytes() == b"\0" * 100


def test_unpack_without_a_cap_is_unchanged(project, tmp_path):
    """The local `rya deploy` path unpacks an archive it just built and passes no
    cap; that default must stay uncapped rather than becoming a hidden limit."""
    bundle = build_bundle(project)
    archive = pack(bundle, tmp_path / "b.tar.gz")
    assert build_bundle(unpack(archive, tmp_path / "dest")).hash == bundle.hash


def test_source_symlinks_are_not_bundled(project):
    link = project / "src" / "linked.py"
    try:
        link.symlink_to(project / "src" / "agent.py")
    except OSError:  # pragma: no cover - no symlink support
        pytest.skip("symlinks unavailable")
    assert "src/linked.py" not in build_bundle(project).files


# --------------------------------------------------------------------------- #
# the content-addressed archive store
# --------------------------------------------------------------------------- #
def test_store_and_load_bundle(project, tmp_path):
    root = tmp_path / "archives"
    bundle = build_bundle(project)
    path = store_bundle(bundle, root)
    assert path == bundle_archive_path(bundle.hash, root)
    assert path.parent.name == bundle.hash[:2]

    # Idempotent: the address IS the content, so a second store is a no-op.
    assert store_bundle(bundle, root) == path

    dest = load_bundle(bundle.hash, root, tmp_path / "worker-workdir")
    assert build_bundle(dest).hash == bundle.hash
    assert not list(dest.glob("*.tar.gz"))  # the archive is not left behind


def test_load_bundle_missing_hash(tmp_path):
    with pytest.raises(RyaError) as exc:
        load_bundle("0" * 64, tmp_path / "archives", tmp_path / "dest")
    assert exc.value.code == "E_BUNDLE_NOT_FOUND"
    assert "rya deploy" in exc.value.hint


# --------------------------------------------------------------------------- #
# D20 — the tenant namespace
#
# A bundle hash is content-addressed, so two tenants that publish the same bytes
# derive the SAME hash independently. Before D20 that made one tenant's archive
# readable by any other that could compute the hash — which, for a public
# template or a shared base project, is anyone.
# --------------------------------------------------------------------------- #
def test_a_workspace_scoped_store_cannot_resolve_another_tenants_archive(tmp_path):
    """The Phase 1 exit criterion, asserted rather than reasoned about."""
    root = tmp_path / "archives"
    bundle = build_bundle(_scaffold(tmp_path / "proj", "shared"))

    acme = BundleStore(kind="local", root=root, workspace="ws_acme")
    other = BundleStore(kind="local", root=root, workspace="ws_other")

    stored = store_bundle(bundle, acme)
    assert "ws_acme" in str(stored)

    # Same bytes, same hash, different tenant -> not resolvable.
    with pytest.raises(RyaError) as exc:
        load_bundle(bundle.hash, other, tmp_path / "dest")
    assert exc.value.code == "E_BUNDLE_NOT_FOUND"

    # ...and the owning tenant still resolves it.
    dest = load_bundle(bundle.hash, acme, tmp_path / "mine")
    assert build_bundle(dest).hash == bundle.hash


def test_each_tenant_gets_its_own_copy_of_identical_content(tmp_path):
    """Cross-tenant dedupe is deliberately forfeited (D20): identical bytes are
    stored once PER TENANT, because a shared read namespace is the thing being
    removed."""
    root = tmp_path / "archives"
    bundle = build_bundle(_scaffold(tmp_path / "proj", "shared"))

    a = store_bundle(bundle, BundleStore(kind="local", root=root, workspace="ws_a"))
    b = store_bundle(bundle, BundleStore(kind="local", root=root, workspace="ws_b"))
    assert a != b
    assert Path(a).is_file() and Path(b).is_file()


def test_an_unnamespaced_store_is_byte_for_byte_the_old_layout(tmp_path):
    """`rya dev`, the test suite and single-tenant self-host must not move."""
    root = tmp_path / "archives"
    bundle = build_bundle(_scaffold(tmp_path / "proj", "shared"))
    path = store_bundle(bundle, BundleStore(kind="local", root=root))
    assert path == bundle_archive_path(bundle.hash, root)
    assert path.parent.name == bundle.hash[:2]


def test_the_single_tenant_sentinel_is_the_unnamespaced_address(tmp_path):
    """"default" (PostgresStore's default) and "" (FileStore has no workspace)
    are two spellings of "no tenant", and both must resolve to the pre-D20 flat
    address — with ONE lookup, not a namespaced miss followed by a fallback."""
    root = tmp_path / "archives"
    bundle = build_bundle(_scaffold(tmp_path / "proj", "shared"))

    legacy = store_bundle(bundle, BundleStore(kind="local", root=root))  # pre-D20
    upgraded = BundleStore(kind="local", root=root, workspace="default")

    assert upgraded.read_workspaces() == ("",)      # one lookup, no fallback cost
    assert store_bundle(bundle, upgraded) == legacy
    assert build_bundle(load_bundle(bundle.hash, upgraded, tmp_path / "d")).hash == bundle.hash
    assert bundle_archive_key("abc", "bundles", "default") == "bundles/abc.tar.gz"


def test_a_named_workspace_does_not_read_legacy_keys_by_default(tmp_path):
    """A deployment that was multi-tenant BEFORE D20 has flat archives, and its
    tenants would 404 after the upgrade. The fallback that fixes it is opt-in,
    because reading a flat key from a named tenant is a cross-tenant read."""
    root = tmp_path / "archives"
    bundle = build_bundle(_scaffold(tmp_path / "proj", "shared"))
    store_bundle(bundle, BundleStore(kind="local", root=root))  # pre-D20 archive

    closed = BundleStore(kind="local", root=root, workspace="ws_acme")
    assert closed.read_workspaces() == ("ws_acme",)
    with pytest.raises(RyaError) as exc:
        load_bundle(bundle.hash, closed, tmp_path / "dest")
    assert exc.value.code == "E_BUNDLE_NOT_FOUND"

    # Opted in, the same store resolves it — the documented upgrade path.
    opened = BundleStore(kind="local", root=root, workspace="ws_acme",
                         legacy_fallback=True)
    assert opened.read_workspaces() == ("ws_acme", "")
    assert build_bundle(load_bundle(bundle.hash, opened, tmp_path / "d2")).hash == bundle.hash


def test_the_legacy_fallback_never_diverts_a_write(tmp_path):
    """Even opted in, a write lands in the tenant's own namespace. A fallback
    that could also redirect writes would put one tenant's bytes in the shared
    space, which is worse than the read it was added to permit."""
    root = tmp_path / "archives"
    bundle = build_bundle(_scaffold(tmp_path / "proj", "shared"))
    store = BundleStore(kind="local", root=root, workspace="ws_acme",
                        legacy_fallback=True)
    assert "ws_acme" in str(store_bundle(bundle, store))


def test_s3_keys_carry_the_workspace_segment(tmp_path):
    """The prefix an IAM/bucket policy is scoped to. Without this segment there
    is nothing to grant per-tenant on."""
    assert bundle_archive_key("abc", "bundles", "ws_acme") == "bundles/ws_acme/abc.tar.gz"
    assert bundle_archive_key("abc", "bundles") == "bundles/abc.tar.gz"
    assert bundle_archive_key("abc", "", "ws_acme") == "ws_acme/abc.tar.gz"
    # Slashes an operator might type around the prefix must not double up.
    assert bundle_archive_key("abc", "/bundles/", "/ws_acme/") == "bundles/ws_acme/abc.tar.gz"


def test_two_projects_two_hashes(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    scaffold.write_project(a, "agent-a", template="demo")
    scaffold.write_project(b, "agent-b", template="demo")
    assert build_bundle(a).hash != build_bundle(b).hash
