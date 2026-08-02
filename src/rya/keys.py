"""Per-tenant encryption keys — key management for D18/#13, and the mechanism D31 shreds with.

``seal.py`` resolves **one key per deployment**, from ``RYA_SECRET_KEY`` or a
project keyfile. That is the right shape for a laptop and for a single-tenant
self-host, and it does not survive D17: one key means one compromise is total, and
"delete a tenant's data" becomes an unbounded row hunt rather than an O(1) act.

This module adds the key *provider* seam underneath sealing, the same shape
``open_store`` and ``resolve_bundle_store`` already have. Four providers, and the
differences between them are the whole point:

======================  ==========================  ==========  ================
provider                key per                     shreddable  needs
======================  ==========================  ==========  ================
``deployment``          the deployment (status quo)  **no**      nothing
``derived``             workspace, HKDF from a root  **no**      a root key
``wrapped``             workspace, random DEK        **yes**     a key table
``wrapped`` + KMS       workspace, random DEK        **yes**     KMS
======================  ==========================  ==========  ================

**`derived` cannot be crypto-shredded, and that is the trap.** HKDF-per-workspace
is the cheap way to get per-tenant keys: no table, no wrapping, no rotation
bookkeeping, and it genuinely delivers the *compromise-isolation* half — cracking
`acme`'s key tells you nothing about `globex`'s. But the key is a pure function of
the root secret and the workspace id, so it can always be recomputed, which means
there is nothing to destroy. A deployment that adopted it believing D31 was
satisfied would have an erasure story that is provably false. So
:meth:`KeyProvider.destroy` on the derived provider raises rather than returning
zero: an erasure path must not appear to succeed.

**Only `wrapped` satisfies D31.** The workspace's data-encryption key is random,
stored wrapped (by a root key locally, by KMS in production), and destroying that
one row makes every value sealed under it unreadable without enumerating any of
them. That is what makes a purge O(1) in the number of secrets.

**Rotation needs a key id in the ciphertext, so the envelope changed.** The old
``enc:v1:<ct>`` is self-describing only if there is exactly one key. The new
``enc:v2:<key_id>:<ct>`` names the key that sealed it, so two generations can
coexist while a re-seal walks the rows. v1 values keep opening under the
deployment key forever — ``seal.py``'s "legacy plaintext is read transparently and
re-sealed on the next write" promise, applied one envelope later.

**What the boundary is, and is not.** The key table is readable by the api and by
the claimer, because both are platform code that already holds every secret the
deployment has. It is *not* readable by tenant code — not because of a grant, but
because tenant code has no database connection at all (D18). §5 rejected
role-per-tenant as a substitute for the broker for exactly this reason: the
process is the boundary, and the role is defence in depth (D19).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from .errors import RyaError

# ---- envelopes --------------------------------------------------------------
# v1 is seal.py's original: one deployment key, nothing to name. v2 carries the
# key id because rotation and per-tenant keys both need to answer "which key
# sealed this" from the ciphertext alone.
PREFIX_V1 = "enc:v1:"
PREFIX_V2 = "enc:v2:"

# A key id must survive being a colon-delimited envelope field, so it uses dots:
# "<provider>.<scope>.<generation>". `scope` is the workspace, or "_" where the
# provider has no tenant dimension.
KEY_ID_SEP = "."
NO_SCOPE = "_"

# The single-tenant sentinel, and it MUST be the same rule `bundles._normalize_workspace`
# uses. `PostgresStore` defaults `workspace_id` to "default" and `FileStore` has no
# workspace at all, so "default" and "" are two spellings of "no tenant" — real
# workspaces are `ws_*`, minted by `tenancy`. Getting this wrong was a live bug: a
# `FileStore` sealed under "" while `rya workspaces purge default` shredded "default",
# so the purge destroyed nothing and reported it accurately, which is the worst
# combination available. Two modules deciding what "no tenant" means, differently, is
# the shape of that bug.
UNTENANTED = "default"

PROVIDER_ENV = "RYA_KEY_PROVIDER"
ROOT_KEY_ENV = "RYA_SECRET_KEY"          # the same variable seal.py reads
KMS_KEY_ENV = "RYA_KMS_KEY_ID"
KEY_TABLE = "rya_tenant_keys"

PROVIDER_DEPLOYMENT = "deployment"
PROVIDER_DERIVED = "derived"
PROVIDER_WRAPPED = "wrapped"
PROVIDERS = (PROVIDER_DEPLOYMENT, PROVIDER_DERIVED, PROVIDER_WRAPPED)

E_KEY_UNAVAILABLE = "E_KEY_UNAVAILABLE"
E_KEY_NOT_FOUND = "E_KEY_NOT_FOUND"
E_KEY_NOT_SHREDDABLE = "E_KEY_NOT_SHREDDABLE"
E_KEY_PROVIDER_UNKNOWN = "E_KEY_PROVIDER_UNKNOWN"


def available() -> bool:
    """Whether real encryption is possible. Same question ``seal.available`` asks."""
    try:
        import cryptography.fernet  # noqa: F401

        return True
    except Exception:
        return False


def _fernet(material: bytes):
    from cryptography.fernet import Fernet

    return Fernet(material)


def new_key() -> bytes:
    """A fresh Fernet key (urlsafe-base64 of 32 random bytes)."""
    from cryptography.fernet import Fernet

    return Fernet.generate_key()


def _hkdf(root: bytes, *, info: bytes, length: int = 32) -> bytes:
    """HKDF-SHA256, stdlib only.

    Written out rather than pulled from ``cryptography.hazmat`` so the derivation
    is inspectable next to the warning about what it cannot do. Extract with an
    empty salt because the root key is already high-entropy key material, not a
    password.
    """
    prk = hmac.new(b"\x00" * 32, root, hashlib.sha256).digest()
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def _scope(workspace: Optional[str]) -> str:
    """The key-id scope for a workspace. ``""`` and ``"default"`` both mean no tenant."""
    ws = (workspace or "").strip()
    if not ws or ws == UNTENANTED:
        return NO_SCOPE
    return ws


def make_key_id(provider: str, workspace: Optional[str], generation: int) -> str:
    return KEY_ID_SEP.join((provider, _scope(workspace), str(int(generation))))


def parse_key_id(key_id: str) -> Tuple[str, str, int]:
    """``("wrapped", "ws_abc", 2)``. Raises on anything this build cannot resolve."""
    parts = (key_id or "").split(KEY_ID_SEP)
    if len(parts) != 3 or not parts[0]:
        raise RyaError(
            E_KEY_NOT_FOUND,
            f"Malformed key id {key_id!r}.",
            hint="A key id is '<provider>.<workspace>.<generation>'. A value sealed "
                 "by a newer build may name a provider this one does not have.",
        )
    try:
        gen = int(parts[2])
    except ValueError:
        raise RyaError(E_KEY_NOT_FOUND, f"Malformed key id {key_id!r}: generation is not a number.") from None
    return parts[0], parts[1], gen


@dataclass(frozen=True)
class TenantKey:
    """One usable key, and enough provenance to explain where it came from."""

    key_id: str
    material: bytes
    workspace: str = ""
    generation: int = 1
    provider: str = PROVIDER_DEPLOYMENT

    def describe(self) -> dict:
        return {"keyId": self.key_id, "workspace": self.workspace,
                "generation": self.generation, "provider": self.provider}


# ---- where wrapped keys live ------------------------------------------------

class KeyStore:
    """Persistence for wrapped per-workspace DEKs.

    Deliberately tiny and separate from :class:`Store`. The key table is the one
    piece of state whose *deletion* is a feature, so it must be addressable
    without going through the 75-method surface a tenant might reach; and it has
    to exist in the file arm too, or `rya dev` cannot exercise the purge path the
    hosted deployment depends on.
    """

    def get(self, workspace: str, generation: int) -> Optional[bytes]:  # pragma: no cover - interface
        raise NotImplementedError

    def current(self, workspace: str) -> Optional[Tuple[int, bytes]]:  # pragma: no cover
        raise NotImplementedError

    def put(self, workspace: str, generation: int, wrapped: bytes) -> None:  # pragma: no cover
        raise NotImplementedError

    def generations(self, workspace: str) -> List[int]:  # pragma: no cover
        raise NotImplementedError

    def destroy(self, workspace: str) -> int:  # pragma: no cover
        raise NotImplementedError

    def workspaces(self) -> List[str]:  # pragma: no cover
        raise NotImplementedError


class FileKeyStore(KeyStore):
    """``<root>/.rya/tenant-keys.json``, 0600. The local arm.

    The wrapped DEKs sit next to the root key that wraps them, which is no worse
    than ``seal.py``'s keyfile and no better: it is at-rest encryption with the
    key co-located, and it is here so the purge path is exercisable locally.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / ".rya" / "tenant-keys.json"
        self._lock = threading.Lock()

    def _read(self) -> Dict[str, Dict[str, str]]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text() or "{}")
        except Exception:
            return {}

    def _write(self, data: Dict[str, Dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:  # pragma: no cover - non-POSIX
            pass

    def get(self, workspace: str, generation: int) -> Optional[bytes]:
        raw = self._read().get(_scope(workspace), {}).get(str(generation))
        return base64.b64decode(raw) if raw else None

    def current(self, workspace: str) -> Optional[Tuple[int, bytes]]:
        gens = self.generations(workspace)
        if not gens:
            return None
        top = gens[-1]
        wrapped = self.get(workspace, top)
        return (top, wrapped) if wrapped else None

    def put(self, workspace: str, generation: int, wrapped: bytes) -> None:
        with self._lock:
            data = self._read()
            data.setdefault(_scope(workspace), {})[str(generation)] = \
                base64.b64encode(wrapped).decode()
            self._write(data)

    def generations(self, workspace: str) -> List[int]:
        return sorted(int(g) for g in self._read().get(_scope(workspace), {}))

    def destroy(self, workspace: str) -> int:
        with self._lock:
            data = self._read()
            gone = len(data.pop(_scope(workspace), {}))
            if gone:
                self._write(data)
            return gone

    def workspaces(self) -> List[str]:
        return sorted(self._read())


_KEY_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {KEY_TABLE} (
  workspace_id  text    NOT NULL,
  generation    int     NOT NULL,
  wrapped_key   bytea   NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, generation)
);
"""


class PostgresKeyStore(KeyStore):
    """The key table.

    **No RLS policy, deliberately.** Every other tenant-scoped table has one, and
    for this table a policy would be actively misleading: the reader is platform
    code (the api sealing a new connection, the claimer's broker opening one) and
    it legitimately reads across workspaces, so a GUC-scoped policy would either
    be bypassed by that reader or break it. What keeps a tenant out of this table
    is that a tenant process has no connection to this database (D18), which is a
    property of the topology rather than of a grant.
    """

    def __init__(self, conn_factory) -> None:
        # A callable returning a psycopg connection, so this can share the store's
        # connection handling without importing it.
        self._conn = conn_factory
        self._ensured = False
        self._lock = threading.Lock()

    def ensure(self) -> None:
        if self._ensured:
            return
        with self._lock:
            if self._ensured:
                return
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(_KEY_SCHEMA)
            self._ensured = True

    def get(self, workspace: str, generation: int) -> Optional[bytes]:
        self.ensure()
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT wrapped_key FROM {KEY_TABLE} "
                        "WHERE workspace_id = %s AND generation = %s",
                        (_scope(workspace), int(generation)))
            row = cur.fetchone()
        return bytes(row[0]) if row else None

    def current(self, workspace: str) -> Optional[Tuple[int, bytes]]:
        self.ensure()
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT generation, wrapped_key FROM {KEY_TABLE} "
                        "WHERE workspace_id = %s ORDER BY generation DESC LIMIT 1",
                        (_scope(workspace),))
            row = cur.fetchone()
        return (int(row[0]), bytes(row[1])) if row else None

    def put(self, workspace: str, generation: int, wrapped: bytes) -> None:
        self.ensure()
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {KEY_TABLE} (workspace_id, generation, wrapped_key) "
                "VALUES (%s, %s, %s) ON CONFLICT (workspace_id, generation) DO NOTHING",
                (_scope(workspace), int(generation), wrapped))

    def generations(self, workspace: str) -> List[int]:
        self.ensure()
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT generation FROM {KEY_TABLE} WHERE workspace_id = %s "
                        "ORDER BY generation", (_scope(workspace),))
            return [int(r[0]) for r in cur.fetchall()]

    def destroy(self, workspace: str) -> int:
        """Delete every generation for a workspace. The crypto-shred (D31)."""
        self.ensure()
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"DELETE FROM {KEY_TABLE} WHERE workspace_id = %s",
                        (_scope(workspace),))
            return cur.rowcount or 0

    def workspaces(self) -> List[str]:
        self.ensure()
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT DISTINCT workspace_id FROM {KEY_TABLE} ORDER BY 1")
            return [r[0] for r in cur.fetchall()]


# ---- how a DEK is protected at rest ----------------------------------------

class Wrapper:
    """Encrypts a data-encryption key for storage. The KMS seam."""

    name = "abstract"

    def wrap(self, material: bytes) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def unwrap(self, blob: bytes) -> bytes:  # pragma: no cover
        raise NotImplementedError


class LocalWrapper(Wrapper):
    """Wrap with a root key held by the process. No KMS required.

    Honest about what it buys: the wrapped DEKs are useless without the root key,
    so the *table* is not a credential store — but the root key still exists in one
    place, so this is per-tenant compromise isolation and crypto-shredding without
    a hardware-backed root. §9 risk 3 asks for "KMS, rotation and a recovery
    story"; this is rotation and shredding with the recovery story being the root
    key you already had.
    """

    name = "local"

    def __init__(self, root_key: bytes) -> None:
        self._f = _fernet(root_key)

    def wrap(self, material: bytes) -> bytes:
        return self._f.encrypt(material)

    def unwrap(self, blob: bytes) -> bytes:
        return self._f.decrypt(blob)


class KmsWrapper(Wrapper):
    """Wrap with AWS KMS. The DEK is never stored in a form KMS cannot revoke.

    Deleting the CMK is a *second* shred lever above D31's row delete, and a
    coarser one — it takes out every workspace at once. Per-workspace erasure is
    still the row delete; the CMK is the deployment-wide backstop.
    """

    name = "kms"

    def __init__(self, key_id: str, *, client=None) -> None:
        self.key_id = key_id
        self._client = client

    def _kms(self):
        if self._client is None:  # pragma: no cover - requires boto3 + AWS
            try:
                import boto3
            except ImportError as exc:
                raise RyaError(
                    E_KEY_UNAVAILABLE,
                    f"{KMS_KEY_ENV} is set but boto3 is not installed.",
                    hint="pip install 'rya[aws]', or unset the variable to wrap keys locally.",
                ) from exc
            self._client = boto3.client("kms")
        return self._client

    def wrap(self, material: bytes) -> bytes:
        out = self._kms().encrypt(KeyId=self.key_id, Plaintext=material)
        return out["CiphertextBlob"]

    def unwrap(self, blob: bytes) -> bytes:
        return self._kms().decrypt(CiphertextBlob=blob)["Plaintext"]


# ---- providers --------------------------------------------------------------

class KeyProvider:
    """Resolves the key to seal with, and the key a ciphertext names."""

    name = "abstract"
    shreddable = False
    per_tenant = False

    def current(self, workspace: str = "") -> TenantKey:  # pragma: no cover - interface
        raise NotImplementedError

    def get(self, key_id: str) -> TenantKey:  # pragma: no cover
        raise NotImplementedError

    def rotate(self, workspace: str = "") -> TenantKey:
        raise RyaError(
            E_KEY_NOT_SHREDDABLE,
            f"The '{self.name}' key provider has nothing to rotate.",
            hint="Rotation needs per-workspace key material the platform stores. "
                 f"Set {PROVIDER_ENV}={PROVIDER_WRAPPED}.",
        )

    def destroy(self, workspace: str) -> int:
        raise RyaError(
            E_KEY_NOT_SHREDDABLE,
            f"The '{self.name}' key provider cannot crypto-shred a workspace.",
            hint=f"Set {PROVIDER_ENV}={PROVIDER_WRAPPED} so each workspace gets a "
                 "random stored key whose deletion makes its sealed data unreadable. "
                 "Until then a purge has to delete rows one table at a time and "
                 "cannot make any claim about backups.",
        )

    def describe(self) -> dict:
        return {"provider": self.name, "shreddable": self.shreddable,
                "perTenant": self.per_tenant}


class DeploymentKeyProvider(KeyProvider):
    """One key for the whole deployment — ``seal.py``'s behaviour, kept.

    This is the default, and it must stay the default: the alternative is that
    every existing deployment fails to open its own secrets after an upgrade.
    """

    name = PROVIDER_DEPLOYMENT

    def __init__(self, material: bytes, source: str = "env") -> None:
        self._material = material
        self.source = source

    def current(self, workspace: str = "") -> TenantKey:
        return TenantKey(key_id=make_key_id(self.name, None, 1),
                         material=self._material, provider=self.name)

    def get(self, key_id: str) -> TenantKey:
        return self.current()

    def describe(self) -> dict:
        return {**super().describe(), "source": self.source}


class DerivedKeyProvider(KeyProvider):
    """Per-workspace keys derived from one root. **Not shreddable.**

    Chosen when compromise isolation is wanted and a key table is not — no
    migration, no rotation state, no extra round trip on the seal path. What it
    cannot do is forget: the key for `ws_abc` is ``HKDF(root, "rya/ws/ws_abc")``
    forever, so :meth:`destroy` refuses instead of pretending.
    """

    name = PROVIDER_DERIVED
    per_tenant = True

    def __init__(self, root_key: bytes) -> None:
        self._root = root_key

    def _derive(self, workspace: str, generation: int) -> bytes:
        info = f"rya/ws/{_scope(workspace)}/{int(generation)}".encode()
        return base64.urlsafe_b64encode(_hkdf(self._root, info=info))

    def current(self, workspace: str = "") -> TenantKey:
        return TenantKey(key_id=make_key_id(self.name, workspace, 1),
                         material=self._derive(workspace, 1),
                         workspace=_scope(workspace), provider=self.name)

    def get(self, key_id: str) -> TenantKey:
        provider, ws, gen = parse_key_id(key_id)
        return TenantKey(key_id=key_id, material=self._derive(ws, gen),
                         workspace=ws, generation=gen, provider=self.name)


class WrappedKeyProvider(KeyProvider):
    """A random DEK per workspace, stored wrapped. **The one D31 needs.**

    Reads go through a small in-process cache because the seal path is on every
    connection read and an unwrap is either a Fernet decrypt or a KMS round trip.
    The cache is keyed by key id, which is content-addressed by construction — a
    generation never changes material — so it cannot go stale in the way
    §9 risk 9's pool could. It IS a copy of key material in platform memory, which
    is the same exposure the deployment key has always had.

    A destroyed workspace's entries are evicted, or a purge would leave a live
    key in the process that just shredded it.
    """

    name = PROVIDER_WRAPPED
    shreddable = True
    per_tenant = True

    def __init__(self, store: KeyStore, wrapper: Wrapper) -> None:
        self.store = store
        self.wrapper = wrapper
        self._cache: Dict[str, bytes] = {}
        self._lock = threading.Lock()

    def _mint(self, workspace: str, generation: int) -> TenantKey:
        material = new_key()
        self.store.put(_scope(workspace), generation, self.wrapper.wrap(material))
        # Re-read rather than trust the write: two processes can race to mint the
        # first key for a workspace, ON CONFLICT DO NOTHING means one of them lost,
        # and the loser must use the winner's key or half the rows are sealed under
        # a key nobody will look for.
        stored = self.store.get(_scope(workspace), generation)
        if stored is not None:
            material = self.wrapper.unwrap(stored)
        key_id = make_key_id(self.name, workspace, generation)
        with self._lock:
            self._cache[key_id] = material
        return TenantKey(key_id=key_id, material=material,
                         workspace=_scope(workspace), generation=generation,
                         provider=self.name)

    def current(self, workspace: str = "") -> TenantKey:
        found = self.store.current(_scope(workspace))
        if found is None:
            return self._mint(workspace, 1)
        gen, wrapped = found
        key_id = make_key_id(self.name, workspace, gen)
        with self._lock:
            cached = self._cache.get(key_id)
        material = cached if cached is not None else self.wrapper.unwrap(wrapped)
        with self._lock:
            self._cache[key_id] = material
        return TenantKey(key_id=key_id, material=material,
                         workspace=_scope(workspace), generation=gen,
                         provider=self.name)

    def get(self, key_id: str) -> TenantKey:
        provider, ws, gen = parse_key_id(key_id)
        with self._lock:
            cached = self._cache.get(key_id)
        if cached is not None:
            return TenantKey(key_id=key_id, material=cached, workspace=ws,
                             generation=gen, provider=self.name)
        wrapped = self.store.get(ws, gen)
        if wrapped is None:
            raise RyaError(
                E_KEY_NOT_FOUND,
                f"No key '{key_id}' — generation {gen} for workspace '{ws}' is not stored.",
                hint="If this workspace was purged, that is the designed outcome: the "
                     "key was destroyed and values sealed under it are unreadable by "
                     "construction (D31). Otherwise the key table has lost a row and "
                     "the data sealed under it is gone; restore the table from backup.",
            )
        material = self.wrapper.unwrap(wrapped)
        with self._lock:
            self._cache[key_id] = material
        return TenantKey(key_id=key_id, material=material, workspace=ws,
                         generation=gen, provider=self.name)

    def rotate(self, workspace: str = "") -> TenantKey:
        """Mint the next generation. Old ciphertext keeps opening until re-sealed.

        Rotation alone re-protects nothing already written — that is what
        :func:`reseal` is for. Splitting them is deliberate: minting is instant and
        a re-seal walks every sealed row, so an operator can rotate now and
        re-seal on a schedule.
        """
        gens = self.store.generations(_scope(workspace))
        return self._mint(workspace, (max(gens) + 1) if gens else 1)

    def destroy(self, workspace: str) -> int:
        gone = self.store.destroy(_scope(workspace))
        prefix = f"{self.name}{KEY_ID_SEP}{_scope(workspace)}{KEY_ID_SEP}"
        with self._lock:
            for key_id in [k for k in self._cache if k.startswith(prefix)]:
                self._cache.pop(key_id, None)
        return gone

    def describe(self) -> dict:
        return {**super().describe(), "wrapper": self.wrapper.name}


# ---- the ring: what sealing actually calls ----------------------------------

class KeyRing:
    """The façade ``seal.py`` delegates to: seal, open, rotate, shred.

    Holds a provider and nothing else. The workspace arrives as an argument on
    every call rather than being bound at construction, because one platform
    process serves many tenants and a ring that remembered a workspace would be
    one ``ctx`` away from sealing `acme`'s secret under `globex`'s key.
    """

    def __init__(self, provider: KeyProvider) -> None:
        self.provider = provider

    # -- properties callers branch on
    @property
    def shreddable(self) -> bool:
        return self.provider.shreddable

    @property
    def per_tenant(self) -> bool:
        return self.provider.per_tenant

    def describe(self) -> dict:
        return {**self.provider.describe(), "envelope": PREFIX_V2.rstrip(":")}

    # -- the seal path
    def seal(self, plaintext: Optional[str], workspace: str = "") -> Optional[str]:
        if plaintext is None or plaintext == "":
            return plaintext
        if is_sealed(plaintext):
            return plaintext
        key = self.provider.current(workspace)
        ct = _fernet(key.material).encrypt(plaintext.encode()).decode()
        return f"{PREFIX_V2}{key.key_id}:{ct}"

    def open(self, value: Optional[str], workspace: str = "") -> Optional[str]:
        """Open a v2 envelope. v1 and plaintext are not this ring's business.

        The envelope names its own key, so ``workspace`` is not needed to *find*
        the key — it is checked against the one the envelope names. That check is
        pure defence in depth (D19) and it is here because the value is cheap and
        the failure it catches is expensive: a platform process legitimately holds
        every tenant's keys, so the only thing standing between a mis-scoped query
        and one tenant reading another's secret is that nothing hands it the wrong
        ciphertext. This makes that a refusal rather than a silent success.

        Passing ``""`` means "no tenant dimension" and skips the check, which is
        what `FileStore` and the deployment provider genuinely are.
        """
        if value is None or not value.startswith(PREFIX_V2):
            return value
        key_id, _, ct = value[len(PREFIX_V2):].partition(":")
        if not ct:
            raise RyaError(E_KEY_NOT_FOUND,
                           "Sealed value carries no ciphertext after its key id.")
        named, sealed_ws, _gen = parse_key_id(key_id)
        asked = _scope(workspace)
        if asked != NO_SCOPE and sealed_ws != NO_SCOPE and asked != sealed_ws:
            raise RyaError(
                "E_KEY_WORKSPACE_MISMATCH",
                f"This value was sealed for workspace '{sealed_ws}' and workspace "
                f"'{asked}' asked to open it.",
                hint="A row reached a reader scoped to a different tenant. The key "
                     "ring refuses rather than decrypting, because it holds every "
                     "tenant's keys and is therefore the last place that can tell.",
            )
        if named != self.provider.name:
            # Worth its own error because the alternative is a bare
            # `InvalidToken`: the ciphertext is fine and the key is wrong, and the
            # cause is almost always that RYA_KEY_PROVIDER changed under a
            # deployment that had already sealed rows.
            raise RyaError(
                E_KEY_NOT_FOUND,
                f"This value was sealed by the '{named}' key provider, but "
                f"'{self.provider.name}' is configured.",
                hint=f"Set {PROVIDER_ENV}={named} to read it, then `rya keys reseal` "
                     "to move it across. Changing provider does not re-address "
                     "ciphertext already written.",
            )
        key = self.provider.get(key_id)
        return _fernet(key.material).decrypt(ct.encode()).decode()

    def key_id_of(self, value: Optional[str]) -> Optional[str]:
        if not value or not value.startswith(PREFIX_V2):
            return None
        return value[len(PREFIX_V2):].partition(":")[0] or None

    def needs_reseal(self, value: Optional[str], workspace: str = "") -> bool:
        """True when ``value`` is not sealed under the workspace's current key.

        Covers three cases with one question, which is why re-seal is one loop:
        plaintext from before sealing existed, a v1 envelope from before per-tenant
        keys, and a v2 envelope naming a superseded generation.
        """
        if value is None or value == "":
            return False
        if not is_sealed(value):
            return True
        if value.startswith(PREFIX_V1):
            return True
        return self.key_id_of(value) != self.provider.current(workspace).key_id

    # -- lifecycle
    def rotate(self, workspace: str = "") -> TenantKey:
        return self.provider.rotate(workspace)

    def destroy(self, workspace: str) -> int:
        """Crypto-shred one workspace. Returns the number of generations destroyed."""
        return self.provider.destroy(workspace)


def is_sealed(value) -> bool:
    """True for either envelope. The one predicate both modules agree on."""
    return isinstance(value, str) and (value.startswith(PREFIX_V1)
                                       or value.startswith(PREFIX_V2))


# ---- resolution -------------------------------------------------------------

def root_key(env: Optional[Mapping[str, str]] = None,
             root: Optional[Path] = None) -> Tuple[bytes, str]:
    """The deployment root key and where it came from. ``seal.py``'s order, kept.

    Returning the source alongside the material is not decoration: `readiness`
    and the credential inventory both need to say *which* of the two a deployment
    is relying on, and "env" versus "keyfile" is the difference between a key from
    a secrets manager and a key sitting in the project directory.
    """
    env = env if env is not None else os.environ
    key = env.get(ROOT_KEY_ENV)
    if key:
        return (key.encode() if isinstance(key, str) else key), "env"
    if root is None:
        raise RyaError(
            E_KEY_UNAVAILABLE,
            f"{ROOT_KEY_ENV} is required to seal secrets without a project keyfile.",
            hint="Supply it from your secrets manager. There is no project directory "
                 "in a server context, so there is nowhere safe to persist one.",
        )
    path = Path(root) / ".rya" / "secret.key"
    if path.exists():
        return path.read_bytes(), "keyfile"
    path.parent.mkdir(parents=True, exist_ok=True)
    material = new_key()
    path.write_bytes(material)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - non-POSIX
        pass
    return material, "keyfile"


def resolve_keyring(*, root: Optional[Path] = None,
                    env: Optional[Mapping[str, str]] = None,
                    key_store: Optional[KeyStore] = None,
                    wrapper: Optional[Wrapper] = None) -> KeyRing:
    """The declared key provider, defaulting to the deployment key.

    Same seam shape as ``open_store``/``resolve_bundle_store``/``resolve_driver``:
    one environment variable, and the default is the arm that works with nothing
    configured. The default is ``deployment`` rather than the strongest option on
    purpose — an upgrade must not change how existing ciphertext is addressed.
    """
    env = env if env is not None else os.environ
    if not available():
        raise RyaError(
            E_KEY_UNAVAILABLE,
            "The 'cryptography' package is not installed, so keys cannot be resolved.",
            hint="pip install cryptography. `seal.py` degrades to plaintext and flags "
                 "it; a key ring cannot, because the caller asked for a key.",
        )
    name = (env.get(PROVIDER_ENV) or PROVIDER_DEPLOYMENT).strip().lower()
    if name not in PROVIDERS:
        raise RyaError(
            E_KEY_PROVIDER_UNKNOWN,
            f"No key provider named '{name}'.",
            hint=f"One of: {', '.join(PROVIDERS)}. Only '{PROVIDER_WRAPPED}' can "
                 "crypto-shred a tenant (D31).",
        )
    if name == PROVIDER_DEPLOYMENT:
        material, source = root_key(env, root)
        return KeyRing(DeploymentKeyProvider(material, source))
    if name == PROVIDER_DERIVED:
        material, _ = root_key(env, root)
        return KeyRing(DerivedKeyProvider(material))
    # wrapped
    if wrapper is None:
        kms = (env.get(KMS_KEY_ENV) or "").strip()
        if kms:
            wrapper = KmsWrapper(kms)
        else:
            material, _ = root_key(env, root)
            wrapper = LocalWrapper(material)
    if key_store is None:
        if root is None:
            raise RyaError(
                E_KEY_UNAVAILABLE,
                f"{PROVIDER_ENV}={PROVIDER_WRAPPED} needs somewhere to store wrapped "
                "keys, and neither a key store nor a project root was supplied.",
                hint="In a server context pass the store's key table; locally pass a "
                     "project root and the keys land in .rya/tenant-keys.json.",
            )
        key_store = FileKeyStore(root)
    return KeyRing(WrappedKeyProvider(key_store, wrapper))


# ---- re-seal ----------------------------------------------------------------

def reseal(store, *, keyring: KeyRing, workspace: str = "") -> dict:
    """Re-seal every connection secret under the workspace's current key.

    Lives here rather than on the store because it is a *key* operation that
    happens to touch rows: after :meth:`KeyRing.rotate` the old generation is
    still needed to read, and this is the loop that ends that dependency. The
    store's own ``reseal_connections`` predates this and answers a narrower
    question (plaintext → sealed); this one also catches v1 → v2 and a superseded
    generation.

    Reports rather than raises on a value it cannot open: one unreadable secret
    must not stop the other 99 from being re-sealed, and a value whose key is gone
    is exactly what a purged-then-restored workspace looks like.
    """
    out = {"scanned": 0, "resealed": 0, "current": 0, "empty": 0, "failed": 0,
           "errors": []}
    for row in store.list_connections():
        out["scanned"] += 1
        conn = store.get_connection(row.get("provider"), row.get("owner")) or {}
        secret = conn.get("secret")
        if not secret:
            out["empty"] += 1
            continue
        try:
            if not keyring.needs_reseal(secret, workspace):
                out["current"] += 1
                continue
            # get_connection already returned it opened, so seal the plaintext.
            store.upsert_connection(row.get("provider"), row.get("scopes") or [],
                                    secret=secret, owner=row.get("owner"))
            out["resealed"] += 1
        except Exception as exc:  # noqa: BLE001 - reported per row, not fatal
            out["failed"] += 1
            out["errors"].append({"provider": row.get("provider"),
                                  "owner": row.get("owner"),
                                  "error": f"{type(exc).__name__}: {exc}"})
    return out
