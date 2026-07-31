"""Bundle upload over HTTP — `POST /agents/{id}/versions` (PLATFORM_DESIGN §9).

The §9 pipeline without local database or bucket credentials: a client repo with
only the thin SDK packs an archive, posts it, and the platform verifies the bytes,
records an immutable version and flips an environment pointer.

Two properties carry the weight here, and both have a test that fails loudly if
they regress:

- **The content proves the version (D12).** The hash is rebuilt from the received
  bytes, so the sidecar cannot certify itself and a tampered archive is refused.
- **The control plane does not import bundles (D13).** Publishing must not load
  agent code, which is why no readiness attestation can exist on this path — and
  why `test_publish_does_not_import_agent_code` pins it.
"""

import gzip
import io
import json
import tarfile

import pytest
from fastapi.testclient import TestClient

from rya import bundles
from rya.api.app import build_app
from rya.cli import scaffold
from rya.store import open_store

AGENT = "publish-agent"
TOKEN = "test-operator-token"


@pytest.fixture
def platform(tmp_path, monkeypatch):
    """A control plane serving `publish-agent`, with auth on.

    Auth is ON by default because the endpoint refuses to publish to an
    unauthenticated control plane — an open one would accept anonymous code for a
    worker to import.
    """
    monkeypatch.delenv("RYA_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("RYA_DATABASE_URL", raising=False)
    monkeypatch.delenv("RYA_ALLOW_UNAUTHENTICATED_PUBLISH", raising=False)
    monkeypatch.setenv("RYA_TOKEN", TOKEN)
    root = tmp_path / "platform"
    scaffold.write_project(root, AGENT, template="minimal")
    return TestClient(build_app(root)), root


@pytest.fixture
def client_project(tmp_path):
    """The 'client repo': a separate tree that packs its own bundle."""
    root = tmp_path / "clientrepo"
    scaffold.write_project(root, AGENT, template="minimal")
    return root


def _pack(root, dest_dir) -> tuple[bytes, str]:
    """What `rya publish` does client-side: build, pack, hand over bytes + hash."""
    bundle = bundles.build_bundle(root)
    archive = bundles.pack(bundle, dest_dir / f"{bundle.hash}.tar.gz")
    return archive.read_bytes(), bundle.hash


def _publish(c, payload: bytes, hash: str, *, token: str | None = TOKEN, **params):
    query = {"hash": hash, **params}
    headers = {"content-type": "application/gzip"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return c.post(f"/agents/{AGENT}/versions", params=query, content=payload, headers=headers)


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #
def test_publish_records_and_promotes_a_version(platform, client_project, tmp_path):
    c, root = platform
    payload, digest = _pack(client_project, tmp_path)

    r = _publish(c, payload, digest, env="prod")
    assert r.status_code == 200, r.text
    body = r.json()

    # The hash the client computed is the hash the platform recorded: the archive
    # travelled without the address changing.
    assert body["ok"] is True
    assert body["bundleHash"] == digest
    assert body["agent"] == AGENT
    assert body["promoted"] is True and body["environment"] == "prod"
    assert body["versionId"].startswith("ver_")
    assert body["lockfile"] is None or isinstance(body["lockfile"], str)

    # And it is really in the ledger, not just in the response.
    store = open_store(root)
    version = store.version_get(body["versionId"])
    assert version is not None
    assert version["bundleHash"] == digest and version["agent"] == AGENT

    listed = c.get(f"/agents/{AGENT}/versions", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert body["versionId"] in [v["id"] for v in listed["versions"]]

    env = c.get(f"/agents/{AGENT}/environments/prod",
                headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert env["currentVersionId"] == body["versionId"]


def test_the_archive_lands_in_the_bundle_store(platform, client_project, tmp_path):
    """A version row without its artifact is a pointer to nothing — the worker
    resolves the same store and would fail with E_BUNDLE_NOT_FOUND."""
    c, root = platform
    payload, digest = _pack(client_project, tmp_path)
    body = _publish(c, payload, digest, env="prod").json()

    archive = bundles.bundle_archive_path(digest, bundles.default_archive_root(root))
    assert archive.is_file()
    assert body["archive"] == str(archive)
    # Round-trip: the stored artifact is loadable and still hashes to the same id.
    dest = tmp_path / "reloaded"
    bundles.load_bundle(digest, bundles.resolve_bundle_store(root), dest)
    assert bundles.build_bundle(dest).hash == digest


def test_publish_without_an_env_records_but_does_not_promote(platform, client_project, tmp_path):
    c, _ = platform
    payload, digest = _pack(client_project, tmp_path)
    body = _publish(c, payload, digest).json()
    assert body["promoted"] is False and body["environment"] is None
    assert body["versionId"]


def test_promote_false_records_only(platform, client_project, tmp_path):
    c, _ = platform
    payload, digest = _pack(client_project, tmp_path)
    body = _publish(c, payload, digest, env="prod", promote="false").json()
    assert body["promoted"] is False


def test_publish_is_idempotent_by_content(platform, client_project, tmp_path):
    """`version_create` is idempotent on (agent, bundleHash), so a retried publish
    after a network failure does not litter the version list."""
    c, _ = platform
    payload, digest = _pack(client_project, tmp_path)

    first = _publish(c, payload, digest, env="prod").json()
    second = _publish(c, payload, digest, env="prod").json()
    assert first["versionId"] == second["versionId"]

    listed = c.get(f"/agents/{AGENT}/versions",
                   headers={"Authorization": f"Bearer {TOKEN}"}).json()["versions"]
    assert len([v for v in listed if v["bundleHash"] == digest]) == 1


def test_provenance_metadata_is_recorded(platform, client_project, tmp_path):
    c, root = platform
    payload, digest = _pack(client_project, tmp_path)
    body = _publish(c, payload, digest, env="prod",
                    actor="ada@example.com", **{"meta.gitSha": "abc1234", "meta.ci": "run/42"}).json()

    version = open_store(root).version_get(body["versionId"])
    assert version["metadata"]["gitSha"] == "abc1234"
    assert version["metadata"]["ci"] == "run/42"
    assert version["metadata"]["publishedVia"] == "http"
    assert version["createdBy"] == "ada@example.com"


# --------------------------------------------------------------------------- #
# the content is the address (D12)
# --------------------------------------------------------------------------- #
def test_a_wrong_claimed_hash_is_refused(platform, client_project, tmp_path):
    c, _ = platform
    payload, _ = _pack(client_project, tmp_path)
    r = _publish(c, payload, "0" * 64, env="prod")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "E_BUNDLE_MISMATCH"


def test_a_tampered_archive_is_refused(platform, client_project, tmp_path):
    """The sidecar is not allowed to certify the payload: rebuild wins. An
    attacker who edits a file AND rewrites .rya-bundle.json still fails."""
    c, _ = platform
    _, digest = _pack(client_project, tmp_path)

    # Unpack, edit the entrypoint, keep the sidecar's claimed hash, repack.
    work = tmp_path / "tamper"
    bundles.unpack(tmp_path / f"{digest}.tar.gz", work)
    (work / "src" / "agent.py").write_text("# smuggled in\n")
    buf = io.BytesIO()
    with (
        gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tf,
    ):
        for path in sorted(p for p in work.rglob("*") if p.is_file()):
            data = path.read_bytes()
            info = tarfile.TarInfo(str(path.relative_to(work)))
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

    r = _publish(c, buf.getvalue(), digest, env="prod")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "E_BUNDLE_MISMATCH"


def test_mismatch_names_sdk_skew_when_that_is_the_cause(platform, client_project, tmp_path):
    """The hash folds in the SDK version, so identical bytes hash differently
    across SDKs. Blaming tampering would send an operator hunting a ghost."""
    c, _ = platform
    bundle = bundles.build_bundle(client_project)
    work = tmp_path / "skew"
    archive = bundles.pack(bundle, tmp_path / "skew.tar.gz")
    bundles.unpack(archive, work)

    sidecar = json.loads((work / bundles.BUNDLE_META_NAME).read_text())
    sidecar["sdkVersion"] = "9.9.9"
    (work / bundles.BUNDLE_META_NAME).write_text(json.dumps(sidecar))

    buf = io.BytesIO()
    with (
        gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tf,
    ):
        for path in sorted(p for p in work.rglob("*") if p.is_file()):
            data = path.read_bytes()
            info = tarfile.TarInfo(str(path.relative_to(work)))
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

    r = _publish(c, buf.getvalue(), "1" * 64, env="prod")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "E_BUNDLE_MISMATCH"
    assert "9.9.9" in detail["message"]
    assert "SDK version" in (detail["hint"] or "")


def test_a_tar_escaping_member_is_rejected(platform, tmp_path):
    """An upload is untrusted input; `unpack` validates every member before a
    byte is written, and the endpoint must surface that rather than 500."""
    c, _ = platform
    info = tarfile.TarInfo("../escaped.txt")
    payload = b"pwned"
    info.size = len(payload)
    buf = io.BytesIO()
    with (
        gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tf,
    ):
        tf.addfile(info, io.BytesIO(payload))

    r = _publish(c, buf.getvalue(), "2" * 64, env="prod")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "E_VALIDATION"
    assert not (tmp_path / "escaped.txt").exists()


# --------------------------------------------------------------------------- #
# request validation
# --------------------------------------------------------------------------- #
def test_a_missing_hash_is_a_validation_error(platform, client_project, tmp_path):
    c, _ = platform
    payload, _ = _pack(client_project, tmp_path)
    r = c.post(f"/agents/{AGENT}/versions", content=payload,
               headers={"content-type": "application/gzip", "Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "E_VALIDATION"


def test_an_empty_body_is_a_validation_error(platform):
    c, _ = platform
    r = _publish(c, b"", "3" * 64)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "E_VALIDATION"


def test_an_oversized_body_is_refused(platform, client_project, tmp_path, monkeypatch):
    c, _ = platform
    monkeypatch.setenv("RYA_MAX_BUNDLE_BYTES", "10")
    payload, digest = _pack(client_project, tmp_path)
    r = _publish(c, payload, digest)
    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "E_VALIDATION"


def test_a_bundle_for_another_agent_is_refused(platform, tmp_path):
    """This deployment serves one manifest, so a version filed under a different
    name would be listed by nothing and executed by nobody."""
    c, _ = platform
    other = tmp_path / "other"
    scaffold.write_project(other, "some-other-agent", template="minimal")
    payload, digest = _pack(other, tmp_path)

    r = _publish(c, payload, digest, env="prod")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "E_VALIDATION"
    assert "some-other-agent" in detail["message"] and AGENT in detail["message"]


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #
def test_publish_requires_the_operator_token(platform, client_project, tmp_path):
    c, _ = platform
    payload, digest = _pack(client_project, tmp_path)
    r = _publish(c, payload, digest, env="prod", token=None)
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "E_UNAUTHORIZED"


def test_a_wrong_token_is_refused(platform, client_project, tmp_path):
    c, _ = platform
    payload, digest = _pack(client_project, tmp_path)
    r = _publish(c, payload, digest, env="prod", token="not-the-token")
    assert r.status_code == 401


def test_an_open_control_plane_refuses_to_publish(tmp_path, monkeypatch, client_project):
    """Every other route is open in dev mode; this one is not, because an open
    publish endpoint is anonymous code upload to a box whose worker imports it."""
    monkeypatch.delenv("RYA_TOKEN", raising=False)
    monkeypatch.delenv("RYA_DATABASE_URL", raising=False)
    monkeypatch.delenv("RYA_ALLOW_UNAUTHENTICATED_PUBLISH", raising=False)
    root = tmp_path / "open"
    scaffold.write_project(root, AGENT, template="minimal")
    c = TestClient(build_app(root))

    payload, digest = _pack(client_project, tmp_path)
    r = _publish(c, payload, digest, env="prod", token=None)
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["code"] == "E_UNAUTHORIZED"
    assert "RYA_TOKEN" in detail["hint"]


def test_the_local_loop_escape_hatch_works(tmp_path, monkeypatch, client_project):
    monkeypatch.delenv("RYA_TOKEN", raising=False)
    monkeypatch.delenv("RYA_DATABASE_URL", raising=False)
    monkeypatch.setenv("RYA_ALLOW_UNAUTHENTICATED_PUBLISH", "1")
    root = tmp_path / "loop"
    scaffold.write_project(root, AGENT, template="minimal")
    c = TestClient(build_app(root))

    payload, digest = _pack(client_project, tmp_path)
    r = _publish(c, payload, digest, env="prod", token=None)
    assert r.status_code == 200 and r.json()["bundleHash"] == digest


# --------------------------------------------------------------------------- #
# what this path deliberately cannot do
# --------------------------------------------------------------------------- #
def test_the_response_admits_readiness_was_not_attested(platform, client_project, tmp_path):
    c, _ = platform
    payload, digest = _pack(client_project, tmp_path)
    body = _publish(c, payload, digest, env="prod").json()
    assert body["attested"] is False
    assert body["notAttested"] == ["readiness"]
    assert "does not import bundles" in body["note"]


def test_a_readiness_gate_blocks_promotion_on_this_path(platform, client_project, tmp_path):
    """The honest consequence of not attesting: an environment that demands
    readiness evidence will refuse a version published over HTTP."""
    from rya import gates

    c, root = platform
    store = open_store(root)
    gates.set_gate(store, {"environments": {"prod": {"requireReadiness": True}}}, actor="ops")

    payload, digest = _pack(client_project, tmp_path)
    r = _publish(c, payload, digest, env="prod")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "E_PROMOTION_BLOCKED"

    # ...but recording the version is still allowed; only the pointer flip is not.
    ok = _publish(c, payload, digest, env="prod", promote="false")
    assert ok.status_code == 200 and ok.json()["promoted"] is False


def test_publish_does_not_import_agent_code(platform, client_project, tmp_path, monkeypatch):
    """D13, pinned. The control plane must not execute tenant code, so neither
    `load_agent` nor `check_readiness` may be reached on this path — booby-trap
    both and publish anyway."""
    import rya.readiness
    import rya.runtime.engine

    def explode(*a, **k):
        raise AssertionError("the control plane imported the bundle")

    monkeypatch.setattr(rya.runtime.engine, "load_agent", explode)
    monkeypatch.setattr(rya.readiness, "check_readiness", explode)

    c, _ = platform
    payload, digest = _pack(client_project, tmp_path)
    r = _publish(c, payload, digest, env="prod")
    assert r.status_code == 200 and r.json()["bundleHash"] == digest
