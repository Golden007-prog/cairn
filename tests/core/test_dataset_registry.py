"""Dataset registry (#1466) — named, append-only versions over the artifact
store, plus ``dataset:`` ref parsing/resolution. Hermetic: sqlite in-memory."""
from __future__ import annotations

import pytest

from cairn.core.dataflow.contract import ArtifactDescriptor
from cairn.core.storage.artifacts import (
    SqliteArtifactStore,
    _NullArtifactStore,
    parse_dataset_ref,
    resolve_dataset_ref,
)


def _desc(h: str, uri: str) -> ArtifactDescriptor:
    return ArtifactDescriptor(content_hash=h, kind="dataset", uri=uri)


@pytest.fixture()
async def store():
    s = SqliteArtifactStore(":memory:")
    await s.register(_desc("hash-v1", "s3://b/facts-train/one.jsonl"), producer_run_id="run-1")
    await s.register(_desc("hash-v2", "s3://b/facts-train/two.jsonl"), producer_run_id="run-2")
    yield s
    await s.close()


async def test_versions_append_and_resolve(store):
    assert await store.register_dataset_version("facts-train", "hash-v1", actor="t") == 1
    assert await store.register_dataset_version("facts-train", "hash-v2") == 2
    # latest
    v, rec = await store.get_dataset_version("facts-train")
    assert (v, rec.id) == (2, "hash-v2")
    # pinned
    v, rec = await store.get_dataset_version("facts-train", 1)
    assert (v, rec.id, rec.uri) == (1, "hash-v1", "s3://b/facts-train/one.jsonl")
    # history + catalog
    vers = await store.list_dataset_versions("facts-train")
    assert [x["version"] for x in vers] == [2, 1]
    cats = await store.list_datasets()
    assert cats == [{"name": "facts-train", "latest_version": 2,
                     "updated_at": cats[0]["updated_at"],
                     "description": "", "labels": []}]  # #1969: name-level metadata rides along


async def test_identical_head_is_idempotent(store):
    assert await store.register_dataset_version("d", "hash-v1") == 1
    assert await store.register_dataset_version("d", "hash-v1") == 1   # no bump
    assert await store.register_dataset_version("d", "hash-v2") == 2
    assert await store.register_dataset_version("d", "hash-v1") == 3   # content changed back = new version


async def test_unregistered_artifact_rejected(store):
    with pytest.raises(KeyError):
        await store.register_dataset_version("d", "no-such-hash")


async def test_missing_lookups(store):
    assert await store.get_dataset_version("nope") is None
    await store.register_dataset_version("d", "hash-v1")
    assert await store.get_dataset_version("d", 9) is None


def test_parse_dataset_ref():
    assert parse_dataset_ref("s3://bucket/x.jsonl") is None
    assert parse_dataset_ref("dataset:facts-train") == ("facts-train", None)
    assert parse_dataset_ref("dataset:facts-train@latest") == ("facts-train", None)
    assert parse_dataset_ref("dataset:facts-train@3") == ("facts-train", 3)
    with pytest.raises(ValueError):
        parse_dataset_ref("dataset:")
    with pytest.raises(ValueError):
        parse_dataset_ref("dataset:facts@v3")


async def test_resolve_ref(store):
    await store.register_dataset_version("facts-train", "hash-v1")
    await store.register_dataset_version("facts-train", "hash-v2")
    out = await resolve_dataset_ref(store, "dataset:facts-train@1")
    assert out == {"name": "facts-train", "version": 1,
                   "uri": "s3://b/facts-train/one.jsonl",
                   "content_hash": "hash-v1", "size_bytes": None}
    latest = await resolve_dataset_ref(store, "dataset:facts-train")
    assert latest["version"] == 2 and latest["content_hash"] == "hash-v2"
    assert await resolve_dataset_ref(store, "s3://plain/uri.jsonl") is None
    with pytest.raises(KeyError):
        await resolve_dataset_ref(store, "dataset:unknown@1")


async def test_null_store_fails_loudly():
    null = _NullArtifactStore()
    with pytest.raises(RuntimeError):
        await null.register_dataset_version("d", "h")
    assert await null.get_dataset_version("d") is None
    assert await null.list_datasets() == []


