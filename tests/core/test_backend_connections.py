"""#336 — profile-bound connection creds resolve into the env the store
factories read (the runtime half of #305). Hermetic: in-memory connection
store; secret values come via env refs (ADR-0033's fallback path)."""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

import cairn.core.backend_connections as bc
from cairn.core.connections import Connection, InMemoryConnectionStore
from cairn.core.operator.secrets import reset_secret_provider


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # reset_applied() drops the per-process injection map + the get_settings
    # lru_cache snapshot (#359). We also reset the secret-provider cache: these
    # tests resolve creds via the env-fallback path (ADR-0033), but
    # resolve_connection() consults the process-global get_secret_provider()
    # cache FIRST — an earlier operator invocation elsewhere in the suite can
    # leave it warm with a bound provider that shadows the env value, making
    # this test order-dependent. Clearing it keeps the fallback path honest.
    bc.reset_applied()
    reset_secret_provider()
    for var in ("QDRANT_URL", "QDRANT_API_KEY", "PGVECTOR_DSN", "RAG_VECTOR_STORE"):
        monkeypatch.delenv(var, raising=False)
    yield
    bc.reset_applied()
    reset_secret_provider()


def _profile(tmp_path, monkeypatch, body: str) -> None:
    p = tmp_path / "env.toml"
    p.write_text(body, encoding="utf-8")
    monkeypatch.setenv("OBS_PROFILE", str(p))


async def _store_with_qdrant() -> InMemoryConnectionStore:
    store = InMemoryConnectionStore()
    await store.create(Connection(
        name="prod-qdrant", kind="api_key",
        config={"url": "https://qdrant.internal:6333"},
        secrets={"API_KEY": "TEST_QDRANT_KEY_REF"},   # ref, resolved via env below
    ))
    return store


async def test_bound_qdrant_connection_injects_env(tmp_path, monkeypatch):
    _profile(tmp_path, monkeypatch,
             '[profile.backends]\nrag_vector_store = "qdrant"\nrag_vector_store_connection = "prod-qdrant"\n')
    monkeypatch.setenv("TEST_QDRANT_KEY_REF", "sk-qdrant-secret")
    store = await _store_with_qdrant()
    monkeypatch.setattr("cairn.core.connections.default_connection_store", lambda: store)

    name = await bc.apply_backend_connection("rag_vector_store")
    assert name == "prod-qdrant"
    # the env names Settings/build_vector_store already read:
    assert os.environ["QDRANT_URL"] == "https://qdrant.internal:6333"
    assert os.environ["QDRANT_API_KEY"] == "sk-qdrant-secret"
    # …and Settings picks them up (rebuilt per call)
    from cairn.core.provider_settings import get_settings
    assert get_settings().qdrant_url == "https://qdrant.internal:6333"


async def test_injection_visible_through_cached_settings(tmp_path, monkeypatch):
    """#359 — get_settings() is lru-cached; a caller BEFORE the injection froze
    a stale snapshot. apply_backend_connection must clear the cache so
    get_settings() consumers (rag stores, query, ingest, memory) see the
    injected creds. This is the warm-engine path — the other tests only cover
    a cold cache."""
    from cairn.core.provider_settings import get_settings

    stale = get_settings().qdrant_url                  # warm the cache pre-injection
    assert stale != "https://qdrant.internal:6333"

    _profile(tmp_path, monkeypatch,
             '[profile.backends]\nrag_vector_store = "qdrant"\nrag_vector_store_connection = "prod-qdrant"\n')
    monkeypatch.setenv("TEST_QDRANT_KEY_REF", "sk-qdrant-secret")
    store = await _store_with_qdrant()
    monkeypatch.setattr("cairn.core.connections.default_connection_store", lambda: store)

    assert await bc.apply_backend_connection("rag_vector_store") == "prod-qdrant"
    assert get_settings().qdrant_url == "https://qdrant.internal:6333"


async def test_idempotent_per_process(tmp_path, monkeypatch):
    _profile(tmp_path, monkeypatch,
             '[profile.backends]\nrag_vector_store = "qdrant"\nrag_vector_store_connection = "prod-qdrant"\n')
    monkeypatch.setenv("TEST_QDRANT_KEY_REF", "v1")
    store = await _store_with_qdrant()
    monkeypatch.setattr("cairn.core.connections.default_connection_store", lambda: store)

    assert await bc.apply_backend_connection("rag_vector_store") == "prod-qdrant"
    monkeypatch.setenv("TEST_QDRANT_KEY_REF", "v2")   # would change the value…
    assert await bc.apply_backend_connection("rag_vector_store") == "prod-qdrant"
    assert os.environ["QDRANT_API_KEY"] == "v1"        # …but same binding → no re-resolve


async def test_no_binding_is_a_noop(tmp_path, monkeypatch):
    _profile(tmp_path, monkeypatch, '[profile.backends]\nrag_vector_store = "qdrant"\n')
    assert await bc.apply_backend_connection("rag_vector_store") is None
    assert "QDRANT_URL" not in os.environ


async def test_unknown_connection_falls_back(tmp_path, monkeypatch):
    _profile(tmp_path, monkeypatch,
             '[profile.backends]\nrag_vector_store = "qdrant"\nrag_vector_store_connection = "ghost"\n')
    monkeypatch.setattr("cairn.core.connections.default_connection_store", lambda: InMemoryConnectionStore())
    assert await bc.apply_backend_connection("rag_vector_store") is None
    assert "QDRANT_URL" not in os.environ


async def test_backend_without_injection_map_ignored(tmp_path, monkeypatch):
    # sqlite_vec is local — a bound connection makes no sense; ignored, no crash
    _profile(tmp_path, monkeypatch,
             '[profile.backends]\nrag_vector_store = "sqlite_vec"\nrag_vector_store_connection = "prod-qdrant"\n')
    assert await bc.apply_backend_connection("rag_vector_store") is None
