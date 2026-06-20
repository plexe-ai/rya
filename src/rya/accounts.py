"""Self-serve accounts — password hashing + signed session tokens (stdlib only).

This is the onboarding identity layer that sits ABOVE the runtime: a new user
signs up with email + password, gets a short-lived session token, and uses it to
create a workspace and mint a data-plane API key — no operator/admin token
needed. It's substrate-agnostic and adds no dependency (PBKDF2 + HMAC from the
stdlib). The session token authenticates the *user* for onboarding routes only;
the workspace ``rya_sk_…`` key still authenticates data-plane requests.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional

_ITER = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITER)
    return f"pbkdf2_sha256${_ITER}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, dk_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def _session_secret() -> str:
    # Stable across replicas: a dedicated secret, else the at-rest key, else a dev default.
    return (os.environ.get("RYA_SESSION_SECRET")
            or os.environ.get("RYA_SECRET_KEY")
            or "rya-dev-session-secret")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_session(user_id: str, email: str, ttl_seconds: int = 7 * 86400,
                  now: Optional[int] = None) -> str:
    """A compact HMAC-signed session token (``<payload>.<sig>``)."""
    now = int(now if now is not None else time.time())
    payload = {"sub": user_id, "email": email, "iat": now, "exp": now + ttl_seconds}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(_session_secret().encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_session(token: Optional[str], now: Optional[int] = None) -> Optional[dict]:
    """Return the session payload if the token is valid + unexpired, else None."""
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expected = hmac.new(_session_secret().encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_unb64(body))
    except Exception:
        return None
    if payload.get("exp", 0) < int(now if now is not None else time.time()):
        return None
    return payload
