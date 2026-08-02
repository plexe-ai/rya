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

**Per-tenant keys (D18/#13) live in :mod:`rya.keys`, and this module delegates.**
The two-key order above is one key for the whole deployment, which does not
survive D17 — one compromise is total, and erasing a tenant becomes a row hunt
instead of destroying a key. ``rya.keys`` adds the provider seam and the
``enc:v2:<key_id>:<ct>`` envelope that a rotation needs.

What did *not* change: with no ``RYA_KEY_PROVIDER`` set, this module behaves
exactly as it did — the deployment key, the ``enc:v1:`` envelope, the same key
resolution order. That default is load-bearing rather than conservative. Sealing
happens on the write path of every connection, so a build that quietly re-addressed
ciphertext would leave an upgraded deployment unable to open secrets it wrote
yesterday. A deployment opts into per-tenant keys, and ``rya keys rotate`` /
``rya keys reseal`` move it across.

``unseal`` opens **both** envelopes regardless of the declared provider, because
which envelope a value carries is a fact about that value, not about today's
configuration.
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
    """True for either envelope: this module's ``enc:v1:`` and ``keys``' ``enc:v2:``.

    Callers use this to decide "is there still a plaintext secret at rest here",
    and a v2 value is sealed by any reading of that question. `store.py` and
    `store_postgres.py` both report it as the ``encrypted`` flag on a connection.
    """
    from .keys import is_sealed as _either

    return _either(value)


def _keyring(root: Optional[Path]):
    """The declared key ring, or None to stay on this module's v1 path.

    Returns None in two cases that must be distinguished from an error: no
    per-tenant provider is declared (the overwhelmingly common one), and the ring
    could not be built at all. The second is a degradation rather than a raise for
    the same reason ``available()`` exists — a deployment that cannot resolve a key
    should write plaintext and be *flagged*, not crash on a connection write.
    """
    from . import keys as _keys

    name = (os.environ.get(_keys.PROVIDER_ENV) or "").strip().lower()
    if not name or name == _keys.PROVIDER_DEPLOYMENT:
        return None
    try:
        return _keys.resolve_keyring(root=root)
    except Exception:  # noqa: BLE001 - flagged by readiness, never fatal here
        return None


def seal(plaintext: Optional[str], root: Optional[Path] = None,
         *, workspace: str = "", keyring=None) -> Optional[str]:
    """Encrypt a secret for storage. No-op for None. Falls back to plaintext only
    if cryptography is unavailable, or if there is no key material to use.

    ``workspace`` selects the tenant key when a per-tenant provider is declared
    and is ignored otherwise. It is a keyword because every existing caller passes
    ``(value, root)`` positionally, and because a workspace silently defaulting to
    the wrong tenant is the one mistake this argument must not make easy: empty
    means "no tenant dimension", which is what `FileStore` genuinely is.
    """
    if plaintext is None or plaintext == "":
        return plaintext
    if is_sealed(plaintext):
        return plaintext  # already sealed
    if not available():
        return plaintext  # honest degradation; flagged elsewhere
    ring = keyring if keyring is not None else _keyring(root)
    if ring is not None:
        return ring.seal(plaintext, workspace)
    try:
        f, _ = _fernet(root)
    except RuntimeError:
        return plaintext  # no key (server context without RYA_SECRET_KEY)
    return PREFIX + f.encrypt(plaintext.encode()).decode()


def unseal(value: Optional[str], root: Optional[Path] = None,
           *, workspace: str = "", keyring=None) -> Optional[str]:
    """Decrypt a stored secret. Legacy plaintext (no prefix) passes through.

    Which envelope a value carries decides how it is opened — not what the
    deployment is configured for today. A v2 value read by a process with no
    provider declared still resolves its ring, because the alternative is handing
    a caller ciphertext and calling it a secret.
    """
    if value is None or not is_sealed(value):
        return value  # legacy plaintext or None
    if not available():
        return value  # cannot open; return as-is rather than crash
    from .keys import PREFIX_V2

    if value.startswith(PREFIX_V2):
        ring = keyring if keyring is not None else _keyring(root)
        if ring is None:
            from .keys import resolve_keyring

            ring = resolve_keyring(root=root)
        return ring.open(value, workspace)
    f, _ = _fernet(root)
    return f.decrypt(value[len(PREFIX):].encode()).decode()
