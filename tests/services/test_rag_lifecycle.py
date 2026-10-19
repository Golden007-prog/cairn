"""RAG indexing lifecycle + scheduler — drive surface (ADR-0023 slice 2)."""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

import cairn.services.rag.lifecycle as L
import cairn.services.rag.schedule as S
from cairn.services.rag.indexer import IndexResult
from cairn.services.rag.lifecycle import CorpusTarget
from cairn.services.rag.schedule import freshness_to_cron


def _pack(name, rag_dir, required):
    """Fake Pack: rag_dir + manifest.required_rag dict of corpus reqs."""
    req = {k: SimpleNamespace(corpus=c, freshness=f) for k, (c, f) in required.items()}
    return SimpleNamespace(name=name, rag_dir=rag_dir, manifest=SimpleNamespace(required_rag=req))


# ── discovery ──────────────────────────────────────────────────────────

def test_discover_resolves_path_collection_and_qualifies_id():
    pack = _pack("incident", "/pk/incident/rag", {
        "runbooks": ("rag/runbooks", "on_change"),
        "service_docs": ("rag/service-docs", "daily"),
    })
    targets = {t.name: t for t in L.discover_corpora([pack])}
    assert targets["runbooks"].corpus_id == "incident/runbooks"
    # #512 — the collection is embedder-scoped (so a changed embedder can't
    # silently cross-wire); same helper the rag/search operator uses.
    from cairn.services.rag.embeddings import rag_collection
    assert targets["runbooks"].collection == rag_collection("runbooks")
    assert targets["runbooks"].collection.startswith("rag_runbooks__")
    assert targets["runbooks"].root == "/pk/incident/rag/runbooks"
    assert targets["service_docs"].freshness == "daily"


def test_discover_skips_pack_without_rag_dir():
    pack = _pack("p", None, {"x": ("rag/x", "daily")})
    assert L.discover_corpora([pack]) == []


def test_discover_skips_pack_without_required_rag():
    pack = SimpleNamespace(name="p", rag_dir="/p/rag", manifest=SimpleNamespace(required_rag={}))
    assert L.discover_corpora([pack]) == []


# ── index_all ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_index_all_drives_index_corpus_per_target(monkeypatch):
    pack = _pack("incident", "/pk/incident/rag", {
        "runbooks": ("rag/runbooks", "on_change"),
        "service_docs": ("rag/service-docs", "daily"),
    })
    calls = []

    async def fake_index(corpus_id, root, *, catalog=None, collection=None):
        calls.append((corpus_id, root, collection))
        return IndexResult(corpus=corpus_id, status="completed")

    # index_all imports index_corpus lazily from the indexer module (#39), so
    # patch it at the source, not on the lifecycle module.
    monkeypatch.setattr("cairn.services.rag.indexer.index_corpus", fake_index)
    results = await L.index_all([pack])
    assert len(results) == 2
    from cairn.services.rag.embeddings import rag_collection
    assert ("incident/runbooks", "/pk/incident/rag/runbooks", rag_collection("runbooks")) in calls


# ── opt-in gate ────────────────────────────────────────────────────────

@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("", False), ("0", False), ("false", False),
])
def test_autoindex_enabled(monkeypatch, val, expected):
    monkeypatch.setenv("OBS_RAG_AUTOINDEX", val)
    assert L.autoindex_enabled() is expected


def test_autoindex_default_off(monkeypatch):
    monkeypatch.delenv("OBS_RAG_AUTOINDEX", raising=False)
    assert L.autoindex_enabled() is False


# ── freshness → cron ───────────────────────────────────────────────────

def test_freshness_mapping():
    assert freshness_to_cron("on_change") is None
    assert freshness_to_cron("manual") is None
    assert freshness_to_cron(None) is None
    assert freshness_to_cron("hourly") == "0 * * * *"
    assert freshness_to_cron("daily") == "0 3 * * *"
    assert freshness_to_cron("*/15 * * * *") == "*/15 * * * *"   # raw cron passthrough
    assert freshness_to_cron("nonsense") is None


# ── scheduler ──────────────────────────────────────────────────────────

def test_scheduler_registers_only_periodic_corpora(monkeypatch):
    targets = [
        CorpusTarget("p", "runbooks", "p/runbooks", "/r", "rag_runbooks", "on_change"),
        CorpusTarget("p", "docs", "p/docs", "/d", "rag_docs", "daily"),
        CorpusTarget("p", "raw", "p/raw", "/x", "rag_raw", "*/30 * * * *"),
    ]
    monkeypatch.setattr(S, "discover_corpora", lambda packs=None: targets)
    sched = S.RagIndexScheduler()
    n = sched.register_all()
    assert n == 2  # daily + raw-cron; on_change gets no job
    job_corpora = {j["corpus_id"] for j in sched.list_jobs()}
    assert job_corpora == {"p/docs", "p/raw"}


# ── backend alignment guard ────────────────────────────────────────────

@pytest.fixture
def _fresh_settings(monkeypatch):
    """check_backend_alignment reads get_settings() (lru-cached) — clear it so
    the monkeypatched env is actually seen, and again after so we don't leak."""
    from cairn.core.provider_settings import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_alignment_warns_on_divergent_backend(monkeypatch, _fresh_settings):
    monkeypatch.setenv("RAG_VECTOR_STORE", "qdrant")
    # No rag.search binding resolved → treated as local provider; qdrant
    # diverges from sqlite-vec → warns.
    msg = L.check_backend_alignment()
    assert msg and "diverge" in msg


def test_alignment_silent_when_sqlite_vec(monkeypatch, _fresh_settings):
    monkeypatch.setenv("RAG_VECTOR_STORE", "sqlite_vec")
    assert L.check_backend_alignment() is None
