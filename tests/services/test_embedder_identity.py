"""Embedder-scoped collections (#512).

A corpus can only be searched with the embedder that indexed it. The collection
name carries the active embedder's fingerprint, so a profile that changes the
embedder resolves to a DIFFERENT collection — search finds it empty (fail-closed)
instead of comparing the query against vectors from a different embedding space
(silent garbage).
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.provider_settings import get_settings
from cairn.services.rag.embeddings import embedder_fingerprint, rag_collection


@pytest.fixture(autouse=True)
def _fresh_settings():
    # provider_settings is cached; drop it so monkeypatched env is seen.
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_collection_carries_embedder_fingerprint():
    c = rag_collection("attack")
    assert c.startswith("rag_attack__")
    assert c.endswith(embedder_fingerprint())


def test_byo_name_with_hyphen_is_sanitized():
    """BYO corpus names are user-supplied (e.g. ``standard-readme``); a store
    collection must be ``[A-Za-z0-9_]`` only. Without slugifying, indexing wrote
    to ``rag_standard_readme__…`` while search looked up the invalid
    ``rag_standard-readme__…`` and always got zero hits (the hyphen bug)."""
    c = rag_collection("standard-readme")
    body = c.rsplit("__", 1)[0]  # strip the embedder fingerprint
    assert body == "rag_standard_readme"
    assert body.replace("_", "").isalnum()  # store-safe: no hyphen survives


def test_dirty_names_map_to_the_same_collection():
    """Every non-alphanumeric run collapses to ``_``, so index and search — which
    both call rag_collection with the raw name — derive the SAME string."""
    assert rag_collection("standard-readme") == rag_collection("standard/readme")
    assert rag_collection("a b.c-d") == rag_collection("a_b_c_d")


def test_changing_the_model_changes_the_collection(monkeypatch):
    monkeypatch.setenv("RAG_EMBED_PROVIDER", "openai")
    monkeypatch.setenv("RAG_EMBED_MODEL", "text-embedding-3-small")
    get_settings.cache_clear()
    a = rag_collection("attack")

    # SAME dim, DIFFERENT model — the dangerous silent-garbage case pre-#512.
    monkeypatch.setenv("RAG_EMBED_MODEL", "text-embedding-ada-002")
    get_settings.cache_clear()
    b = rag_collection("attack")

    assert a != b, "a different embedder model must map to a different collection"


def test_changing_the_provider_changes_the_collection(monkeypatch):
    monkeypatch.setenv("RAG_EMBED_PROVIDER", "openai")
    get_settings.cache_clear()
    openai = rag_collection("attack")
    monkeypatch.setenv("RAG_EMBED_PROVIDER", "gemini")
    get_settings.cache_clear()
    gemini = rag_collection("attack")
    assert openai != gemini


def test_ingest_and_search_derive_the_same_collection():
    """discover_corpora (ingest) and the rag/search operator MUST agree on the
    collection — both go through rag_collection, so a corpus indexed by one is
    found by the other under the same embedder."""
    import types

    from cairn.services.rag.lifecycle import discover_corpora

    pack = types.SimpleNamespace(
        name="security-kb", rag_dir="/fake/rag",
        manifest=types.SimpleNamespace(required_rag={
            "attack": types.SimpleNamespace(corpus="rag/attack", freshness="on_change", role="security"),
        }),
    )
    [t] = discover_corpora([pack])
    assert t.collection == rag_collection("attack")  # the operator's _store_for uses the same


# ─── doctor: embedder-alignment check (#512) ───────────────────────────


def _kb_pack():
    import types
    return types.SimpleNamespace(
        name="security-kb", rag_dir="/fake/rag",
        manifest=types.SimpleNamespace(required_rag={
            "attack": types.SimpleNamespace(corpus="rag/attack", freshness="on_change", role="security"),
        }),
    )


@pytest.mark.asyncio
async def test_doctor_flags_corpus_built_under_a_different_embedder():
    from cairn.core.storage.rag_catalog import CatalogEntry, InMemoryRagCatalog
    from cairn.services.rag.lifecycle import check_embedder_alignment

    cat = InMemoryRagCatalog()
    # indexed under an OLD build fingerprint (e.g. before the embedder changed)
    await cat.upsert(CatalogEntry(document_id="d1", corpus="security-kb/attack",
                                  source="T1078.md", build_fingerprint="OLD_FP",
                                  status="indexed"))
    warn = await check_embedder_alignment([_kb_pack()], catalog=cat)
    assert warn and "security-kb/attack" in warn and "re-index" in warn


@pytest.mark.asyncio
async def test_doctor_ok_when_built_under_active_embedder():
    from cairn.core.storage.rag_catalog import CatalogEntry, InMemoryRagCatalog
    from cairn.services.rag.lifecycle import check_embedder_alignment
    from cairn.services.rag.plan import resolved_build_fingerprint

    cat = InMemoryRagCatalog()
    await cat.upsert(CatalogEntry(document_id="d1", corpus="security-kb/attack",
                                  source="T1078.md",
                                  build_fingerprint=resolved_build_fingerprint(),
                                  status="indexed"))
    assert await check_embedder_alignment([_kb_pack()], catalog=cat) is None
