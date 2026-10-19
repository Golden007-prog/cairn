"""Shared filesystem helpers for storage/put + storage/get.

Pulled out of the operator modules so:

  - the storage root + URI scheme + sha256 computation are all in one place
  - put + get can't drift apart on how a URI maps to a filesystem path
  - tests can patch the storage root via CAIRN_ARTIFACTS_DIR cleanly

The URI scheme is plain ``file://`` with an absolute path. We deliberately
do NOT use ``artifact://<store>/<key>`` — the substrate-level URI
abstraction (per locked memory §2) lives in the artifact service
interface and lands in Phase 3+ alongside the lineage + sensitivity
labels surface. For Phase 2, the operator returns the concrete URI;
the substrate transparently stores both.
"""

from __future__ import annotations
from cairn.core.operator import getenv

import hashlib
import os
from pathlib import Path


_DEFAULT_ROOT = Path.home() / ".cairn" / "artifacts"


def storage_root() -> Path:
    """Resolve the artifact root.

    Reads ``CAIRN_ARTIFACTS_DIR`` if set; otherwise falls back to
    ``~/.cairn/artifacts``. The path is created on first use by
    callers (we don't mkdir here to keep this function side-effect-free
    for test environments).
    """
    raw = getenv("CAIRN_ARTIFACTS_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    return _DEFAULT_ROOT


def artifact_path(store: str, key: str) -> Path:
    """Map (store, key) to a concrete filesystem path.

    Per-store subdir keeps multiple ``[requires.artifacts.<store>]``
    decls from colliding (postmortem-rca and dataset-shards never share
    a key namespace).
    """
    return storage_root() / store / key


def to_uri(path: Path) -> str:
    """Produce the ``file://...`` URI for a resolved absolute path."""
    return f"file://{path.resolve()}"


def from_uri(uri: str) -> Path:
    """Reverse :func:`to_uri`. Accepts both ``file://...`` and bare paths."""
    if uri.startswith("file://"):
        return Path(uri[len("file://") :])
    return Path(uri)


def sha256_hex(content: bytes) -> str:
    """Compute the SHA-256 of ``content`` and return the hex digest.

    Used for both content-addressed key derivation (when the caller
    doesn't supply a key) and lineage-recording (the digest is returned
    on both put and get).
    """
    return hashlib.sha256(content).hexdigest()
