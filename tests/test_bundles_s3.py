"""The object-store arm of the bundle archive store (PLATFORM_DESIGN §5.3).

Same two verbs as the local directory, a `bundles/<hash>.tar.gz` key, and — the
point of the whole feature — the same verification. A bundle fetched from a
bucket is exactly the artifact you must not trust on its face, so the tampering
test here is load-bearing rather than decorative.

boto3 is optional and there is no S3 in CI, so the client is faked the way
`tests/test_files_s3.py` fakes it: monkeypatch the one function that builds it.
"""

import sys

import pytest
import yaml

from rya import bundles, deployments
from rya.bundles import (
    BundleStore,
    build_bundle,
    bundle_archive_key,
    load_bundle,
    resolve_bundle_store,
    store_bundle,
)
from rya.cli import scaffold
from rya.errors import RyaError
from rya.store import Store
from rya.worker import start_worker

BUCKET = "rya-bundles-test"


def _client_error(code: str, message: str = "boom") -> Exception:
    """A stand-in for botocore's ClientError: the arm reads `.response` off it
    rather than importing botocore, so the shape is the whole contract."""
    exc = Exception(f"{code}: {message}")
    exc.response = {"Error": {"Code": code, "Message": message}}
    return exc


class FakeS3:
    """The four calls the bundle arm makes, and nothing else.

    Records reads so a test can assert the *shape* of the traffic — an existence
    check that GETs a whole archive body would pass a round-trip test and still
    be wrong.
    """

    def __init__(self, *, unreachable: bool = False):
        self.objects: dict[str, bytes] = {}
        self.unreachable = unreachable
        self.puts: list[str] = []
        self.gets: list[str] = []
        self.heads: list[str] = []

    def _check(self) -> None:
        if self.unreachable:
            # No `.response`: a connection failure is not a ClientError, which is
            # how the arm tells "store down" from "object absent".
            raise OSError("Could not connect to the endpoint URL: https://s3.fake/")

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self._check()
        assert Bucket == BUCKET
        self.puts.append(Key)
        self.objects[Key] = Body
        return {}

    def get_object(self, Bucket, Key):
        self._check()
        self.gets.append(Key)
        if Key not in self.objects:
            raise _client_error("NoSuchKey", f"no such key {Key}")
        import io

        return {"Body": io.BytesIO(self.objects[Key])}

    def head_object(self, Bucket, Key):
        self._check()
        self.heads.append(Key)
        if Key not in self.objects:
            raise _client_error("404", "Not Found")
        return {"ContentLength": len(self.objects[Key])}


