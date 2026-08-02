"""Two-phase tenant deletion (D31) — exercised, not reasoned about.

The exit criterion is that a purge "destroys the tenant's seal key, its bundle
objects and its `_DATA_TABLES` rows, and leaves an anonymised audit stub", and the
phrase that matters is *exercised*. So these tests build a real workspace with real
sealed secrets and real bundle archives, purge it, and then check the thing that is
actually hard to fake: that the other tenant's secret still opens and this one's does
not.

Row deletion needs Postgres and is covered by :func:`test_a_purge_reports_which_tables_it_could_not_reach`
plus the Postgres-marked test at the bottom; everything else runs on the file arm.
"""

import json
import os

import pytest

from rya import bundles, keys, purge, seal
from rya.errors import RyaError
from rya.store import Store


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """A wrapped-key deployment: the only provider that can crypto-shred."""
    monkeypatch.setenv("RYA_KEY_PROVIDER", keys.PROVIDER_WRAPPED)
    monkeypatch.setenv("RYA_SECRET_KEY", keys.new_key().decode())
    store = Store(tmp_path / ".rya")
    store.ensure()
    ring = keys.resolve_keyring(root=tmp_path)
    return tmp_path, store, ring


# ---- phase one: disable ----------------------------------------------------

def test_a_new_workspace_is_active_without_anything_being_written(tmp_path):
    """Absence means active, which is what every workspace starts as — a lifecycle
    row that had to exist would make every pre-existing deployment look disabled."""
    store = Store(tmp_path / ".rya")
    store.ensure()
    state = purge.lifecycle(store)
    assert state.active and state.state == purge.STATE_ACTIVE
    purge.require_active(store)      # does not raise


def test_disable_stops_admission_everywhere_it_is_checked(tmp_path):
    """The enforcement half. A disable that only revoked API keys would leave every
    already-queued item to run."""
    from rya.quotas import require_admission

    store = Store(tmp_path / ".rya")
    store.ensure()
    purge.disable(store, reason="non-payment", actor="ops@example.com")

    for kind in ("run", "job", "worker", "model"):
        with pytest.raises(RyaError) as e:
            require_admission(store, kind=kind)
        assert e.value.code == "E_WORKSPACE_DISABLED", kind
        assert "non-payment" in e.value.message


def test_disable_is_reversible_and_says_so(tmp_path):
    """Reversible is the whole reason it is a separate phase: a billing failure or an
    abuse report is a recoverable mistake often enough."""
    store = Store(tmp_path / ".rya")
    store.ensure()
    purge.disable(store, reason="suspected abuse")
    with pytest.raises(RyaError) as e:
        purge.require_active(store)
    assert "reversible" in (e.value.hint or "")
    assert purge.enable(store, actor="ops")["state"] == purge.STATE_ACTIVE
    purge.require_active(store)


def test_disable_revokes_the_workspaces_api_keys():
    """Stops new callers, which the lifecycle state alone does not."""
    class FakeTenancy:
        def __init__(self):
            self.keys = [{"id": "k1"}, {"id": "k2"}]
            self.revoked = []

        def list_keys(self, ws):
            return list(self.keys)

        def revoke_key(self, ws, kid):
            self.revoked.append((ws, kid))
            return True

    class FakeStore:
        def __init__(self):
            self.rows = {}

        def policy_get(self, key):
            return self.rows.get(key)

        def policy_set(self, key, value, actor=None):
            self.rows[key] = value
            return value

    tenancy, store = FakeTenancy(), FakeStore()
    out = purge.disable(store, reason="x", tenancy=tenancy, workspace="ws_a")
    assert out["keysRevoked"] == 2
    assert tenancy.revoked == [("ws_a", "k1"), ("ws_a", "k2")]


def test_a_lifecycle_read_failure_fails_open(tmp_path):
    """Unlike the guard, and the difference is deliberate: a missing allowlist is a
    security question, and an unreadable lifecycle row is availability."""
    class Broken:
        def policy_get(self, key):
            raise RuntimeError("database is down")

    assert purge.lifecycle(Broken()).active is True
    purge.require_active(Broken())


# ---- the retention window --------------------------------------------------

def test_purge_refuses_an_active_workspace(tmp_path):
    """Two phases on purpose. Purging straight from active would make `disable`
    optional, and `disable` is the reversible one."""
    store = Store(tmp_path / ".rya")
    store.ensure()
    with pytest.raises(RyaError) as e:
        purge.purge(store, workspace="ws_a")
    assert e.value.code == "E_PURGE_NOT_ALLOWED"
    assert "only a disabled workspace" in e.value.message


