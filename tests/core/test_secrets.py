"""Tests for SecretStore resolution + per-operation scoping (ADR-0033)."""
from __future__ import annotations

import pytest

from cairn.core.operator.loader import ensure_loaded
from cairn.core.operator.protocol import NullSecretStore
from cairn.core.operator.secrets import (
    ScopedSecretStore,
    build_scoped_secrets,
    get_secret_provider,
    reset_secret_provider,
)


class _Backend:
    def get(self, name: str) -> str:
        return {"A": "secret-a", "B": "secret-b"}[name]

    def available_keys(self) -> list[str]:
        return ["A", "B"]


def _bind(monkeypatch, bindings: dict) -> None:
    class _P:
        pass

    _P.bindings = bindings
    monkeypatch.setattr(
        "cairn.core.pack.profile.load_profile_from_env", lambda: _P()
    )


def test_scoped_allows_only_declared_keys():
    s = ScopedSecretStore(_Backend(), ["A"])
    assert s.get("A") == "secret-a"
    assert s.available_keys() == ["A"]
    with pytest.raises(KeyError):
        s.get("B")  # present in backend but NOT declared → blocked before backend


def test_no_declared_secrets_is_null_store():
    s = build_scoped_secrets([])
    assert isinstance(s, NullSecretStore)
    with pytest.raises(KeyError):
        s.get("anything")


def test_declared_but_unbound_raises(monkeypatch):
    reset_secret_provider()
    _bind(monkeypatch, {})  # no secrets.store bound
    s = build_scoped_secrets(["HF_TOKEN"])
    with pytest.raises(KeyError):  # fail-loud: declared a secret, bound no store
        s.get("HF_TOKEN")
    reset_secret_provider()


def test_env_provider_end_to_end(monkeypatch):
    reset_secret_provider()
    ensure_loaded()  # register secrets/env
    monkeypatch.setenv("DEMO_SECRET", "xyz")
    _bind(monkeypatch, {"secrets.store": "secrets/env"})
    s = build_scoped_secrets(["DEMO_SECRET"])
    assert s.get("DEMO_SECRET") == "xyz"
    with pytest.raises(KeyError):
        s.get("OTHER")  # undeclared
    reset_secret_provider()


def test_get_secret_provider_unbound_is_none(monkeypatch):
    reset_secret_provider()
    _bind(monkeypatch, {})
    assert get_secret_provider() is None
    reset_secret_provider()
