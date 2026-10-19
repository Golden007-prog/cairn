"""#76 — sqlite-vec is the DEFAULT vector store; qdrant is a provider.

Core must not default to (or hard-import) qdrant. sqlite_vec is the in-built
dev default, pgvector the in-built prod backend; an unknown/unavailable backend
falls back to sqlite_vec. Memory follows the same default.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from cairn.services.rag.stores import build_vector_store


@pytest.fixture(autouse=True)
def _no_vector_store_override(monkeypatch):
    """Per test, never at import: a module-level pop undoes the suite's storage-env
    neutraliser for the whole session, which conftest refuses (#2361). The settings are
    cached, so a test that read them first must not leak its value in."""
    from cairn.core.provider_settings import get_settings

    monkeypatch.delenv("RAG_VECTOR_STORE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_builder_default_and_fallback_are_sqlite_vec():
    # no name → sqlite_vec; unknown/uninstalled provider → sqlite_vec (not qdrant)
    assert type(build_vector_store("")).__name__ == "SqliteVecStore"
    assert type(build_vector_store("weaviate")).__name__ == "SqliteVecStore"
    # the in-built names still resolve to their classes (lazy; not constructed-connected)
    assert type(build_vector_store("sqlite_vec")).__name__ == "SqliteVecStore"


def test_shipped_settings_default_is_sqlite_vec_not_qdrant(monkeypatch):
    # at TEST time, not import time: the suite's conftest neutralises the var to
    # "" and pydantic-settings takes "" literally (CI saw '' == 'sqlite_vec').
    monkeypatch.delenv("RAG_VECTOR_STORE", raising=False)
    from cairn.core.provider_settings import get_settings
    assert get_settings().rag_vector_store == "sqlite_vec"


def test_memory_store_default_is_sqlite_vec():
    from cairn.services.memory.store import _get_store, reset_memory_store
    reset_memory_store()
    assert type(_get_store()).__name__ == "SqliteVecStore"


def test_external_providers_resolve_via_entry_point():
    # qdrant/pinecone are NOT in core (cairn/services/rag/stores ships only
    # sqlite_vec + pgvector). They load from the `cairn.vector_stores`
    # entry-point group, lazily — constructing the class needs no qdrant-client.
    assert type(build_vector_store("qdrant")).__name__ == "QdrantStore"
    assert type(build_vector_store("pinecone")).__name__ == "PineconeStore"
