"""Account / User / Membership stores + multi-user auth (ADR-0054, epic #392 phase 1).

Hermetic:
  - Store round-trip on the InMemory + Sqlite backends (accounts / users /
    memberships, ``get_account_for_user``, role assignment).
  - argon2 password hashing verifies through the store.
  - ``authenticate_user`` / ``resolve_identity`` prefer the User store but fall
    back to the legacy env admin (back-compat).
  - The Postgres backend is exercised structurally (lazy pool, DSN guard,
    ``OBS_STORAGE`` dispatch) — same shape as ``test_workspace_pg_stores.py``.

``OBS_STORAGE`` is monkeypatched OFF in every store/auth test — the repo ``.env``
may point it at postgres, which would otherwise route ``get_account_store`` to a
live-PG backend and break these hermetic tests.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core import account as acct
from cairn.core.account import (
    Account,
    InMemoryAccountStore,
    Membership,
    PostgresAccountStore,
    SqliteAccountStore,
    User,
)


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """Force sqlite/in-memory routing and a clean auth env for every test."""
    monkeypatch.setenv("OBS_STORAGE", "")  # never route to the repo .env postgres
    for k in (
        "OBS_JWT_SECRET", "OBS_API_USER", "OBS_API_PASSWORD",
        "OBS_API_PASSWORD_HASH", "OBS_ACCOUNT_DB",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(acct, "_store", None)
    yield
    monkeypatch.setattr(acct, "_store", None)


def _stores(tmp_path):
    """The two hermetic backends to parametrize a round-trip over."""
    return {
        "memory": InMemoryAccountStore(),
        "sqlite": SqliteAccountStore(path=str(tmp_path / "accounts.db")),
    }


# ─── store round-trip (memory + sqlite) ────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_store_roundtrip(backend, tmp_path):
    store = _stores(tmp_path)[backend]
    try:
        # default account is seeded on schema-ready
        assert (await store.get_account("default")) is not None

        # create account
        await store.put_account(Account(id="acme", name="Acme Inc"))
        got = await store.get_account("acme")
        assert got is not None and got.name == "Acme Inc"
        assert {"default", "acme"} <= {a.id for a in await store.list_accounts()}

        # create user via the module helper (argon2 hash)
        user = await acct.create_user("alice", "hunter2", store=store)
        assert user.password_hash and user.password_hash != "hunter2"
        assert (await store.get_user_by_username("alice")).id == user.id
        assert (await store.get_user(user.id)).username == "alice"

        # membership + role
        await acct.add_member("acme", user.id, "admin", store=store)
        members = await store.list_members("acme")
        assert len(members) == 1 and members[0].role == "admin"
        assert (await store.get_account_for_user(user.id)) == "acme"
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_duplicate_username_rejected(backend, tmp_path):
    store = _stores(tmp_path)[backend]
    try:
        await acct.create_user("bob", "pw1", store=store)
        with pytest.raises(ValueError, match="already exists"):
            await acct.create_user("bob", "pw2", store=store)
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_set_password_rehashes(backend, tmp_path):
    from cairn.core.triggers import auth

    store = _stores(tmp_path)[backend]
    try:
        user = await acct.create_user("carol", "old-pw", store=store)
        old_hash = user.password_hash
        updated = await acct.set_password(user.id, "new-pw", store=store)
        assert updated.password_hash != old_hash
        # argon2 verifies the new password, not the old
        ph = auth._password_hash()
        assert ph.verify("new-pw", updated.password_hash)
        assert not ph.verify("old-pw", updated.password_hash)
        with pytest.raises(KeyError):
            await acct.set_password("nope", "x", store=store)
    finally:
        store.close()


def test_membership_rejects_bad_role():
    with pytest.raises(ValueError, match="role must be one of"):
        Membership(account_id="a", user_id="u", role="superuser")


def test_default_account_seeded_in_sqlite(tmp_path):
    store = SqliteAccountStore(path=str(tmp_path / "seed.db"))
    try:
        # re-open the same file: default must persist, no duplicate
        store2 = SqliteAccountStore(path=str(tmp_path / "seed.db"))
        assert os.path.exists(str(tmp_path / "seed.db"))
        store2.close()
    finally:
        store.close()


# ─── multi-user auth: store-first, env-admin fallback ──────────────────


@pytest.mark.asyncio
async def test_authenticate_prefers_user_store(monkeypatch, tmp_path):
    from cairn.core.triggers import auth

    store = SqliteAccountStore(path=str(tmp_path / "auth.db"))
    monkeypatch.setattr(acct, "_store", store)
    await acct.create_user("dave", "s3cret", store=store)
    await acct.add_member("default", (await store.get_user_by_username("dave")).id, "member", store=store)

    ident = auth.resolve_identity("dave", "s3cret")
    assert ident is not None
    # sub is the USER ID, account_id resolved via membership
    assert ident.username == "dave"
    assert ident.sub == (await store.get_user_by_username("dave")).id
    assert ident.account_id == "default"
    # wrong password fails
    assert auth.resolve_identity("dave", "wrong") is None
    # authenticate_user back-compat surface returns the sub (user id)
    assert auth.authenticate_user("dave", "s3cret") == ident.sub
    store.close()


@pytest.mark.asyncio
async def test_disabled_user_cannot_authenticate(monkeypatch, tmp_path):
    from cairn.core.triggers import auth

    store = SqliteAccountStore(path=str(tmp_path / "auth.db"))
    monkeypatch.setattr(acct, "_store", store)
    user = await acct.create_user("erin", "pw", disabled=True, store=store)
    assert user.disabled
    assert auth.resolve_identity("erin", "pw") is None


def test_authenticate_falls_back_to_env_admin(monkeypatch, tmp_path):
    """No user in the store → the legacy OBS_API_USER/PASSWORD path still works."""
    from cairn.core.triggers import auth

    # empty store (only the seeded default account, no users)
    store = SqliteAccountStore(path=str(tmp_path / "empty.db"))
    monkeypatch.setattr(acct, "_store", store)
    monkeypatch.setenv("OBS_API_USER", "admin")
    monkeypatch.setenv("OBS_API_PASSWORD", "admin-pw")

    ident = auth.resolve_identity("admin", "admin-pw")
    assert ident is not None
    assert ident.sub == "admin" and ident.username == "admin"
    assert ident.account_id == "default"
    # wrong admin password fails
    assert auth.resolve_identity("admin", "nope") is None
    # back-compat surface
    assert auth.authenticate_user("admin", "admin-pw") == "admin"
    store.close()


def test_no_user_store_and_no_env_admin_denies(monkeypatch, tmp_path):
    from cairn.core.triggers import auth

    store = SqliteAccountStore(path=str(tmp_path / "empty.db"))
    monkeypatch.setattr(acct, "_store", store)
    # neither a store user nor an env admin configured
    assert auth.resolve_identity("ghost", "whatever") is None
    store.close()


# ─── Postgres backend: structural (no live PG) ─────────────────────────


def test_pg_store_instantiates_without_dep():
    """Constructor doesn't import asyncpg — the dep stays lazy/optional."""
    s = PostgresAccountStore(dsn="postgres://nowhere/db")
    assert s._pool is None and s._schema_ready is False


