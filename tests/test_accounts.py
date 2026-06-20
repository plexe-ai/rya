"""Account crypto — password hashing + signed session tokens."""

from rya import accounts


def test_password_hash_roundtrip():
    h = accounts.hash_password("hunter2-correct-horse")
    assert h.startswith("pbkdf2_sha256$") and "hunter2" not in h   # never the plaintext
    assert accounts.verify_password("hunter2-correct-horse", h) is True
    assert accounts.verify_password("wrong", h) is False
    # two hashes of the same password differ (random salt)
    assert accounts.hash_password("x") != accounts.hash_password("x")


def test_session_token_roundtrip_and_tamper(monkeypatch):
    monkeypatch.setenv("RYA_SESSION_SECRET", "test-secret")
    tok = accounts.issue_session("usr_1", "ada@acme.io")
    p = accounts.verify_session(tok)
    assert p and p["sub"] == "usr_1" and p["email"] == "ada@acme.io"
    # a tampered signature is rejected
    body, sig = tok.rsplit(".", 1)
    assert accounts.verify_session(body + "." + ("0" * len(sig))) is None
    # signed with a different secret → rejected
    monkeypatch.setenv("RYA_SESSION_SECRET", "other-secret")
    assert accounts.verify_session(tok) is None


def test_session_expiry():
    tok = accounts.issue_session("usr_1", "a@b.co", ttl_seconds=100, now=1_000_000)
    assert accounts.verify_session(tok, now=1_000_050) is not None   # within ttl
    assert accounts.verify_session(tok, now=1_000_200) is None       # expired
    assert accounts.verify_session(None) is None
