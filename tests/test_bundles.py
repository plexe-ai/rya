"""Content-hashed bundles — PLATFORM_DESIGN D12 / §9's second pipeline step.

The properties under test are the ones the rest of the deployment pipeline
assumes without checking: the hash is a function of content alone, the archive is
a function of the hash alone, and an archive is untrusted input.
"""

import gzip
import io
import os
import tarfile

import pytest

from rya.bundles import (
    BUNDLE_META_NAME,
    Bundle,
    build_bundle,
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


def test_two_projects_two_hashes(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    scaffold.write_project(a, "agent-a", template="demo")
    scaffold.write_project(b, "agent-b", template="demo")
    assert build_bundle(a).hash != build_bundle(b).hash
