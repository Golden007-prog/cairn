"""Tests for the centralized templates_v2 settings module.

Pins two contracts:
1. Each lazy accessor reads env-on-call (so tests / dispatcher's per-call lookup
   keep working) and falls back to a documented default.
2. The INVENTORY stays in sync with the real accessors — the doctor command
   uses INVENTORY as its source of truth, so a missed entry = a silent gap.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-validate-offline")

from pathlib import Path

import pytest

from cairn.core import settings


# ─── lazy accessors honor env + defaults ────────────────────────────


def test_lane_concurrency_defaults(monkeypatch):
    monkeypatch.delenv("CAIRN_REALTIME_CONCURRENCY", raising=False)
    monkeypatch.delenv("CAIRN_BACKGROUND_CONCURRENCY", raising=False)
    assert settings.lane_concurrency("realtime") == 4
    assert settings.lane_concurrency("background") == 2


def test_lane_concurrency_env_override(monkeypatch):
    monkeypatch.setenv("CAIRN_REALTIME_CONCURRENCY", "9")
    assert settings.lane_concurrency("realtime") == 9


def test_lane_concurrency_clamps_and_ignores_garbage(monkeypatch):
    monkeypatch.setenv("CAIRN_REALTIME_CONCURRENCY", "0")
    assert settings.lane_concurrency("realtime") == 1   # clamped to >= 1
    monkeypatch.setenv("CAIRN_REALTIME_CONCURRENCY", "notanint")
    assert settings.lane_concurrency("realtime") == 4   # falls back


def test_run_db(monkeypatch):
    monkeypatch.delenv("CAIRN_RUN_DB", raising=False)
    assert settings.run_db_path() is None
    assert settings.run_db_is_set() is False
    monkeypatch.setenv("CAIRN_RUN_DB", "/tmp/x.db")
    assert settings.run_db_path() == "/tmp/x.db"
    assert settings.run_db_is_set() is True


def test_action_handler_defaults_and_override(monkeypatch):
    monkeypatch.delenv("CAIRN_ACTION_HANDLER", raising=False)
    assert settings.action_handler() == "in_process"
    monkeypatch.setenv("CAIRN_ACTION_HANDLER", "IN_PROCESS")
    assert settings.action_handler() == "in_process"  # lowercased


def test_slack_channel_accepts_either_var(monkeypatch):
    monkeypatch.delenv("SLACK_CHANNEL", raising=False)
    monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)
    assert settings.slack_channel() is None
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C123")
    assert settings.slack_channel() == "C123"
    monkeypatch.setenv("SLACK_CHANNEL", "#ops")
    assert settings.slack_channel() == "#ops"  # SLACK_CHANNEL preferred


def test_slack_creds_and_approval(monkeypatch):
    for var in ("SLACK_WEBHOOK_URL", "SLACK_BOT_TOKEN", "CAIRN_APPROVAL_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    assert settings.slack_webhook_url() is None
    assert settings.slack_bot_token() is None
    assert settings.approval_base_url() is None
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/...")
    assert settings.slack_webhook_url() == "https://hooks.slack.com/..."


def test_mcp_slack_tool_defaults(monkeypatch):
    monkeypatch.delenv("CAIRN_MCP_SLACK_TOOL", raising=False)
    monkeypatch.delenv("CAIRN_MCP_SLACK_CHANNEL_ARG", raising=False)
    assert settings.mcp_slack_tool() == "slack_post_message"
    assert settings.mcp_slack_channel_arg() == "channel"


# ─── temporal task-queue build isolation (#22) ──────────────────────


def test_temporal_worker_build_explicit(monkeypatch):
    monkeypatch.setenv("CAIRN_WORKER_BUILD", "deploy-42")
    assert settings.temporal_worker_build() == "deploy-42"


def test_temporal_worker_build_defaults_nonempty(monkeypatch):
    monkeypatch.delenv("CAIRN_WORKER_BUILD", raising=False)
    b = settings.temporal_worker_build()
    assert isinstance(b, str) and b  # installed version or "dev", never empty


def test_temporal_task_queue_stamps_build(monkeypatch):
    monkeypatch.delenv("CAIRN_TEMPORAL_TASK_QUEUE", raising=False)
    monkeypatch.setenv("CAIRN_WORKER_BUILD", "b1")
    assert settings.temporal_task_queue() == "cairn-b1"


def test_temporal_task_queue_isolates_divergent_builds(monkeypatch):
    monkeypatch.delenv("CAIRN_TEMPORAL_TASK_QUEUE", raising=False)
    monkeypatch.setenv("CAIRN_WORKER_BUILD", "old")
    old_q = settings.temporal_task_queue()
    monkeypatch.setenv("CAIRN_WORKER_BUILD", "new")
    assert settings.temporal_task_queue() != old_q  # divergent builds never share a queue


def test_temporal_task_queue_custom_base(monkeypatch):
    monkeypatch.setenv("CAIRN_TEMPORAL_TASK_QUEUE", "myq")
    monkeypatch.setenv("CAIRN_WORKER_BUILD", "b1")
    assert settings.temporal_task_queue() == "myq-b1"


# ─── inventory ↔ accessors stay in sync ─────────────────────────────


def test_inventory_envvars_unique():
    envs = [s.env_var for s in settings.INVENTORY]
    assert len(envs) == len(set(envs)), f"duplicate env_var in inventory: {envs}"


def test_inventory_categories_known():
    allowed = {
        "dispatcher", "store", "actions", "notifier", "slack", "mcp", "runtime",
        # audit sweep 2026-06-13 — previously ad-hoc env vars registered:
        "profile", "bus", "obs", "rag", "safety", "operators", "cli",
        # #1613 — tenancy/auth knobs (CAIRN_SIGNUP).
        "auth",
        # #2412 — the complete OBS_* inventory.
        "server", "secrets", "triggers", "jobs", "dataflow", "artifacts", "eval",
        "llm", "policy", "playground", "packs",
    }
    bad = [s.env_var for s in settings.INVENTORY if s.category not in allowed]
    assert not bad, f"unknown category for: {bad}"


def test_inventory_secret_flag_for_credentials():
    # If we ever forget to mark a credential secret, doctor would print it.
    for s in settings.INVENTORY:
        if s.env_var in {"SLACK_WEBHOOK_URL", "SLACK_BOT_TOKEN"}:
            assert s.secret, f"{s.env_var} must be marked secret=True"


def test_get_inventory_is_a_copy():
    inv = settings.get_inventory()
    inv.clear()
    # original untouched
    assert len(settings.INVENTORY) > 0


def test_every_known_envvar_in_inventory():
    """The doctor command reads INVENTORY — if an accessor reads an env var
    that the inventory doesn't list, doctor will silently miss it."""
    inventoried = {s.env_var for s in settings.INVENTORY}
    must_have = {
        "CAIRN_REALTIME_CONCURRENCY", "CAIRN_BACKGROUND_CONCURRENCY",
        "CAIRN_RUN_DB", "CAIRN_ACTION_HANDLER", "CAIRN_CHECKPOINTER",
        "CAIRN_INBOX_PATH", "SLACK_WEBHOOK_URL", "SLACK_BOT_TOKEN", "SLACK_CHANNEL",
        "CAIRN_APPROVAL_BASE_URL", "CAIRN_MCP_SERVERS", "CAIRN_MCP_SLACK_TOOL",
        "CAIRN_MCP_SLACK_CHANNEL_ARG",
    }
    missing = must_have - inventoried
    assert not missing, f"inventory is missing: {missing}"


