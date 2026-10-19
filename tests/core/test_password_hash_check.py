"""OBS_API_PASSWORD_HASH must be usable, and an unusable one must say so (#1853).

Prod 2026-09-01: compose interpolated the `$` segments out of the argon2 hash
(`$argon2id$v=19$m=65536,…` → `=19=65536,…`). pwdlib raised UnknownHashError,
the login path swallowed it as "incorrect username or password", and the
password — which was correct — got reset. These pin the three places the real
cause now surfaces: the reusable check, the doctor, and the login path's log.
"""
from __future__ import annotations

import logging

import pytest

from cairn.core.triggers import auth

GOOD = '$argon2id$v=19$m=65536,t=3,p=4$hdL7wUwAlItMM+v3pVfHxA$R1VstcN4/3bgfIjAOEmkCALYtjAqE4bjysDdB7zSit8'                      # argon2 of "s3cret"
CORRUPT = "=19=65536,t=3,p=4/8HHdinpyNtId"   # what prod actually received


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("OBS_API_USER", "OBS_API_PASSWORD", "OBS_API_PASSWORD_HASH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(auth, "_HASH_PROBLEM_LOGGED", False)


def test_unset_is_not_a_problem():
    assert auth.password_hash_problem() is None


def test_a_real_hash_is_not_a_problem(monkeypatch):
    monkeypatch.setenv("OBS_API_PASSWORD_HASH", GOOD)
    assert auth.password_hash_problem() is None


@pytest.mark.parametrize("bad", [CORRUPT, GOOD.replace("$", "$$"), "not-a-hash"])
def test_an_unparseable_hash_is_named_with_the_fix(monkeypatch, bad):
    monkeypatch.setenv("OBS_API_PASSWORD_HASH", bad)
    msg = auth.password_hash_problem()
    assert msg and "CAIRN_API_PASSWORD_HASH" in msg and "$$" in msg and "#1853" in msg


def test_login_with_a_corrupted_hash_fails_closed_but_logs_the_real_cause(monkeypatch, caplog):
    """Before: `except Exception: ok_pass = False` — indistinguishable from a
    wrong password. Still fails closed; now the log names the deployment fault."""
    monkeypatch.setenv("OBS_API_USER", "admin")
    monkeypatch.setenv("OBS_API_PASSWORD_HASH", CORRUPT)
    with caplog.at_level(logging.ERROR):
        assert auth._verify_env_admin("admin", "s3cret", auth._password_hash()) is None
    assert any("#1853" in r.getMessage() for r in caplog.records), caplog.text


def test_the_cause_is_logged_once_not_per_attempt(monkeypatch, caplog):
    """A brute-force loop against a broken deploy must not turn the ERROR log
    into a flood — the cause is stated once per process."""
    monkeypatch.setenv("OBS_API_USER", "admin")
    monkeypatch.setenv("OBS_API_PASSWORD_HASH", CORRUPT)
    ph = auth._password_hash()
    with caplog.at_level(logging.ERROR):
        for _ in range(5):
            auth._verify_env_admin("admin", "x", ph)
    assert sum("#1853" in r.getMessage() for r in caplog.records) == 1


def test_a_real_hash_still_verifies_the_right_password(monkeypatch):
    """Regression guard: the login path change must not touch the happy path."""
    monkeypatch.setenv("OBS_API_USER", "admin")
    monkeypatch.setenv("OBS_API_PASSWORD_HASH", GOOD)
    ph = auth._password_hash()
    assert auth._verify_env_admin("admin", "s3cret", ph) == "admin"
    assert auth._verify_env_admin("admin", "wrong", ph) is None