def test_purge_refuses_inside_the_retention_window(tmp_path):
    store = Store(tmp_path / ".rya")
    store.ensure()
    purge.disable(store, reason="x", retention_days=30)
    with pytest.raises(RyaError) as e:
        purge.purge(store, workspace="ws_a")
    assert "retention window runs until" in e.value.message
    assert "--force skips it deliberately" in (e.value.hint or "")


def test_a_zero_retention_window_is_immediately_purgeable(tmp_path):
    """An operator who wants no window should get one by configuring it, not by
    passing --force to every purge and losing the check's meaning."""
    store = Store(tmp_path / ".rya")
    store.ensure()
    purge.disable(store, reason="test tenant", retention_days=0)
    report = purge.purge(store, workspace="ws_a", dry_run=True)
    assert report.dry_run is True


def test_force_skips_both_the_state_and_the_window(tmp_path):
    """A court order and a throwaway test tenant both need it, and it must be an
    explicit act rather than a default."""
    store = Store(tmp_path / ".rya")
    store.ensure()
    report = purge.purge(store, workspace="ws_a", force=True, dry_run=True)
    assert report.dry_run is True


def test_a_purged_workspace_cannot_be_re_enabled(tmp_path):
    store = Store(tmp_path / ".rya")
    store.ensure()
    store.policy_set(purge.POLICY_KEY, {"state": purge.STATE_PURGED})
    with pytest.raises(RyaError) as e:
        purge.enable(store)
    assert e.value.code == "E_PURGE_NOT_ALLOWED"
    assert "cannot open its own secrets" in (e.value.hint or "")


# ---- the crypto-shred, which is the point ----------------------------------

def test_shredding_one_tenants_key_leaves_the_others_readable(wired):
    """The exit criterion: "compromising one tenant's seal key decrypts nothing
    belonging to another" — checked from the other direction, which is stronger.
    Destroying `acme`'s key must not touch `globex`'s."""
    root, store, ring = wired
    acme = seal.seal("acme-token", root, workspace="ws_acme")
    globex = seal.seal("globex-token", root, workspace="ws_globex")
    assert seal.unseal(acme, root, workspace="ws_acme") == "acme-token"

    destroyed = ring.destroy("ws_acme")
    assert destroyed == 1

    with pytest.raises(RyaError) as e:
        seal.unseal(acme, root, workspace="ws_acme")
    assert e.value.code == "E_KEY_NOT_FOUND"
    assert "designed outcome" in (e.value.hint or "")
    # And the neighbour is untouched.
    assert seal.unseal(globex, root, workspace="ws_globex") == "globex-token"


def test_a_shred_destroys_every_generation_not_just_the_current_one(wired):
    """A rotation leaves older ciphertext readable under an older key, so shredding
    only the newest would leave everything written before the last rotation open."""
    root, store, ring = wired
    old = seal.seal("before-rotation", root, workspace="ws_a")
    ring.rotate("ws_a")
    new = seal.seal("after-rotation", root, workspace="ws_a")
    assert ring.key_id_of(old) != ring.key_id_of(new)

    assert ring.destroy("ws_a") == 2
    for value in (old, new):
        with pytest.raises(RyaError):
            ring.open(value, "ws_a")


def test_a_purge_on_the_default_key_provider_makes_no_cryptographic_claim(tmp_path,
                                                                         monkeypatch):
    """The honest gap, reported rather than implied. One key for the whole deployment
    means shredding it would destroy every tenant's data, so there is nothing to
    shred — and an operator answering a deletion request must not be told otherwise."""
    monkeypatch.delenv("RYA_KEY_PROVIDER", raising=False)
    monkeypatch.setenv("RYA_SECRET_KEY", keys.new_key().decode())
    store = Store(tmp_path / ".rya")
    store.ensure()
    ring = keys.resolve_keyring(root=tmp_path)
    assert ring.shreddable is False

    report = purge.purge(store, workspace="ws_a", keyring=ring, force=True)
    assert report.crypto_shredded is False
    assert "cannot crypto-shred" in report.key_note
    assert "NO cryptographic claim" in report.attestation()
    assert "Backups taken before now still contain readable data" in report.attestation()
    # A dry run says the same thing in the future tense rather than going quiet.
    dry = purge.purge(store, workspace="ws_a", keyring=ring, force=True, dry_run=True)
    assert "could NOT crypto-shred" in dry.attestation()


