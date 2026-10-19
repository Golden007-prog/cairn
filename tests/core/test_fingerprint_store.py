"""ADR-0080 P2 (#1030 / rejected design R3) — the cross-batch fingerprint index.

Cross-batch dedup state as an INDEXED table, not a parquet glob. These tests run
against the hermetic SQLite backend (:memory:) — a real indexed anti-join + upsert
with the same semantics as the production Postgres store — and pin: the O(batch)
probe, ON CONFLICT idempotency, the two-concurrent-batches overlap acceptance,
dataset scoping, retention, and the graceful null (unconfigured) fallback.
"""
from __future__ import annotations

import asyncio

import pytest

from cairn.core.storage.fingerprints import (
    ProbeResult,
    SqliteFingerprintStore,
    _NullFingerprintStore,
    build_fingerprint_store,
)


def _run(coro):
    return asyncio.run(coro)


def _hashes(prefix, n):
    return [f"{prefix}-{i:06d}" for i in range(n)]


def test_publish_then_probe_detects_overlap():
    store = SqliteFingerprintStore(":memory:")
    hs = _hashes("a", 100)
    new = _run(store.publish("ds1", "batch-1", hs))
    assert new == 100
    assert _run(store.count("ds1")) == 100
    # a later batch sharing 30 of those rows: the probe reports exactly 30 overlap.
    later = hs[:30] + _hashes("b", 70)
    res = _run(store.probe("ds1", later))
    assert isinstance(res, ProbeResult)
    assert res.checked is True
    assert res.overlap == 30
    assert res.probed == 100
    assert res.overlap_pct == 30.0


def test_publish_is_idempotent_on_conflict():
    store = SqliteFingerprintStore(":memory:")
    hs = _hashes("a", 50)
    assert _run(store.publish("ds1", "batch-1", hs)) == 50
    # re-publishing the same rows (a retry, or an overlapping batch) inserts none.
    assert _run(store.publish("ds1", "batch-1", hs)) == 0
    assert _run(store.publish("ds1", "batch-2", hs)) == 0
    assert _run(store.count("ds1")) == 50


def test_two_concurrent_batches_second_detects_and_dedups():
    """The R3 acceptance: two batches share rows. Batch A publishes; batch B probes
    and SEES the overlap (a sampled parquet-glob would miss in-flight rows), then
    B's publish inserts only its NEW rows — the shared rows serialize on the PK."""
    store = SqliteFingerprintStore(":memory:")
    shared = _hashes("shared", 40)
    a_only = _hashes("a", 60)
    b_only = _hashes("b", 60)

    assert _run(store.publish("ds1", "batch-A", shared + a_only)) == 100
    # B probes before publishing → advisory overlap = the 40 shared rows.
    res = _run(store.probe("ds1", shared + b_only))
    assert res.overlap == 40
    # B publishes → only its 60 novel rows land; the 40 shared are no-ops.
    assert _run(store.publish("ds1", "batch-B", shared + b_only)) == 60
    assert _run(store.count("ds1")) == 160  # 100 + 60, shared counted once


def test_dataset_scoping_no_cross_dataset_false_overlap():
    store = SqliteFingerprintStore(":memory:")
    hs = _hashes("x", 20)
    _run(store.publish("dsA", "b1", hs))
    # the IDENTICAL rows in a different dataset must not read as an overlap.
    res = _run(store.probe("dsB", hs))
    assert res.overlap == 0
    # and publishing them under dsB is all-new (PK is (dataset_id, hash)).
    assert _run(store.publish("dsB", "b1", hs)) == 20


def test_probe_exclude_batch_ignores_own_rows():
    store = SqliteFingerprintStore(":memory:")
    hs = _hashes("a", 30)
    _run(store.publish("ds1", "batch-1", hs))
    # re-probing batch-1's own rows, excluding itself → no overlap.
    res = _run(store.probe("ds1", hs, exclude_batch="batch-1"))
    assert res.overlap == 0
    # without the exclusion, they all overlap.
    assert _run(store.probe("ds1", hs)).overlap == 30


def test_retention_keep_batches():
    store = SqliteFingerprintStore(":memory:")
    for b in range(5):
        _run(store.publish("ds1", f"batch-{b}", _hashes(f"b{b}", 10), created_at=f"2026-07-{b+1:02d}"))
    assert _run(store.count("ds1")) == 50
    # keep only the 2 most-recent batches → 30 fingerprints dropped.
    dropped = _run(store.retain(dataset_id="ds1", keep_batches=2))
    assert dropped == 30
    assert _run(store.count("ds1")) == 20


def test_retention_keep_days():
    store = SqliteFingerprintStore(":memory:")
    _run(store.publish("ds1", "old", _hashes("o", 10), created_at="2000-01-01T00:00:00+00:00"))
    _run(store.publish("ds1", "new", _hashes("n", 10)))  # created now
    dropped = _run(store.retain(dataset_id="ds1", keep_days=3650))
    assert dropped == 10
    assert _run(store.count("ds1")) == 10


def test_internal_batch_dups_counted_once():
    store = SqliteFingerprintStore(":memory:")
    # a batch that repeats a row internally publishes it once (distinct content).
    assert _run(store.publish("ds1", "b1", ["h1", "h1", "h1", "h2"])) == 2
    assert _run(store.count("ds1")) == 2
    # an internally-duplicated probe also counts distinct content once.
    assert _run(store.probe("ds1", ["h1", "h1"])).overlap == 1


def test_null_store_is_advisory_only():
    store = _NullFingerprintStore()
    res = _run(store.probe("ds1", _hashes("a", 10)))
    assert res.checked is False
    assert res.overlap == 0
    assert res.probed == 10       # still reports how many it WOULD have probed
    assert _run(store.publish("ds1", "b1", _hashes("a", 10))) == 0
    assert _run(store.count()) == 0


def test_resolver_picks_backend(monkeypatch):
    from cairn.core.storage.fingerprints import PostgresFingerprintStore
    # with OBS_STORAGE unset and no dsn → advisory-only null store.
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    assert isinstance(build_fingerprint_store(""), _NullFingerprintStore)
    assert isinstance(build_fingerprint_store("sqlite://:memory:"), SqliteFingerprintStore)
    # postgres:// resolves to the PG store WITHOUT connecting (lazy pool).
    assert isinstance(
        build_fingerprint_store("postgres://u:p@localhost:5432/db"),
        PostgresFingerprintStore,
    )
    # an explicit dsn also overrides a set OBS_STORAGE.
    monkeypatch.setenv("OBS_STORAGE", "postgres://env/db")
    assert isinstance(build_fingerprint_store("sqlite://:memory:"), SqliteFingerprintStore)


def test_empty_probe_is_clean():
    store = SqliteFingerprintStore(":memory:")
    res = _run(store.probe("ds1", []))
    assert res == ProbeResult(checked=True, overlap=0, probed=0)
    assert res.overlap_pct == 0.0
