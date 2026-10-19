"""Suggestion engine + store (#478) — routing, dedupe, persistence.

Hermetic: no ledger/DB env needed for the pure routing tests; the store tests
use InMemory + SQLite(:memory:).
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.eval.suggest import (
    build_suggestions,
    cluster_fingerprint,
    generate_and_store,
    route_cluster,
    suggestions_enabled,
)
from cairn.core.storage.suggestions import (
    InMemorySuggestionStore,
    SqliteSuggestionStore,
    Suggestion,
)


def _items(failure_type: str, n: int, *, sig_evidence: dict | None = None) -> list[dict]:
    ev = sig_evidence or {"reason": "timed out after 2 retries"}
    return [
        {"run_id": f"r{failure_type}{i}", "created_at": f"2026-07-05T12:0{i}:00Z",
         "failure_type": failure_type, "evidence": ev}
        for i in range(n)
    ]


# ─── routing table ─────────────────────────────────────────────────────


@pytest.mark.parametrize("ft,category,target_kind", [
    ("generation_failure", "prompt_quality", "template"),
    ("performance_failure", "model_routing", "model_role"),
    ("retrieval_failure", "rag_config", "rag"),
    ("tool_failure", "tool_binding", "operator"),
    ("quality_failure", "dataset", "eval_set"),
    ("safety_failure", "policy_guard", "policy"),
])
def test_route_cluster_maps_failure_type_to_category(ft, category, target_kind):
    cluster = {"failure_type": ft, "locus": "search", "signature": "timeout", "size": 3,
               "member_run_ids": ["a", "b", "c"]}
    s = route_cluster("compliance/report", cluster)
    assert s.category == category
    assert s.target == f"{target_kind}:compliance/report"
    assert s.evidence["cluster"]["size"] == 3
    assert s.evidence["untrusted_metric"] is True  # #458 honesty marker
    assert 0.0 < s.confidence <= 0.9


def test_unrouted_failure_type_falls_back_to_regression_suite():
    cluster = {"failure_type": "system_failure", "locus": "", "signature": "oom", "size": 2}
    s = route_cluster("wf/x", cluster)
    assert s.category == "regression_suite"
    assert s.target == "evals:wf/x"


def test_cluster_fingerprint_shape():
    c = {"failure_type": "tool_failure", "locus": "search", "signature": "timeout"}
    assert cluster_fingerprint(c) == "tool_failure|search|timeout"


def test_confidence_grows_with_cluster_size_capped():
    small = route_cluster("wf/x", {"failure_type": "tool_failure", "size": 2})
    big = route_cluster("wf/x", {"failure_type": "tool_failure", "size": 20})
    assert big.confidence > small.confidence
    assert big.confidence <= 0.9


# ─── build_suggestions: cluster + threshold ────────────────────────────


def test_build_suggestions_respects_min_cluster_size():
    # one timeout cluster of 3, one rate-limit cluster of 1 (below threshold)
    items = _items("tool_failure", 3, sig_evidence={"reason": "timed out"})
    items += _items("tool_failure", 1, sig_evidence={"reason": "rate limit 429"})
    out = build_suggestions("wf/x", items, min_cluster_size=2)
    assert len(out) == 1
    assert out[0].evidence["cluster"]["size"] == 3


def test_build_suggestions_sorted_by_confidence():
    items = _items("tool_failure", 5, sig_evidence={"reason": "timed out"})
    items += _items("generation_failure", 3, sig_evidence={"reason": "bad json"})
    out = build_suggestions("wf/x", items, min_cluster_size=2)
    assert [s.confidence for s in out] == sorted((s.confidence for s in out), reverse=True)


# ─── generate_and_store: dedupe by fingerprint ─────────────────────────


@pytest.mark.asyncio
async def test_generate_and_store_persists_and_dedupes():
    store = InMemorySuggestionStore()
    items = _items("tool_failure", 3, sig_evidence={"reason": "timed out"})

    first = await generate_and_store("wf/x", items, store)
    assert len(first) == 1
    assert len(await store.list_suggestions()) == 1

    # second pass over the SAME cluster → no new suggestion (fingerprint spoken-for)
    second = await generate_and_store("wf/x", items, store)
    assert second == []
    assert len(await store.list_suggestions()) == 1


@pytest.mark.asyncio
async def test_rejected_fingerprint_never_resurfaces():
    store = InMemorySuggestionStore()
    items = _items("tool_failure", 3, sig_evidence={"reason": "timed out"})
    [s] = await generate_and_store("wf/x", items, store)
    await store.update_status(s.suggestion_id, "rejected", reason="wontfix")
    # a later pass must NOT re-emit the same cluster
    assert await generate_and_store("wf/x", items, store) == []


# ─── store tiers ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("make_store", [
    lambda: InMemorySuggestionStore(),
    lambda: SqliteSuggestionStore(path=":memory:"),
])
async def test_store_crud_roundtrip(make_store):
    store = make_store()
    s = Suggestion(cluster_fingerprint="tool_failure|search|timeout", category="tool_binding",
                   target="operator:wf/x", title="fix tool", confidence=0.7)
    await store.add_suggestion(s)

    assert (await store.get_suggestion(s.suggestion_id)).title == "fix tool"
    assert len(await store.list_suggestions(status="proposed")) == 1
    assert await store.open_fingerprints() == {"tool_failure|search|timeout"}

    updated = await store.update_status(s.suggestion_id, "accepted", reason="lgtm")
    assert updated.status == "accepted" and updated.reason == "lgtm" and updated.decided_at
    assert len(await store.list_suggestions(status="proposed")) == 0
    assert len(await store.list_suggestions(status="accepted")) == 1


def test_suggestions_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OBS_SUGGEST", raising=False)
    assert suggestions_enabled() is False
    monkeypatch.setenv("OBS_SUGGEST", "on")
    assert suggestions_enabled() is True


# ─── store selection follows OBS_STORAGE (#1471) ─────────────────────────


def test_suggestion_store_selection_follows_obs_storage(monkeypatch, tmp_path):
    """Postgres deployments must keep `suggestions` in the ONE cairn database.

    Before #1471 the selector only ever built a SQLite store, so on prod the
    table lived on the container filesystem — outside the database the
    migration chain manages, and outside backups.
    """
    from cairn.core.storage.suggestions import (
        PostgresSuggestionStore, SqliteSuggestionStore, default_suggestion_store,
    )

    monkeypatch.delenv("OBS_EVAL_LEDGER_DB", raising=False)
    monkeypatch.setenv("OBS_STORAGE", "postgres://u:p@h/cairn")
    assert isinstance(default_suggestion_store(), PostgresSuggestionStore)

    monkeypatch.setenv("OBS_STORAGE", f"sqlite://{tmp_path / 'cairn.db'}")
    store = default_suggestion_store()
    assert isinstance(store, SqliteSuggestionStore)
    assert store.path == str(tmp_path / "cairn.db")   # the shared file, not a private one