def test_a_derived_key_provider_is_refused_for_shredding_by_name(tmp_path, monkeypatch):
    """The trap: HKDF-per-workspace delivers compromise isolation and *cannot forget*,
    so a deployment believing D31 was satisfied would have a provably false erasure
    story."""
    monkeypatch.setenv("RYA_KEY_PROVIDER", keys.PROVIDER_DERIVED)
    monkeypatch.setenv("RYA_SECRET_KEY", keys.new_key().decode())
    store = Store(tmp_path / ".rya")
    store.ensure()
    ring = keys.resolve_keyring(root=tmp_path)
    assert ring.per_tenant is True and ring.shreddable is False

    report = purge.purge(store, workspace="ws_a", keyring=ring, force=True)
    assert report.crypto_shredded is False
    assert "derivable from a root" in report.key_note


def test_the_shred_happens_before_the_bulk_deletion(wired):
    """A purge interrupted halfway has already delivered the property that matters
    most. Asserted through the report: the key note is populated even when a later
    step failed."""
    root, store, ring = wired
    seal.seal("secret", root, workspace="ws_a")
    report = purge.purge(store, workspace="ws_a", keyring=ring, force=True,
                         # An unreachable database, so step 3 fails after step 1 ran.
                         admin_dsn="postgresql://nobody@127.0.0.1:1/none")
    assert report.crypto_shredded is True
    assert report.ok is False
    assert "already unreadable" in report.attestation()


# ---- bundle objects (D20 is what makes this enumerable) --------------------