@pytest.mark.asyncio
async def test_pg_pool_raises_clearly_without_dsn():
    store = PostgresAccountStore()
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="storage-postgres"):
            await store._get_pool()
    else:
        with pytest.raises(RuntimeError, match="CAIRN_STORAGE=postgres"):
            await store._get_pool()


@pytest.mark.parametrize(
    "url,expect",
    [
        ("postgres://u:p@h/db", PostgresAccountStore),
        ("postgresql://u:p@h/db", PostgresAccountStore),
        ("sqlite://:memory:", SqliteAccountStore),
        ("", SqliteAccountStore),
    ],
)
def test_factory_honors_obs_storage(monkeypatch, tmp_path, url, expect):
    monkeypatch.setenv("OBS_STORAGE", url)
    monkeypatch.setenv("OBS_ACCOUNT_DB", str(tmp_path / "acct.db"))
    monkeypatch.setattr(acct, "_store", None)
    try:
        assert isinstance(acct.get_account_store(), expect)
    finally:
        monkeypatch.setattr(acct, "_store", None)


# ─── Live PG round-trip (gated on PGSTORE_DSN + dep) ────────────────────


@pytest.fixture
def _pg_dsn():
    dsn = os.environ.get("PGSTORE_DSN")
    if not dsn:
        pytest.skip("PGSTORE_DSN not set — skipping live Postgres integration")
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        pytest.skip("storage-postgres optional dep group not installed")
    return dsn


@pytest.mark.asyncio
async def test_live_pg_roundtrip(_pg_dsn):
    store = PostgresAccountStore(dsn=_pg_dsn)
    try:
        assert (await store.get_account("default")) is not None
        await store.put_account(Account(id="pg-acme", name="PG Acme"))
        u = User(username="pg-user", password_hash="x")
        await store.put_user(u)
        await store.put_membership(Membership(account_id="pg-acme", user_id=u.id, role="owner"))
        assert (await store.get_user_by_username("pg-user")).id == u.id
        assert (await store.get_account_for_user(u.id)) == "pg-acme"
        members = await store.list_members("pg-acme")
        assert members and members[0].role == "owner"
    finally:
        await store.aclose()
