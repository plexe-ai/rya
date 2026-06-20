"""Per-user identity via verified JWTs.

The platform's identity story: a caller presents a signed JWT, the runtime
verifies it, and binds an ``Identity{sub, claims}`` into the run — so memory,
permissions, and audit can be scoped to the *user*, not just the workspace.

Verification:
- ``RYA_JWT_SECRET``  → HS256, verified with stdlib (no dependency).
- ``RYA_JWKS_URL``    → RS256 via JWKS (Cognito/Auth0). Needs the [auth] extra
  (PyJWT); raises a clear error if it's not installed.

This is the dependency-free core of the vision's "JWT at the edge → identity-bound
session". The full single-purpose-mutator / RLS-as-the-user model is Phase C.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Optional

from .errors import RyaError


@dataclass
class Identity:
    sub: str
    claims: dict

    @property
    def email(self) -> Optional[str]:
        return self.claims.get("email")

    @property
    def scopes(self) -> Optional[list]:
        """Scopes this user has granted, from the verified token. Used for the
        agent-authority intersection rule (effective = agent ∩ user). Returns
        None when the token carries no scope claim — treated as 'unrestricted'
        so existing single-tenant/no-scope setups are unaffected."""
        raw = self.claims.get("scopes", self.claims.get("scope"))
        if raw is None:
            return None
        return raw.split() if isinstance(raw, str) else list(raw)

    def to_dict(self) -> dict:
        return {"sub": self.sub, "email": self.email, "scopes": self.scopes}


def _b64url_decode(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def _verify_hs256(token: str, secret: str) -> dict:
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
    except ValueError:
        raise RyaError("E_UNAUTHORIZED", "Malformed JWT.", hint="Expected a three-part JWT.")
    signing_input = f"{header_b64}.{payload_b64}".encode()
    expected = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _b64url_decode(sig_b64)):
        raise RyaError("E_UNAUTHORIZED", "JWT signature verification failed.")
    claims = json.loads(_b64url_decode(payload_b64))
    exp = claims.get("exp")
    if exp is not None and time.time() > exp:
        raise RyaError("E_UNAUTHORIZED", "JWT is expired.")
    return claims


def _verify_rs256_jwks(token: str, jwks_url: str) -> dict:
    try:
        import jwt
        from jwt import PyJWKClient
    except ImportError:
        raise RyaError("E_UNAUTHORIZED", "RS256/JWKS verification needs the [auth] extra.",
                       hint="pip install 'rya[auth]' (PyJWT), or use RYA_JWT_SECRET for HS256.")
    signing_key = PyJWKClient(jwks_url).get_signing_key_from_jwt(token)
    return jwt.decode(token, signing_key.key, algorithms=["RS256"],
                      options={"verify_aud": False})


def verify_jwt(token: str, env: Optional[dict] = None) -> Identity:
    env = env or os.environ
    secret = env.get("RYA_JWT_SECRET")
    if secret:
        claims = _verify_hs256(token, secret)
    elif env.get("RYA_JWKS_URL"):
        claims = _verify_rs256_jwks(token, env["RYA_JWKS_URL"])
    else:
        raise RyaError("E_UNAUTHORIZED", "No JWT verifier configured.",
                       hint="Set RYA_JWT_SECRET (HS256) or RYA_JWKS_URL (RS256).")
    sub = claims.get("sub")
    if not sub:
        raise RyaError("E_UNAUTHORIZED", "JWT has no 'sub' claim.")
    return Identity(sub=sub, claims=claims)


def jwt_configured(env: Optional[dict] = None) -> bool:
    env = env or os.environ
    return bool(env.get("RYA_JWT_SECRET") or env.get("RYA_JWKS_URL"))
