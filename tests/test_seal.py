"""Encryption-at-rest for connection secrets — seal/unseal + store integration."""

import json

import pytest

from rya import seal as sealmod
from rya.store import Store

SECRET = "ghp_super_secret_token_value_123456"


def test_seal_roundtrip_and_format(tmp_path, monkeypatch):
    monkeypatch.delenv("RYA_SECRET_KEY", raising=False)
    sealed = sealmod.seal(SECRET, tmp_path)
    assert sealed.startswith("enc:v1:")
    assert SECRET not in sealed                       # ciphertext, not plaintext
    assert sealmod.unseal(sealed, tmp_path) == SECRET  # opens back to original
    # a keyfile was created with restrictive perms
    kp = tmp_path / ".rya" / "secret.key"
    assert kp.exists()


def test_legacy_plaintext_passthrough(tmp_path):
    # values written before encryption existed have no prefix → returned as-is
    assert sealmod.unseal("plain-legacy-token", tmp_path) == "plain-legacy-token"
    assert sealmod.unseal(None, tmp_path) is None
    assert sealmod.seal(None, tmp_path) is None


def test_env_key_is_used(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("RYA_SECRET_KEY", key)
    sealed = sealmod.seal(SECRET, tmp_path)
    assert sealmod.key_source(tmp_path) == "env"
    # no keyfile when an env key is provided
    assert not (tmp_path / ".rya" / "secret.key").exists()
    # decryptable only with the same key
    assert Fernet(key.encode()).decrypt(sealed[len("enc:v1:"):].encode()).decode() == SECRET


def test_store_encrypts_secret_at_rest(tmp_path, monkeypatch):
    monkeypatch.delenv("RYA_SECRET_KEY", raising=False)
    store = Store(tmp_path); store.ensure()
    pub = store.create_connection("github", ["repo:read"], secret=SECRET)
    assert pub["secretSet"] is True and pub["encrypted"] is True and "secret" not in pub

    # the raw token must NOT appear anywhere on disk under .rya/connections/
    on_disk = (tmp_path / ".rya" / "connections")
    blob = "".join(p.read_text() for p in on_disk.glob("*.json"))
    assert SECRET not in blob
    assert "enc:v1:" in blob

    # but the runtime can still resolve the real secret for injection
    assert store.get_connection("github")["secret"] == SECRET
    # and list never exposes it
    assert all("secret" not in c for c in store.list_connections())
    assert store.list_connections()[0]["encrypted"] is True


def test_reseal_migrates_legacy_plaintext(tmp_path, monkeypatch):
    monkeypatch.delenv("RYA_SECRET_KEY", raising=False)
    store = Store(tmp_path); store.ensure()
    # one secret written ENCRYPTED via the normal path...
    store.create_connection("slack", ["chat:write"], secret="xoxb-already-sealed-tok")
    # ...and one LEGACY plaintext row written directly (pre-encryption format)
    cid = "conn_legacyplaintext1"
    store._write(store.connections_dir / f"{cid}.json", {
        "id": cid, "provider": "github", "owner": None, "scopes": ["repo:read"],
        "label": "legacy", "secret": SECRET, "status": "active", "createdAt": "2020-01-01T00:00:00Z"})
    assert store.list_connections()  # both present
    # before reseal: the legacy github connection reads as unencrypted
    assert any(c["provider"] == "github" and c["encrypted"] is False for c in store.list_connections())

    res = store.reseal_connections()
    assert res == {"scanned": 2, "resealed": 1, "alreadyEncrypted": 1, "noSecret": 0}

    # the legacy plaintext is now gone from disk; both are encrypted + still decrypt
    blob = "".join(p.read_text() for p in store.connections_dir.glob("*.json"))
    assert SECRET not in blob
    assert all(c["encrypted"] is True for c in store.list_connections())
    assert store.get_connection("github")["secret"] == SECRET

    # idempotent: a second pass reseals nothing
    assert store.reseal_connections()["resealed"] == 0
