"""#1432 — cross-leg lineage: URI parents on a leg's descriptors resolve to the
registry ids of the artifacts stored at those URIs (sqlite store + the leg
executor's resolver)."""
from __future__ import annotations

import pytest

from cairn.core.dataflow.contract import ArtifactDescriptor
from cairn.core.dispatch.leg_executor import _resolve_uri_parents
from cairn.core.storage.artifacts import SqliteArtifactStore


@pytest.mark.asyncio
async def test_find_by_uri_and_parent_resolution(tmp_path):
    store = SqliteArtifactStore(path=str(tmp_path / "a.db"))
    await store.ensure_schema()
    prep_export = ArtifactDescriptor(uri="s3://bkt/dataflow/prep/train-ready.jsonl", content_hash="a" * 64,
                                     size_bytes=10, rows=3)
    pid = await store.register(prep_export, producer_run_id="run-prep")
    assert await store.find_by_uri("s3://bkt/dataflow/prep/train-ready.jsonl") == pid
    assert await store.find_by_uri("s3://bkt/nope") is None

    # the NEXT leg's stage 0 names the S3 export as its parent (a URI) …
    curate0 = ArtifactDescriptor(uri="file:///tmp/wd/stage-00", content_hash="b" * 64,
                                 parents=["s3://bkt/dataflow/prep/train-ready.jsonl", "s3://external/raw.jsonl"])
    curate1 = ArtifactDescriptor(uri="s3://bkt/dataflow/curate/curated.jsonl", content_hash="c" * 64,
                                 parents=["b" * 64])
    await _resolve_uri_parents([curate0, curate1], store)
    # … resolved to the registry id; an unknown external URI is kept verbatim
    assert curate0.parents == [pid, "s3://external/raw.jsonl"]
    assert curate1.parents == ["b" * 64]
    for d in (curate0, curate1):
        await store.register(d, producer_run_id="run-curate")
    # the DAG now walks curate → prep across legs
    lin = await store.lineage("c" * 64, depth=5)
    assert lin is not None
    flat = str(lin)
    assert pid in flat and "b" * 64 in flat