# ─── one CAIRN_STORAGE → ONE sqlite file (#1471) ────────────────────────────


@pytest.mark.parametrize("spelling", ["sqlite://{p}", "sqlite:///{p}"])
def test_every_store_resolves_one_url_to_one_file(monkeypatch, tmp_path, spelling):
    """ONE ``CAIRN_STORAGE`` → ONE sqlite file, across every store builder.

    Six call sites parsed the URL by hand with two different slash counts, so
    `sqlite:///abs` and `sqlite:////abs` (SQLAlchemy's absolute spelling) each
    split the stores across two files. Both spellings must now agree.
    """
    from cairn.core.storage import build_storage
    from cairn.core.storage.artifacts import build_artifact_store
    from cairn.core.storage.fingerprints import build_fingerprint_store
    from cairn.core.storage.judges import build_judge_store
    from cairn.core.storage.reviews import build_review_store

    db = tmp_path / "cairn.db"
    monkeypatch.delenv("CAIRN_SQLITE_DB", raising=False)
    monkeypatch.delenv("CAIRN_RUN_DB", raising=False)
    monkeypatch.setenv("CAIRN_STORAGE", spelling.format(p=db))

    resolved = {
        "config-family": settings.default_sqlite_db(),
        "artifacts": build_artifact_store().path,
        "fingerprints": build_fingerprint_store().path,
        "judges": build_judge_store().path,
        "reviews": build_review_store().path,
        "runs": build_storage().runs.path,   # build_storage returns the bundle
    }
    assert set(resolved.values()) == {str(db)}, f"stores split across files: {resolved}"


def test_default_sqlite_db_matches_the_registry_stores(monkeypatch, tmp_path):
    """The config family and the registry family must resolve the SAME file.

    ``default_sqlite_db`` stripped ``sqlite:///`` (10 chars) while every store
    builder strips ``sqlite://`` (9) — so `CAIRN_STORAGE=sqlite:///abs/path` put
    policies/triggers/profiles on a CWD-RELATIVE path and runs/artifacts on the
    absolute one: one env var, two databases (found live 2026-08-20).
    """
    from cairn.core.storage.artifacts import build_artifact_store

    db = tmp_path / "cairn.db"
    monkeypatch.delenv("CAIRN_SQLITE_DB", raising=False)
    monkeypatch.setenv("CAIRN_STORAGE", f"sqlite://{db}")
    assert settings.default_sqlite_db() == str(db)          # absolute, not relative
    assert build_artifact_store().path == str(db)           # SAME file as the registry


def test_default_sqlite_db_falls_back_for_memory_and_postgres(monkeypatch):
    """`:memory:` can't be shared across stores (each connection is its own DB)
    and a postgres DSN isn't a file — both keep the durable home default."""
    monkeypatch.delenv("CAIRN_SQLITE_DB", raising=False)
    home_default = str(Path.home() / ".cairn" / "cairn.db")
    for url in ("sqlite://:memory:", "postgres://u:p@h/db", ""):
        monkeypatch.setenv("CAIRN_STORAGE", url)
        assert settings.default_sqlite_db() == home_default


def test_obs_sqlite_db_still_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("CAIRN_STORAGE", f"sqlite://{tmp_path / 'a.db'}")
    monkeypatch.setenv("CAIRN_SQLITE_DB", str(tmp_path / "override.db"))
    assert settings.default_sqlite_db() == str(tmp_path / "override.db")
