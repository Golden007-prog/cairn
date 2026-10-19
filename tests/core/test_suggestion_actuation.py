"""Suggestion actuation surface (#488) — adaptive, pluggable, toggleable.

GitHub linked → a GitHub issue (diff-less) / PR (with diff); not linked → a
database-based issue. Hermetic: GitHubActuator runs with dry_run=True (builds
the plan, no git/gh side effects); `_github_linked` is monkeypatched for `auto`.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.eval import actuation as act
from cairn.core.eval.actuation import DbIssueActuator, GitHubActuator, get_actuator
from cairn.core.storage.suggestions import Suggestion


def _suggestion(**over) -> Suggestion:
    base = dict(
        cluster_fingerprint="tool_failure|search|timeout",
        category="tool_binding", target="operator:compliance/report",
        title="Fix the tool binding for compliance/report",
        description="3 runs share a search timeout.",
        evidence={"cluster": {"failure_type": "tool_failure", "signature": "timeout",
                              "locus": "search", "size": 3, "member_run_ids": ["a", "b", "c"]},
                  "untrusted_metric": True},
        confidence=0.7,
    )
    base.update(over)
    return Suggestion(**base)


# ─── selector / toggle ─────────────────────────────────────────────────


def test_actuator_off_by_default(monkeypatch):
    monkeypatch.delenv("OBS_SUGGEST_ACTUATOR", raising=False)
    assert get_actuator() is None


def test_actuator_selects_github(monkeypatch):
    monkeypatch.setenv("OBS_SUGGEST_ACTUATOR", "github")
    assert isinstance(get_actuator(), GitHubActuator)


def test_actuator_selects_db(monkeypatch):
    monkeypatch.setenv("OBS_SUGGEST_ACTUATOR", "db")
    assert isinstance(get_actuator(), DbIssueActuator)


def test_auto_picks_github_when_linked(monkeypatch):
    monkeypatch.setenv("OBS_SUGGEST_ACTUATOR", "auto")
    monkeypatch.setattr(act, "_github_linked", lambda repo_dir=".": True)
    assert isinstance(get_actuator(), GitHubActuator)


def test_auto_falls_back_to_db_when_not_linked(monkeypatch):
    monkeypatch.setenv("OBS_SUGGEST_ACTUATOR", "auto")
    monkeypatch.setattr(act, "_github_linked", lambda repo_dir=".": False)
    assert isinstance(get_actuator(), DbIssueActuator)


# ─── GitHub binding: issue for diff-less, PR once a diff exists ─────────


@pytest.mark.asyncio
async def test_github_diffless_opens_issue_dry_run():
    result = await GitHubActuator(dry_run=True).actuate(_suggestion())
    assert result.ok and result.kind == "github-issue"
    assert result.url is None  # nothing opened
    plan = result.plan
    assert plan["mode"] == "issue"
    assert "branch" not in plan and "file" not in plan  # an issue has no branch
    assert "compliance/report" in plan["body"]
    assert "a, b, c" in plan["body"]              # affected runs enumerated
    assert "#458" in plan["body"]                 # untrusted-metric warning surfaced


@pytest.mark.asyncio
async def test_github_with_diff_opens_pr_dry_run():
    result = await GitHubActuator(dry_run=True).actuate(
        _suggestion(proposed_diff="--- a/prompt\n+++ b/prompt\n@@ tighten @@")
    )
    assert result.ok and result.kind == "github-pr"
    plan = result.plan
    assert plan["mode"] == "pr"
    assert plan["branch"].startswith("suggest/")
    assert plan["file"].startswith("docs/suggestions/") and plan["file"].endswith(".md")
    assert "```diff" in plan["body"]  # the concrete change is carried


# ─── DB binding (database-based issue) ─────────────────────────────────


@pytest.mark.asyncio
async def test_db_actuator_records_database_issue():
    result = await DbIssueActuator().actuate(_suggestion())
    assert result.ok and result.kind == "db-issue"
    assert "database issue" in result.detail.lower()
    assert "deferred" in result.detail.lower()
    assert result.plan is not None