@pytest.fixture(autouse=True)
def no_ambient_bucket(monkeypatch):
    """D8, test-suite half: a declared bucket only ever comes from the test.

    conftest's `hermetic_env` does not know about these names yet, and every
    assertion below about the *local* default would flip if a developer had
    RYA_BUNDLES_S3_BUCKET exported.
    """
    for name in (bundles.S3_BUCKET_ENV, bundles.S3_PREFIX_ENV, bundles.S3_REGION_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def s3(monkeypatch) -> FakeS3:
    fake = FakeS3()
    monkeypatch.setattr(bundles, "_s3_client", lambda store: fake)
    return fake


@pytest.fixture
def s3_store() -> BundleStore:
    return BundleStore(kind="s3", bucket=BUCKET)


# --------------------------------------------------------------------------- #
# resolution — declared, not ambient (D8)
# --------------------------------------------------------------------------- #
def test_local_directory_is_the_default(tmp_path):
    store = resolve_bundle_store(tmp_path, env={})
    assert store.is_local
    assert store.root == bundles.default_archive_root(tmp_path)


def test_a_declared_bucket_selects_the_object_store(tmp_path):
    store = resolve_bundle_store(
        tmp_path,
        env={bundles.S3_BUCKET_ENV: BUCKET,
             bundles.S3_PREFIX_ENV: "ws/42/bundles",
             bundles.S3_REGION_ENV: "eu-west-1"},
    )
    assert (store.kind, store.bucket, store.prefix, store.region) == (
        "s3", BUCKET, "ws/42/bundles", "eu-west-1")
    assert store.describe() == f"s3://{BUCKET}/ws/42/bundles"


def test_an_explicit_root_beats_a_declared_bucket(tmp_path):
    store = resolve_bundle_store(tmp_path, env={bundles.S3_BUCKET_ENV: BUCKET},
                                 root=tmp_path / "archives")
    assert store.is_local and store.root == tmp_path / "archives"


def test_resolution_without_a_root_or_a_bucket_is_an_error():
    with pytest.raises(RyaError) as exc:
        resolve_bundle_store(env={})
    assert exc.value.code == "E_BUNDLE_STORE"
    assert bundles.S3_BUCKET_ENV in exc.value.hint


def test_key_layout(tmp_path):
    assert bundle_archive_key("abc123") == "bundles/abc123.tar.gz"
    assert bundle_archive_key("abc123", "ws/42/") == "ws/42/abc123.tar.gz"


# --------------------------------------------------------------------------- #
# put / get
# --------------------------------------------------------------------------- #
def test_store_and_load_roundtrip(project, tmp_path, s3, s3_store):
    bundle = build_bundle(project)
    uri = store_bundle(bundle, s3_store)

    key = bundle_archive_key(bundle.hash)
    assert uri == f"s3://{BUCKET}/{key}"
    assert list(s3.objects) == [key]

    dest = load_bundle(bundle.hash, s3_store, tmp_path / "workdir")
    assert build_bundle(dest).hash == bundle.hash
    assert not list(dest.glob("*.tar.gz"))  # the archive is not left behind


def test_restore_is_idempotent_and_checks_with_a_head(project, s3, s3_store):
    bundle = build_bundle(project)
    first = store_bundle(bundle, s3_store)
    s3.gets.clear()

    assert store_bundle(bundle, s3_store) == first
    assert s3.puts == [bundle_archive_key(bundle.hash)]  # not re-uploaded
    assert s3.gets == []                                  # HEAD, not a body GET
    assert s3.heads.count(bundle_archive_key(bundle.hash)) == 2


def test_absent_artifact_is_not_found(tmp_path, s3, s3_store):
    with pytest.raises(RyaError) as exc:
        load_bundle("0" * 64, s3_store, tmp_path / "dest")
    assert exc.value.code == "E_BUNDLE_NOT_FOUND"
    assert "rya deploy" in exc.value.hint
    assert f"s3://{BUCKET}/bundles" in exc.value.message


def test_unreachable_store_is_not_a_missing_bundle(project, tmp_path, monkeypatch, s3_store):
    """The distinction E_BUNDLE_STORE exists for: re-deploying does not fix a
    bucket the worker cannot reach."""
    monkeypatch.setattr(bundles, "_s3_client", lambda store: FakeS3(unreachable=True))

    with pytest.raises(RyaError) as get_exc:
        load_bundle("0" * 64, s3_store, tmp_path / "dest")
    assert get_exc.value.code == "E_BUNDLE_STORE"
    assert bundles.S3_BUCKET_ENV in get_exc.value.hint

    with pytest.raises(RyaError) as put_exc:
        store_bundle(build_bundle(project), s3_store)
    assert put_exc.value.code == "E_BUNDLE_STORE"


def test_a_bucket_that_does_not_exist_is_a_store_error(tmp_path, s3, s3_store, monkeypatch):
    def no_bucket(Bucket, Key):
        raise _client_error("NoSuchBucket", "The specified bucket does not exist")

    monkeypatch.setattr(s3, "get_object", no_bucket)
    with pytest.raises(RyaError) as exc:
        load_bundle("0" * 64, s3_store, tmp_path / "dest")
    assert exc.value.code == "E_BUNDLE_STORE"
    assert "NoSuchBucket" in exc.value.message


def test_missing_boto3_is_a_clear_error_not_an_importerror(tmp_path, s3_store, monkeypatch):
    # None in sys.modules is what an absent optional dependency looks like to
    # `import boto3`; the arm must translate it, not leak the traceback.
    monkeypatch.setitem(sys.modules, "boto3", None)
    with pytest.raises(RyaError) as exc:
        load_bundle("0" * 64, s3_store, tmp_path / "dest")
    assert exc.value.code == "E_BUNDLE_STORE"
    # The hint names the EXTRA rather than a bare `pip install boto3`, so it
    # carries the version constraint the platform actually requires.
    assert "boto3" in exc.value.message and "rya[s3]" in exc.value.hint


# --------------------------------------------------------------------------- #
# verification — the security property (D12)
# --------------------------------------------------------------------------- #
def test_a_swapped_remote_archive_fails_before_any_import(tmp_path, s3, s3_store):
    """Anyone who can write the bucket can put other code under our key. The
    hash is re-derived from the materialised tree, so the swap fails closed."""
    good = tmp_path / "good"
    evil = tmp_path / "evil"
    scaffold.write_project(good, "good-agent", template="demo")
    scaffold.write_project(evil, "evil-agent", template="demo")
    (evil / "src" / "agent.py").write_text(
        "raise AssertionError('a swapped bundle must never be imported')\n")

    wanted = build_bundle(good)
    intruder = build_bundle(evil)
    store_bundle(wanted, s3_store)
    store_bundle(intruder, s3_store)
    # Same key, other bytes — the store is not the authority on what a hash means.
    s3.objects[bundle_archive_key(wanted.hash)] = s3.objects[bundle_archive_key(intruder.hash)]

    dest = tmp_path / "workdir"
    with pytest.raises(RyaError) as exc:
        load_bundle(wanted.hash, s3_store, dest)
    assert exc.value.code == "E_BUNDLE_MISMATCH"
    assert "D12" in exc.value.hint
    # The caller never gets a root back, so the swapped entrypoint is never imported.
    assert (dest / "src" / "agent.py").read_text().startswith("raise AssertionError")


def test_a_truncated_remote_object_fails_rather_than_unpacking(project, tmp_path, s3, s3_store):
    bundle = build_bundle(project)
    store_bundle(bundle, s3_store)
    key = bundle_archive_key(bundle.hash)
    s3.objects[key] = s3.objects[key][:-64]  # a partial upload / truncated object

    dest = tmp_path / "workdir"
    with pytest.raises(Exception):
        load_bundle(bundle.hash, s3_store, dest)
    assert not (dest / "src" / "agent.py").is_file()


# --------------------------------------------------------------------------- #
# the worker (§6): a pinned bundle can come from the object store
# --------------------------------------------------------------------------- #
def _project(tmp_path, name="wrk"):
    root = tmp_path / name
    scaffold.write_project(root, name, template="demo")
    p = root / "rya.agent.yaml"
    doc = yaml.safe_load(p.read_text())
    doc["tools"] = []
    p.write_text(yaml.safe_dump(doc))
    return root


def test_worker_loads_a_pinned_bundle_from_the_object_store(tmp_path, monkeypatch, s3):
    monkeypatch.setenv(bundles.S3_BUCKET_ENV, BUCKET)
    state = Store(tmp_path / "state")
    state.ensure()
    root = _project(tmp_path)
    bundle = build_bundle(root)
    store_bundle(bundle, resolve_bundle_store(root))
    version = deployments.create_version(state, agent="wrk", bundle=bundle, environment="prod")

    worker = start_worker(project_root=root, store=state, version_id=version["id"],
                          agent_name="wrk")

    assert worker.key.bundle_hash == bundle.hash
    assert worker.engine.project_root != root  # the unpacked archive, not the tree
    assert s3.gets == [bundle_archive_key(bundle.hash)]
    # The unpacked tree is local even when the artifact is remote.
    assert worker.engine.project_root.is_relative_to(bundles.default_archive_root(root))


def test_worker_reports_an_unreachable_store_at_startup(tmp_path, monkeypatch):
    monkeypatch.setenv(bundles.S3_BUCKET_ENV, BUCKET)
    monkeypatch.setattr(bundles, "_s3_client", lambda store: FakeS3(unreachable=True))
    state = Store(tmp_path / "state")
    state.ensure()
    root = _project(tmp_path)
    version = deployments.create_version(state, agent="wrk", bundle=build_bundle(root))

    with pytest.raises(RyaError) as exc:
        start_worker(project_root=root, store=state, version_id=version["id"], agent_name="wrk")
    assert exc.value.code == "E_BUNDLE_STORE"
