"""S3 files backend - bytes offload + presign flow (faked S3 client)."""
import pytest

import rya.files_s3 as fs3
from rya.cli import scaffold
from rya.manifest import load_manifest
from rya.runtime import Engine, load_agent
from rya.store import Store


class FakeS3:
    def __init__(self):
        self.objects = {}
    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = (Body, ContentType)
    def get_object(self, Bucket, Key):
        import io
        body, _ = self.objects[Key]
        return {"Body": io.BytesIO(body)}
    def head_object(self, Bucket, Key):
        body, ct = self.objects[Key]
        return {"ContentLength": len(body), "ContentType": ct}
    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://s3.fake/{Params['Key']}?sig=x"


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setenv("RYA_FILES_S3_BUCKET", "bkt")
    monkeypatch.setattr(fs3, "_client", lambda: fake)
    return fake


def test_bytes_offload_roundtrip_filestore(tmp_path, s3):
    scaffold.write_project(tmp_path, "s3a", template="demo")
    store = Store(tmp_path); store.ensure()
    meta = store.save_file("big.pdf", b"BYTES" * 100, content_type="application/pdf",
                           tags={"cif": "1"})
    assert meta["storage"] == "s3"
    assert not (tmp_path / ".rya" / "files" / meta["id"]).exists()  # no local bytes
    assert store.read_file(meta["id"]) == b"BYTES" * 100            # served from S3


def test_presign_confirm_fires_event(tmp_path, s3):
    from fastapi.testclient import TestClient
    from rya.api.app import build_app
    scaffold.write_project(tmp_path, "s3b", template="demo")
    client = TestClient(build_app(tmp_path))
    r = client.post("/files/presign", json={"name": "huge.pdf",
                                            "contentType": "application/pdf",
                                            "tags": {"cif": "9", "docType": "aecb"}})
    assert r.status_code == 200, r.text
    fid, url = r.json()["fileId"], r.json()["uploadUrl"]
    assert url.startswith("https://s3.fake/files/")
    # confirm before upload -> 409
    assert client.post(f"/files/{fid}/confirm").status_code == 409
    s3.objects[fs3.key_for(fid)] = (b"%PDF-huge", "application/pdf")
    ok = client.post(f"/files/{fid}/confirm").json()
    assert ok["runId"] and ok["size"] == 9
    run = client.get(f"/runs/{ok['runId']}").json()
    assert run["event"]["payload"]["tags"] == {"cif": "9", "docType": "aecb", "_storage": "s3"}
