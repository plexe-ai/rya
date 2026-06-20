"""Encryption-at-rest for vaulted secrets (connection credentials).

A connection's secret is *sealed* before it is ever written to the store and
*opened* only inside the runtime at the moment a tool credential is injected.
So the secret is protected in three places now: at rest (encrypted here),
in logs/traces (redacted by the runtime vault), and from the model/handler
(never returned by any public read).

Key resolution, in order:
  1. ``RYA_SECRET_KEY`` — a Fernet key. Supply this from your secrets manager /
     KMS in production; the ciphertext at rest is then useless without it.
  2. A per-project keyfile at ``<root>/.rya/secret.key`` (auto-generated, 0600).
     This is the zero-config local-dev default — at-rest encryption with the key
     co-located on disk (defense-in-depth, not a substitute for #1).

Legacy plaintext values written before this existed are read transparently and
re-sealed on the next write, so upgrades are seamless. If ``cryptography`` is not
installed, sealing degrades to plaintext and ``available()`` returns False so the
readiness gate / provision can flag it honestly.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple

PREFIX = "enc:v1:"


def available() -> bool:
    try:
        import cryptography.fernet  # noqa: F401
        return True
    except Exception:
        return False


def _fernet(root: Optional[Path]) -> Tuple[object, str]:
    """Return (Fernet, key_source). Raises if cryptography is missing."""
    from cryptography.fernet import Fernet

    key = os.environ.get("RYA_SECRET_KEY")
    if key:
        return Fernet(key.encode() if isinstance(key, str) else key), "env"
    if root is None:
        # No project root (e.g. Postgres server context) and no env key — we have
        # nowhere safe to persist a key, so caller must set RYA_SECRET_KEY.
        raise RuntimeError("RYA_SECRET_KEY is required to seal secrets without a project keyfile.")
    kp = Path(root) / ".rya" / "secret.key"
    if kp.exists():
        return Fernet(kp.read_bytes()), "keyfile"
    kp.parent.mkdir(parents=True, exist_ok=True)
    k = Fernet.generate_key()
    kp.write_bytes(k)
    try:
        os.chmod(kp, 0o600)
    except OSError:  # pragma: no cover - non-POSIX
        pass
    return Fernet(k), "keyfile"


def key_source(root: Optional[Path] = None) -> str:
    """Where the encryption key comes from: 'env' | 'keyfile' | 'none'."""
    if not available():
        return "none"
    if os.environ.get("RYA_SECRET_KEY"):
        return "env"
    if root is not None:
        return "keyfile"
    return "none"


def is_sealed(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def seal(plaintext: Optional[str], root: Optional[Path] = None) -> Optional[str]:
    """Encrypt a secret for storage. No-op for None. Falls back to plaintext only
    if cryptography is unavailable, or if there is no key material to use."""
    if plaintext is None or plaintext == "":
        return plaintext
    if is_sealed(plaintext):
        return plaintext  # already sealed
    if not available():
        return plaintext  # honest degradation; flagged elsewhere
    try:
        f, _ = _fernet(root)
    except RuntimeError:
        return plaintext  # no key (server context without RYA_SECRET_KEY)
    return PREFIX + f.encrypt(plaintext.encode()).decode()


def unseal(value: Optional[str], root: Optional[Path] = None) -> Optional[str]:
    """Decrypt a stored secret. Legacy plaintext (no prefix) passes through."""
    if value is None or not is_sealed(value):
        return value  # legacy plaintext or None
    if not available():
        return value  # cannot open; return as-is rather than crash
    f, _ = _fernet(root)
    return f.decrypt(value[len(PREFIX):].encode()).decode()
