"""Typed infra connections — model + store + resolver (#68/#70).

Covers: id slugging + validation + secret-key exposure (never values); CRUD
round-trip on both InMemory and Sqlite stores; and resolve_connection() pulling
secret refs from the SecretStore/env while masking values in repr.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.connections import (
    Connection,
    InMemoryConnectionStore,
    PROVIDER_CATALOG,
    ResolvedConnection,
    SqliteConnectionStore,
    connection_from_provider,
    default_connection_store,
    resolve_connection,
    resolve_provider_key,
    slugify,
)


def test_model_id_slug_and_secret_keys():
    c = Connection(
        name="Prod OpenAI",
        kind="api_key",
        config={"base_url": "https://api.openai.com"},
        secrets={"API_KEY": "OPENAI_API_KEY"},
    )
    assert c.id == "prod-openai"                     # slugged from name
    assert c.secret_keys == ["API_KEY"]              # keys only, not refs/values
    # the stored secret is a REFERENCE, never a raw value
    assert c.secrets["API_KEY"] == "OPENAI_API_KEY"


def test_model_rejects_empty_name_and_kind():
    with pytest.raises(Exception):
        Connection(name="  ", kind="mcp")
    with pytest.raises(Exception):
        Connection(name="x", kind="")


def test_slugify():
    assert slugify("My Cluster #1") == "my-cluster-1"
    assert slugify("a/b c") == "a-b-c"


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        s = InMemoryConnectionStore()
    else:
        s = SqliteConnectionStore(str(tmp_path / "conns.db"))
    yield s
    s.close()


@pytest.mark.asyncio
async def test_store_crud_round_trip(store):
    c = Connection(name="runpod-a100", kind="compute.runpod",
                   config={"gpu": "A100"}, secrets={"TOKEN": "RUNPOD_TOKEN"})
    await store.create(c)

    assert (await store.get("runpod-a100")).config["gpu"] == "A100"
    assert (await store.get_by_name("runpod-a100")).kind == "compute.runpod"
    assert [x.name for x in await store.list()] == ["runpod-a100"]

    updated = await store.update("runpod-a100", enabled=False)
    assert updated.enabled is False and updated.updated_at is not None

    assert await store.delete("runpod-a100") is True
    assert await store.get("runpod-a100") is None
    assert await store.delete("runpod-a100") is False


@pytest.mark.asyncio
async def test_resolver_resolves_refs_and_masks_values(monkeypatch):
    """resolve_connection pulls secret refs from env (no SecretStore bound) and
    the resolved object never echoes the secret value in its repr."""
    monkeypatch.delenv("OBS_PROFILE", raising=False)   # no secrets.store binding
    monkeypatch.setenv("MY_RUNPOD_TOKEN", "rp-SECRET-xyz")
    store = InMemoryConnectionStore()
    await store.create(Connection(
        name="rp", kind="compute.runpod", secrets={"TOKEN": "MY_RUNPOD_TOKEN"},
    ))

    resolved = await resolve_connection("rp", store=store)
    assert isinstance(resolved, ResolvedConnection)
    assert resolved.secrets["TOKEN"] == "rp-SECRET-xyz"   # resolved at the boundary
    assert "rp-SECRET-xyz" not in repr(resolved)          # but masked in repr
    assert "••••••" in repr(resolved)


@pytest.mark.asyncio
async def test_resolver_skips_disabled_and_unknown(monkeypatch):
    store = InMemoryConnectionStore()
    await store.create(Connection(name="off", kind="mcp", enabled=False))
    assert await resolve_connection("off", store=store) is None
    assert await resolve_connection("missing", store=store) is None


# ── provider catalog (cairn connect substrate, #68/#98) ────────────────


def test_provider_key_resolution_and_aliases():
    assert resolve_provider_key("openai") == "openai"
    assert resolve_provider_key("OpenAI") == "openai"       # case-insensitive
    assert resolve_provider_key("hf") == "huggingface"      # alias
    assert resolve_provider_key("claude") == "anthropic"    # alias
    assert resolve_provider_key("k8s") == "kube"            # alias
    assert resolve_provider_key("nope") is None             # unknown


def test_connection_from_provider_uses_catalog_defaults():
    c = connection_from_provider("openai")
    assert c.name == "openai"
    assert c.kind == "api_key"
    # the default secret ref is the conventional env var — a REFERENCE, not a value
    assert c.secrets == {"API_KEY": "OPENAI_API_KEY"}


def test_connection_from_provider_overrides_and_alias():
    c = connection_from_provider(
        "claude",                                  # alias → anthropic
        name="claude-prod",
        config={"region": "us"},
        secrets={"API_KEY": "MY_ANTHROPIC_KEY"},   # override the default ref
        enabled=False,
    )
    assert c.name == "claude-prod"
    assert c.kind == "api_key"
    assert c.secrets == {"API_KEY": "MY_ANTHROPIC_KEY"}
    # override layers on top of the catalog default (anthropic now carries a
    # default base_url so its authenticated probe has a host to hit).
    assert c.config["region"] == "us"
    assert c.config["base_url"] == "https://api.anthropic.com"
    assert c.enabled is False


def test_connection_from_provider_unknown_raises():
    with pytest.raises(KeyError):
        connection_from_provider("not-a-provider")


def test_compute_provider_has_no_secret_refs():
    c = connection_from_provider("docker", config={"host": "tcp://h:2375"})
    assert c.kind == "compute.docker"
    assert c.secret_keys == []                     # compute target, no secrets
    assert c.config["host"] == "tcp://h:2375"


def test_catalog_entries_are_self_consistent():
    from cairn.core.connections import AUTH_TYPES

    for key, spec in PROVIDER_CATALOG.items():
        assert spec.provider == key                # keyed by its own name
        assert spec.kind                           # has a kind
        assert spec.auth_type in AUTH_TYPES        # every entry declares an auth_type
        # secret values are refs (uppercase-ish env names), not obvious literals
        for ref in spec.secrets.values():
            assert ref and ref == ref.strip()


def test_default_store_durable_by_default(monkeypatch, tmp_path):
    # nothing set → durable sqlite (so CLI/admin/resolve agree on location).
    # OBS_STORAGE=postgres:// now routes to Postgres (#427), so this "nothing set"
    # case must clear it explicitly.
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    monkeypatch.setenv("OBS_CONNECTIONS_DB", str(tmp_path / "c.db"))
    s = default_connection_store()
    assert isinstance(s, SqliteConnectionStore)
    s.close()
    # explicit :memory: → volatile in-memory store
    monkeypatch.setenv("OBS_CONNECTIONS_DB", ":memory:")
    assert isinstance(default_connection_store(), InMemoryConnectionStore)