def _archive(root, workspace, payload=b"x"):
    store = bundles.BundleStore(kind="local", root=root / "archives",
                                workspace=workspace)
    import hashlib

    digest = hashlib.sha256(payload).hexdigest()
    path = bundles.bundle_archive_path(digest, root / "archives", workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return store, digest


def test_bundle_objects_are_enumerable_per_tenant_only_because_d20_exists(tmp_path):
    """D31 named #7 a prerequisite for exactly this: a flat content-addressed
    namespace has no per-tenant listing."""
    acme, _ = _archive(tmp_path, "ws_acme", b"acme-bundle")
    _archive(tmp_path, "ws_globex", b"globex-bundle")
    assert len(bundles.list_workspace_objects(acme, "ws_acme")) == 1
    assert len(bundles.list_workspace_objects(acme, "ws_globex")) == 1


def test_enumerating_an_un_namespaced_store_is_refused_not_empty(tmp_path):
    """The dangerous case. An empty workspace addresses the SHARED namespace, so
    deleting what a listing returned would delete every tenant's archives."""
    store, _ = _archive(tmp_path, "ws_acme")
    with pytest.raises(RyaError) as e:
        bundles.list_workspace_objects(store, "")
    assert e.value.code == "E_BUNDLE_STORE"
    assert "would delete every tenant's archives" in (e.value.hint or "")


def test_deleting_one_tenants_objects_leaves_the_others(tmp_path):
    acme, _ = _archive(tmp_path, "ws_acme", b"acme-bundle")
    _archive(tmp_path, "ws_globex", b"globex-bundle")
    assert bundles.delete_workspace_objects(acme, "ws_acme") == 1
    assert bundles.list_workspace_objects(acme, "ws_acme") == []
    assert len(bundles.list_workspace_objects(acme, "ws_globex")) == 1
    # No directory tree left behind to suggest the workspace still exists.
    assert not (tmp_path / "archives" / "ws_acme").exists()


def test_a_purge_counts_the_objects_it_would_delete_before_deleting_them(wired):
    """A dry run for a step with no undo, and the counts it reports are the ones the
    real run will report."""
    root, store, ring = wired
    archive, _ = _archive(root, "ws_a", b"bundle-bytes")
    dry = purge.purge(store, workspace="ws_a", keyring=ring, bundle_store=archive,
                      force=True, dry_run=True)
    assert dry.objects_deleted == 1
    assert "would delete 1 object" in dry.object_note
    assert bundles.list_workspace_objects(archive, "ws_a")   # still there

    real = purge.purge(store, workspace="ws_a", keyring=ring, bundle_store=archive,
                       force=True)
    assert real.objects_deleted == 1
    assert bundles.list_workspace_objects(archive, "ws_a") == []


# ---- the audit stub --------------------------------------------------------

def test_the_audit_stub_keeps_the_decision_and_drops_the_payload(wired):
    """D31's unresolved tension, implemented as the position it took — and written so
    a reviewer can see the whole of what was kept."""
    root, store, ring = wired
    purge.disable(store, reason="non-payment", actor="ops@example.com",
                  retention_days=0)
    report = purge.purge(store, workspace="ws_acme", keyring=ring,
                         actor="ops@example.com")
    stub = report.audit_stub
    assert stub is not None
    assert stub["workspace"] == "ws_acme"
    assert stub["disabledReason"] == "non-payment"
    assert stub["cryptoShredded"] is True
    assert "no identifiers, payloads or run content" in stub["retained"]
    assert "jurisdiction requiring full erasure" in stub["note"]
    # Nothing a person could be identified from beyond the actor who acted.
    blob = json.dumps(stub)
    assert "non-payment" in blob      # the reason IS kept
    assert stub.keys() >= {"purgedAt", "purgedBy", "counts", "retained", "note"}


def test_the_purged_state_survives_the_row_deletion(wired):
    """Written last and through the policy surface, so the row that says "purged" is
    not caught by the deletion it describes."""
    root, store, ring = wired
    purge.disable(store, reason="x", retention_days=0)
    purge.purge(store, workspace="ws_a", keyring=ring)
    assert purge.lifecycle(store).state == purge.STATE_PURGED
    with pytest.raises(RyaError) as e:
        purge.require_active(store)
    assert "cannot be restored" in (e.value.hint or "")


def test_the_attestation_distinguishes_the_two_things_it_could_mean(wired):
    """The difference between "unreadable by construction" and "rows deleted" is
    exactly the difference someone answering a legal request must not get wrong."""
    root, store, ring = wired
    seal.seal("something", root, workspace="ws_a")   # so there IS a key to destroy
    shredded = purge.purge(store, workspace="ws_a", keyring=ring, force=True)
    assert shredded.key_generations == 1
    assert "unreadable without enumerating" in shredded.attestation()

    plain = purge.purge(store, workspace="ws_b", keyring=None, force=True)
    assert "purged by deletion only" in plain.attestation()
    assert plain.crypto_shredded is False


def test_shredding_a_workspace_that_never_had_a_key_says_so(wired):
    """A shreddable provider and zero generations is neither a failure nor a shred.
    Claiming "its seal key was destroyed" would be a small untruth in the one sentence
    an operator quotes to answer a deletion request."""
    root, store, ring = wired
    report = purge.purge(store, workspace="ws_never_used", keyring=ring, force=True)
    assert report.crypto_shredded is True and report.key_generations == 0
    assert "no per-tenant key existed" in report.key_note
    assert "No key was destroyed because none existed" in report.attestation()
    assert "its seal key was destroyed" not in report.attestation()


def test_a_purge_reports_which_tables_it_could_not_reach(wired):
    """One missing table must not abandon the other eighteen: a partially-migrated
    database is a real state, and giving up on the first gap leaves the most data."""
    root, store, ring = wired
    report = purge.purge(store, workspace="ws_a", keyring=ring, force=True,
                         admin_dsn="postgresql://nobody@127.0.0.1:1/none")
    assert report.ok is False
    assert any("could not connect" in e or "rya_runs" in e for e in report.errors)
    # And the shred still happened, because it runs first.
    assert report.crypto_shredded is True


def test_default_and_empty_are_one_tenant_for_keys_as_they_are_for_bundles(wired):
    """A live bug, and the worst kind: `FileStore` sealed under "" while
    `rya workspaces purge default` shredded "default", so the purge destroyed nothing
    and reported it accurately.

    `bundles._normalize_workspace` already had this rule. Two modules deciding what
    "no tenant" means, differently, is the shape of the bug — so `keys._scope` uses the
    same sentinel.
    """
    root, store, ring = wired
    sealed = seal.seal("token", root)                 # FileStore: no workspace at all
    assert ring.key_id_of(sealed) == keys.make_key_id(keys.PROVIDER_WRAPPED, "", 1)
    # And the purge of "default" — what the CLI passes for a single-tenant store —
    # destroys exactly that key.
    report = purge.purge(store, workspace="default", keyring=ring, force=True)
    assert report.key_generations == 1
    with pytest.raises(RyaError):
        seal.unseal(sealed, root)


def test_a_named_workspace_is_not_the_untenanted_one(wired):
    """The other half: `ws_acme` must not normalise into the shared scope, or every
    tenant would share one key and D31 would shred all of them at once."""
    root, store, ring = wired
    shared = seal.seal("shared", root, workspace="default")
    named = seal.seal("named", root, workspace="ws_acme")
    assert ring.key_id_of(shared) != ring.key_id_of(named)
    ring.destroy("ws_acme")
    assert seal.unseal(shared, root, workspace="default") == "shared"


# ---- the whole thing, against real Postgres --------------------------------

PG = os.environ.get("RYA_TEST_DATABASE_URL")


@pytest.mark.skipif(not PG, reason="set RYA_TEST_DATABASE_URL to run the Postgres purge")
def test_a_real_purge_erases_one_tenant_and_leaves_the_neighbour_intact(tmp_path,
                                                                       monkeypatch):
    """The exit criterion, exercised rather than reasoned about — and the only place
    `_delete_rows` and `_delete_tenancy` actually run, because both need the admin
    connection.

    Two tenants, both with sealed secrets, rows and identifiers. Purge one. What makes
    this the load-bearing assertion is the *second* half: it is easy to delete
    everything and easy to delete nothing, and the property that matters is deleting
    exactly one tenant's worth.
    """
    import psycopg

    from rya.store_postgres import PostgresStore
    from rya.tenancy import Tenancy

    monkeypatch.setenv("RYA_KEY_PROVIDER", keys.PROVIDER_WRAPPED)
    monkeypatch.setenv("RYA_SECRET_KEY", keys.new_key().decode())

    tenancy = Tenancy(PG)
    tenancy.setup()
    acme = tenancy.create_workspace("purge-acme")["id"]
    globex = tenancy.create_workspace("purge-globex")["id"]
    tenancy.create_api_key(acme, "k1")
    tenancy.invite_member(acme, "someone@example.com", "u_inviter")

    ring = keys.resolve_keyring(
        root=tmp_path,
        key_store=keys.PostgresKeyStore(lambda: psycopg.connect(PG, autocommit=True)))
    a_store = PostgresStore(PG, workspace_id=acme)
    g_store = PostgresStore(PG, workspace_id=globex)
    a_store.ensure()
    g_store.ensure()
    a_secret = ring.seal("acme-stripe-key", acme)
    g_secret = ring.seal("globex-stripe-key", globex)
    # Ids from the store, NOT literals. `rya_runs.id` is the primary key across the
    # whole table, and `save_run` upserts on it without re-homing the row to a
    # different workspace — correctly, because a run must not change tenant. A fixed
    # literal therefore collides with a previous run of this test against the same
    # database and silently updates the OLD workspace's row, leaving this workspace
    # with zero runs and the assertion below failing for a reason that has nothing to
    # do with the purge.
    a_run, g_run = a_store.new_run_id(), g_store.new_run_id()
    a_store.save_run({"id": a_run, "status": "completed", "journal": {}, "trace": []})
    g_store.save_run({"id": g_run, "status": "completed", "journal": {}, "trace": []})

    purge.disable(a_store, reason="test", retention_days=0, tenancy=tenancy,
                  workspace=acme)
    report = purge.purge(a_store, workspace=acme, keyring=ring, admin_dsn=PG)

    assert report.ok, report.errors
    assert report.crypto_shredded and report.key_generations == 1
    assert report.rows_deleted.get("rya_runs") == 1
    # The identifiers, which `_DATA_TABLES` alone would have left behind.
    assert report.rows_deleted.get("rya_workspace_members") == 1
    assert report.rows_deleted.get("rya_workspaces") == 1

    # acme's secret is unreadable by construction.
    with pytest.raises(RyaError) as e:
        ring.open(a_secret, acme)
    assert e.value.code == "E_KEY_NOT_FOUND"
    # And the neighbour is entirely untouched — key, rows and identity.
    assert ring.open(g_secret, globex) == "globex-stripe-key"
    assert len(g_store.list_runs()) == 1
    assert any(w["id"] == globex for w in tenancy.list_workspaces())
    assert not any(w["id"] == acme for w in tenancy.list_workspaces())


# ---- the KMS arm, which is what production would use ------------------------

class _FakeKms:
    """Enough of the KMS API to exercise the wrapper's contract, and no more.

    Worth having because `wrapped`+KMS is the arm §9 risk 3 actually asks for — "KMS,
    rotation and a recovery story" — and it is the one arm that cannot be exercised
    against the real service in CI. The injection point exists on `KmsWrapper` for
    this reason rather than for mocking convenience.
    """

    def __init__(self):
        self.encrypts = 0
        self.decrypts = 0
        # OPAQUE handles, which is what KMS actually returns: the blob carries no
        # recoverable plaintext. A first cut of this fake embedded the key id in a
        # colon-delimited blob and broke on a real ARN, which contains colons — a
        # reminder that a fake should imitate the contract rather than invent a format.
        self._blobs = {}

    def encrypt(self, KeyId, Plaintext):     # noqa: N803 - boto3's casing
        self.encrypts += 1
        handle = f"kms-blob-{KeyId}-{len(self._blobs)}".encode()
        self._blobs[handle] = Plaintext
        return {"CiphertextBlob": handle}

    def decrypt(self, CiphertextBlob):       # noqa: N803
        self.decrypts += 1
        return {"Plaintext": self._blobs[bytes(CiphertextBlob)]}


def test_the_kms_wrapper_never_stores_an_unwrapped_key(tmp_path, monkeypatch):
    """The property KMS buys: the key table holds nothing usable without a live
    Decrypt call, so a database backup is not a credential store."""
    monkeypatch.setenv("RYA_KEY_PROVIDER", keys.PROVIDER_WRAPPED)
    monkeypatch.setenv("RYA_SECRET_KEY", keys.new_key().decode())
    kms = _FakeKms()
    store = keys.FileKeyStore(tmp_path)
    ring = keys.KeyRing(keys.WrappedKeyProvider(
        store, keys.KmsWrapper("arn:aws:kms:eu-west-1:1:key/abc", client=kms)))

    sealed = ring.seal("tenant-secret", "ws_acme")
    assert ring.open(sealed, "ws_acme") == "tenant-secret"
    assert kms.encrypts == 1          # one DEK minted, wrapped once
    assert ring.describe()["wrapper"] == "kms"

    # What is at rest is the WRAPPED blob, and it is not the key.
    at_rest = store.get("ws_acme", 1)
    assert at_rest.startswith(b"kms-blob-")
    assert b"tenant-secret" not in at_rest


def test_the_kms_wrapper_still_shreds_by_deleting_the_row(tmp_path, monkeypatch):
    """The CMK is a second, coarser lever — deleting it takes out every workspace at
    once. Per-workspace erasure is still the row delete, so D31 does not depend on a
    KMS operation."""
    monkeypatch.setenv("RYA_KEY_PROVIDER", keys.PROVIDER_WRAPPED)
    monkeypatch.setenv("RYA_SECRET_KEY", keys.new_key().decode())
    kms = _FakeKms()
    ring = keys.KeyRing(keys.WrappedKeyProvider(
        keys.FileKeyStore(tmp_path), keys.KmsWrapper("arn:key", client=kms)))
    acme = ring.seal("a", "ws_acme")
    globex = ring.seal("g", "ws_globex")

    assert ring.destroy("ws_acme") == 1
    with pytest.raises(RyaError):
        ring.open(acme, "ws_acme")
    assert ring.open(globex, "ws_globex") == "g"


def test_kms_is_selected_by_declaring_a_key_id(monkeypatch, tmp_path):
    """Same seam shape as everything else: one environment variable, and the default
    is the arm that needs nothing configured."""
    monkeypatch.setenv("RYA_KEY_PROVIDER", keys.PROVIDER_WRAPPED)
    monkeypatch.setenv("RYA_SECRET_KEY", keys.new_key().decode())
    local = keys.resolve_keyring(root=tmp_path)
    assert local.describe()["wrapper"] == "local"

    monkeypatch.setenv("RYA_KMS_KEY_ID", "arn:aws:kms:eu-west-1:1:key/abc")
    with_kms = keys.resolve_keyring(root=tmp_path)
    assert with_kms.describe()["wrapper"] == "kms"
    # And the inventory classifies the key id as a platform credential, so a tenant
    # process holding it is a violation rather than a curiosity.
    from rya.broker.inventory import CLASS_PLATFORM, classify

    assert classify("RYA_KMS_KEY_ID")[0] == CLASS_PLATFORM
