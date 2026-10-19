"""The account comes from MEMBERSHIP, not from the token (#1611, ADR-0094 D2).

`caller_identity` used to read `account_id` straight off the JWT claim, so the
tenant a request acted in was decided at LOGIN and frozen until the token
expired. Two consequences: moving or removing a member was unenforceable
without invalidating their session, and multi-account later would have been a
token-format migration.

Membership is authoritative now. The claim stays in the token as a hint and as
the fallback the env-admin path and a store outage rely on.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core import account as A
from cairn.core.triggers.auth import caller_identity, create_access_token


@pytest.fixture(autouse=True)
def _isolated_accounts(tmp_path, monkeypatch):
    """Own account store per test — otherwise these write into the developer's
    real ~/.cairn store (the lesson from #1605).

    `OBS_JWT_SECRET` is set through monkeypatch, NOT at module import: pytest
    loads every test module into ONE process, so a module-level
    `os.environ.setdefault` here switches auth ON for the whole session and
    401s unrelated suites that legitimately run keyless. (Caught doing exactly
    that — four other files failed until this moved into the fixture.)
    """
    monkeypatch.setenv("OBS_JWT_SECRET", "test-secret-for-account-resolution")
    monkeypatch.setattr(A, "_store", A.SqliteAccountStore(path=str(tmp_path / "acct.db")))
    yield


def _token(sub: str, account_id: str) -> str:
    return create_access_token({"sub": sub, "account_id": account_id})


async def _user_in(account_id: str, username: str = "alice"):
    store = A.get_account_store()
    await store.put_account(A.Account(id=account_id, name=account_id))
    u = await store.put_user(A.User(username=username, password_hash="x"))
    await A.add_member(account_id, u.id, "member", store=store)
    return u


async def test_membership_wins_over_a_stale_token_claim():
    """THE point. A token minted while the user was in `old-co` must not keep
    acting in `old-co` after they are moved."""
    u = await _user_in("new-co")
    token = _token(u.id, "old-co")      # stale claim
    ident = await caller_identity(token)
    assert ident["account_id"] == "new-co", "the claim decided the tenant"


async def test_a_role_change_takes_effect_on_the_next_request():
    """No re-login, no token re-issue. `put_membership` is an idempotent upsert
    on (account, user), so promoting someone is a row update — and the SAME
    token must reflect it immediately, or a demotion is unenforceable until the
    token happens to expire."""
    u = await _user_in("acme")                      # joins as `member`
    token = _token(u.id, "acme")
    assert (await caller_identity(token))["is_admin"] is False

    await A.add_member("acme", u.id, "owner")       # promote — same token
    ident = await caller_identity(token)
    assert ident["role"] == "owner" and ident["is_admin"] is True


async def test_the_role_still_comes_from_the_resolved_account():
    store = A.get_account_store()
    await store.put_account(A.Account(id="acme", name="acme"))
    u = await store.put_user(A.User(username="boss", password_hash="x"))
    await A.add_member("acme", u.id, "owner", store=store)
    ident = await caller_identity(_token(u.id, "acme"))
    assert ident["role"] == "owner" and ident["is_admin"] is True


async def test_a_user_with_no_membership_falls_back_to_the_claim():
    """Back-compat: a user record with no membership row must not lose its
    tenant — it keeps whatever the token said."""
    store = A.get_account_store()
    u = await store.put_user(A.User(username="orphan", password_hash="x"))
    ident = await caller_identity(_token(u.id, "legacy-acct"))
    assert ident["account_id"] == "legacy-acct"


async def test_auth_off_is_unchanged(monkeypatch):
    """Single-node OSS has no tenant boundary and must keep working."""
    monkeypatch.delenv("OBS_JWT_SECRET", raising=False)
    ident = await caller_identity(None)
    assert ident["auth_enabled"] is False
    assert ident["account_id"] == "default" and ident["is_admin"] is True
