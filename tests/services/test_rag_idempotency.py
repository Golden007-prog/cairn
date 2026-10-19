"""RAG idempotency hashes — determinism + change detection (ADR-0023)."""
from __future__ import annotations

from cairn.services.rag.idempotency import (
    build_fingerprint,
    content_hash,
    document_id,
)


def test_document_id_deterministic_and_corpus_scoped():
    a = document_id("runbooks", "/x/a.md")
    assert a == document_id("runbooks", "/x/a.md")          # stable
    assert a != document_id("service_docs", "/x/a.md")      # corpus-scoped
    assert a != document_id("runbooks", "/x/b.md")          # source-scoped


def test_content_hash_tracks_bytes(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("hello")
    h1 = content_hash(str(p))
    assert h1 == content_hash(str(p))
    p.write_text("hello world")
    assert content_hash(str(p)) != h1


def test_build_fingerprint_order_independent_but_value_sensitive():
    assert build_fingerprint({"a": 1, "b": 2}) == build_fingerprint({"b": 2, "a": 1})
    assert build_fingerprint({"a": 1}) != build_fingerprint({"a": 2})