async def test_resolve_refs_in_config(store):
    from cairn.core.storage.artifacts import resolve_dataset_refs_in_config

    await store.register_dataset_version("facts-train", "hash-v1")
    cfg = {"dataset_uri": "dataset:facts-train@1", "base_model": "qwen",
           "other": "s3://plain/uri.jsonl", "n": 3}
    out = await resolve_dataset_refs_in_config(store, cfg)
    assert out["dataset_uri"] == "s3://b/facts-train/one.jsonl#sha256=hash-v1"
    assert out["other"] == "s3://plain/uri.jsonl" and out["n"] == 3
    with pytest.raises(KeyError):
        await resolve_dataset_refs_in_config(store, {"x": "dataset:nope@1"})


async def test_resolve_refs_in_config_handles_lists(store):
    """#1625 — ``dataset/replay``'s `sources` is a LIST of prior-cycle refs, and
    the old rewriter only touched top-level strings, so the list reached the box
    UNRESOLVED (and the box has no DB to resolve it with, ADR-0082 D4)."""
    from cairn.core.storage.artifacts import (
        config_has_dataset_ref,
        resolve_dataset_refs_in_config,
    )

    await store.register_dataset_version("facts-train", "hash-v1")
    await store.register_dataset_version("facts-train", "hash-v2")
    cfg = {
        "sources": ["dataset:facts-train@1", "s3://plain/uri.jsonl", "dataset:facts-train"],
        "new_rows_path": "dataset:facts-train@2",
        "plain_list": ["a", "b"],
        "ratio": 0.2,
    }
    assert config_has_dataset_ref(cfg)
    out = await resolve_dataset_refs_in_config(store, cfg)
    assert out["sources"] == [
        "s3://b/facts-train/one.jsonl#sha256=hash-v1",
        "s3://plain/uri.jsonl",                                  # non-refs pass through
        "s3://b/facts-train/two.jsonl#sha256=hash-v2",           # @latest → head
    ]
    assert out["new_rows_path"] == "s3://b/facts-train/two.jsonl#sha256=hash-v2"
    assert out["plain_list"] == ["a", "b"] and out["ratio"] == 0.2
    assert cfg["sources"][0] == "dataset:facts-train@1"  # input not mutated

    # fail-closed for list items too — a typo'd name dies at LAUNCH.
    with pytest.raises(KeyError):
        await resolve_dataset_refs_in_config(store, {"sources": ["dataset:nope@1"]})

    # NOT recursive into dicts: a ref nested under a key has no launch-time
    # contract, so it is left alone rather than silently rewritten.
    nested = {"spec": {"sources": ["dataset:facts-train@1"]}}
    assert not config_has_dataset_ref(nested)
    assert await resolve_dataset_refs_in_config(store, nested) == nested


def test_config_has_dataset_ref_is_the_gate_the_rewriter_uses():
    """The leg executor gates resolution on this predicate (#1625) — if it ever
    recognises LESS than the rewriter, resolution is silently skipped."""
    from cairn.core.storage.artifacts import config_has_dataset_ref

    assert config_has_dataset_ref({"p": "dataset:d@1"})
    assert config_has_dataset_ref({"sources": ["x", "dataset:d"]})
    assert not config_has_dataset_ref({"p": "s3://b/x.jsonl", "n": 3, "l": ["a"]})
    assert not config_has_dataset_ref({})


async def test_leg_registers_dataset_versions_from_export_stats(tmp_path):
    """#1466 slice 3 — the control-plane half: a descriptor whose stats carry
    ``register_as`` (echoed by export.jsonl's `register:` config) becomes a
    named dataset version at leg completion."""
    from cairn.core.dispatch.leg_executor import _register_dataset_versions

    s = SqliteArtifactStore(str(tmp_path / "a.db"))
    d = ArtifactDescriptor(uri="s3://b/dpo-pairs.jsonl", content_hash="e" * 64,
                           kind="dataset", stats={"register_as": "dpo-pairs", "rows": 297})
    plain = ArtifactDescriptor(uri="s3://b/other.jsonl", content_hash="f" * 64, kind="dataset")
    for x in (d, plain):
        await s.register(x, producer_run_id="run-pairs")
    out_stats: dict = {}
    await _register_dataset_versions([d, plain], s, "run-pairs", out_stats)
    assert out_stats["dataset_versions"] == ["dpo-pairs@1"]
    v, rec = await s.get_dataset_version("dpo-pairs")
    assert (v, rec.id) == (1, "e" * 64)
    resolved = await resolve_dataset_ref(s, "dataset:dpo-pairs@latest")
    assert resolved["uri"] == "s3://b/dpo-pairs.jsonl"
    await s.close()
