"""Incremental index planner — the pure decision layer (ADR-0023 Fork C)."""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from cairn.core.storage.rag_catalog import CatalogEntry
from cairn.services.rag.idempotency import document_id
from cairn.services.rag.plan import plan

CORPUS = "c"
FP = "fp1"


def _entry(source, chash, *, status="indexed", fp=FP):
    return CatalogEntry(
        document_id=document_id(CORPUS, source), corpus=CORPUS, source=source,
        content_hash=chash, build_fingerprint=fp, status=status,
    )


def test_new_file_is_indexed():
    p = plan(CORPUS, {"a": "h"}, [], FP)
    assert p.to_index == ["a"] and not p.to_skip and not p.to_delete


def test_unchanged_file_is_skipped():
    p = plan(CORPUS, {"a": "h"}, [_entry("a", "h")], FP)
    assert p.to_skip == ["a"] and not p.to_index and p.is_noop


def test_changed_content_reindexed():
    p = plan(CORPUS, {"a": "h2"}, [_entry("a", "h1")], FP)
    assert p.to_index == ["a"]


def test_build_fingerprint_change_reindexes():
    p = plan(CORPUS, {"a": "h"}, [_entry("a", "h", fp="old-model")], FP)
    assert p.to_index == ["a"]  # bytes same, recipe changed


def test_failed_entry_is_retried():
    p = plan(CORPUS, {"a": "h"}, [_entry("a", "h", status="failed")], FP)
    assert p.to_index == ["a"]


def test_removed_file_is_tombstoned():
    p = plan(CORPUS, {}, [_entry("a", "h")], FP)
    assert p.to_delete == ["a"] and not p.to_index


def test_tombstoned_entry_not_redeleted():
    p = plan(CORPUS, {}, [_entry("a", "h", status="tombstoned")], FP)
    assert not p.to_delete and p.is_noop
