"""Dataset manifest — the artifact form for sharded data (ADR-0081 D2, #1046).

A dataset artifact IS a manifest: an ordered list of shard URIs with per-shard
write-time stats, plus a deterministic content hash. Gates and profilers read
the manifest (kilobytes), never the shards — stats are frozen at write time so
"judge the dataset" never means "stream the dataset".

Dependency-free by design (no pyarrow/polars here): this module handles the
manifest JSON only; shard bytes are the ENGINE's business (cairn-operator-dataflow
``[engine]`` extra, ADR-0081 D5). Content-address semantics follow the DVC
steal recorded in the ADR's prior-art table: same shards + same hashes → same
manifest hash, which is what makes stage-level caching and idempotent re-runs
checkable."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class ShardRef(BaseModel):
    """One shard of a dataset artifact."""

    model_config = ConfigDict(extra="forbid")

    uri: str = Field(..., min_length=1)
    content_hash: str = Field("", description="Hash of the shard bytes (engine-computed).")
    rows: Optional[int] = Field(None, ge=0)
    size_bytes: Optional[int] = Field(None, ge=0)
    stats: dict[str, Any] = Field(default_factory=dict)


class Manifest(BaseModel):
    """A dataset version: shards + schema + aggregate stats."""

    model_config = ConfigDict(extra="forbid")

    shards: list[ShardRef] = Field(default_factory=list)
    data_schema: Optional[dict[str, Any]] = None
    stats: dict[str, Any] = Field(default_factory=dict)

    @property
    def rows(self) -> Optional[int]:
        counts = [s.rows for s in self.shards]
        if any(c is None for c in counts):
            return None
        return sum(counts)  # type: ignore[arg-type]

    def content_hash(self) -> str:
        """Deterministic identity of this dataset version.

        Keyed by the shards' (uri, content_hash, rows) in ORDER — not by stats
        or schema, which are derived observations about the same bytes. Two
        manifests describing identical shards hash identically even if one
        carries richer stats."""
        key = json.dumps(
            [[s.uri, s.content_hash, s.rows] for s in self.shards],
            sort_keys=False, separators=(",", ":"),
        )
        return hashlib.blake2b(key.encode("utf-8"), digest_size=16).hexdigest()

    # ── json round-trip (the file an engine writes next to its shards) ──────

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> "Manifest":
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
