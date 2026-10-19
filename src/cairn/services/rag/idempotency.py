"""Deterministic identity + change-detection hashes for RAG indexing (ADR-0023).

Content-hash idempotency is what lets the index job drop durability (§24.23.2):
"resume" ≡ "re-run + skip-unchanged" via manifest comparison. All ids are
deterministic sha256s so the same input always maps to the same catalog row,
backend-agnostic.

  - ``document_id(corpus, source)``  — stable identity of a document in a corpus
  - ``content_hash(path)``           — sha256 of the file's bytes (did content change?)
  - ``build_fingerprint(config)``    — sha256 of the resolved build recipe
    (parser/chunk/embedding-model/dim/store); a change here re-indexes even when
    bytes are unchanged (e.g. swapped embedding model → stale vectors).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

_CHUNK = 1 << 16


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def document_id(corpus: str, source: str) -> str:
    """Stable id for ``source`` within ``corpus`` (NUL-joined, collision-safe)."""
    return _sha256(f"{corpus}\x00{source}".encode("utf-8"))


def content_hash(path: str) -> str:
    """sha256 of the file's bytes, streamed so large files don't load whole."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def build_fingerprint(config: dict[str, Any]) -> str:
    """sha256 over the canonicalized build config (stable key order)."""
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return _sha256(canonical.encode("utf-8"))
