"""Judge registry (#1467) — immutable versions, refs and launch-time resolution.
Store tests run the in-memory and sqlite tiers through the same
assertions. The CRUD API (`/judges`) is the platform's router: its tests are
packages/cairn-platform/tests/test_judges_api.py (#2424)."""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.storage.judges import (
    InMemoryJudgeStore,
    JudgeDefinition,
    SqliteJudgeStore,
    build_judge_store,
    parse_judge_ref,
    resolve_judge_ref,
    resolve_judge_refs_in_config,
)


def _factuality(**over) -> JudgeDefinition:
    base = dict(
        name="factuality", model="deepseek/deepseek-v4-pro",
        prompt="Grade {ans} against {gold} for {q}. Label: correct|hallucinated|abstain",
        labels=["correct", "hallucinated", "abstain"],
    )
    return JudgeDefinition(**{**base, **over})


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryJudgeStore()
    return SqliteJudgeStore(str(tmp_path / "judges.db"))


async def test_versions_are_immutable_and_append_only(store):
    v1 = await store.put_judge(_factuality())
    assert v1.version == 1 and v1.created_at
    # identical content re-put → the head, no new version
    same = await store.put_judge(_factuality())
    assert same.version == 1
    # an edit appends
    v2 = await store.put_judge(_factuality(model="deepseek/deepseek-v4-flash"))
    assert v2.version == 2
    # history newest-first; v1 still resolvable
    versions = await store.list_judge_versions("factuality")
    assert [d.version for d in versions] == [2, 1]
    got = await store.get_judge("factuality", 1)
    assert got is not None and got.model == "deepseek/deepseek-v4-pro"


async def test_list_judges_returns_latest_of_each(store):
    await store.put_judge(_factuality())
    await store.put_judge(_factuality(model="x/y"))
    await store.put_judge(_factuality(name="strictness", prompt="Strict: {q} {gold} {ans}"))
    latest = await store.list_judges()
    assert [(d.name, d.version) for d in latest] == [("factuality", 2), ("strictness", 1)]


def test_parse_judge_ref():
    assert parse_judge_ref("judge:factuality") == ("factuality", None)
    assert parse_judge_ref("judge:factuality@latest") == ("factuality", None)
    assert parse_judge_ref("judge:factuality@3") == ("factuality", 3)
    assert parse_judge_ref("deepseek/deepseek-v4-pro") is None  # plain model id passes through
    with pytest.raises(ValueError):
        parse_judge_ref("judge:factuality@newest")
    with pytest.raises(ValueError):
        parse_judge_ref("judge:")


async def test_resolve_ref_and_config(store):
    await store.put_judge(_factuality())
    resolved = await resolve_judge_ref(store, "judge:factuality")
    assert resolved is not None
    assert resolved["model"] == "deepseek/deepseek-v4-pro" and resolved["version"] == 1
    assert resolved["prompt_hash"]
    with pytest.raises(KeyError):
        await resolve_judge_ref(store, "judge:nope")

    cfg = {"judge": "judge:factuality@1", "judge_model": "left-alone", "n": 3}
    out = await resolve_judge_refs_in_config(store, cfg)
    assert out["judge"]["name"] == "factuality" and out["judge"]["prompt"].startswith("Grade")
    assert out["judge_model"] == "left-alone" and out["n"] == 3
    assert cfg["judge"] == "judge:factuality@1"  # input untouched


async def test_null_store_fails_loudly_on_put(monkeypatch):
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    null = build_judge_store()
    assert await null.list_judges() == []
    with pytest.raises(RuntimeError, match="not configured"):
        await null.put_judge(_factuality())

