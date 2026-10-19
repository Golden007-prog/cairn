"""#306 (ADR-0052 §5) — memory is provisioned-then-mandatory.

Two things this pins:
  1. The gate: `_environment_memory_enabled()` reflects the environment's
     `[profile.backends] memory` toggle (default OFF), fail-safe on a bad profile.
  2. The round-trip: with a backend provisioned, an episodic write is recallable
     in a later call (write → recall), using stubbed store/embedder seams so it's
     hermetic (no sqlite_vec / fastembed / model download).
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.engine.runner import _environment_memory_enabled


def _write_profile(tmp_path, body: str) -> str:
    p = tmp_path / "env.toml"
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_gate_on_when_environment_enables_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("OBS_PROFILE", _write_profile(tmp_path, "[profile.backends]\nmemory = true\n"))
    assert _environment_memory_enabled() is True


def test_gate_off_when_memory_false(tmp_path, monkeypatch):
    monkeypatch.setenv("OBS_PROFILE", _write_profile(tmp_path, "[profile.backends]\nmemory = false\n"))
    assert _environment_memory_enabled() is False


def test_gate_off_when_backends_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("OBS_PROFILE", _write_profile(tmp_path, "[profile]\n"))
    assert _environment_memory_enabled() is False


def test_gate_off_when_no_profile(monkeypatch):
    monkeypatch.delenv("OBS_PROFILE", raising=False)
    assert _environment_memory_enabled() is False


def test_gate_off_on_malformed_profile(tmp_path, monkeypatch):
    # a broken profile must never crash the run — fail safe to "off" (no silent capture)
    monkeypatch.setenv("OBS_PROFILE", _write_profile(tmp_path, "this is : not [valid toml"))
    assert _environment_memory_enabled() is False


# ── write → recall round-trip (hermetic; stub the store + embedder seams) ──


class _FakeEmbedder:
    dim = 4

    async def embed_one(self, text: str):
        return [1.0, 0.0, 0.0, 0.0]

    async def embed_sparse_one(self, text: str):
        return None


class _FakeVectorStore:
    """Minimal in-memory stand-in for the vector store contract used by the
    episodic store (initialize / search / upsert / list_sources)."""

    def __init__(self):
        self.docs: list[dict] = []

    async def initialize(self, name, dim):
        return None

    async def search(self, dense, sparse, top_k=5, filters=None):
        # every stored doc is a perfect match — enough to prove recall finds it
        return [{**d, "score": 1.0} for d in self.docs][:top_k]

    async def upsert(self, ids, denses, sparses, payloads):
        self.docs.extend(payloads)

    async def list_sources(self, limit=100):
        return list(self.docs)[:limit]


async def test_episodic_write_is_recallable(monkeypatch):
    import cairn.services.memory.store as store_mod

    fake = _FakeVectorStore()
    monkeypatch.setattr(store_mod, "_get_store", lambda: fake)
    monkeypatch.setattr(store_mod, "get_embedder", lambda: _FakeEmbedder())

    created = await store_mod.save_episodic(
        {"headline": "api latency spike", "service": "checkout"},
        embed_text="checkout api latency spike root cause pool exhaustion",
    )
    assert created["action"] == "created"

    hits = await store_mod.recall_episodic("checkout api latency spike")
    assert any(h.get("headline") == "api latency spike" for h in hits), hits
