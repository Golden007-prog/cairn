"""MCP connector presets — known-good specs (item 7)."""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.services.mcp import presets


def test_ships_the_three_presets():
    assert presets.list_presets() == ["github", "notion", "slack"]


def test_load_preset_shape():
    gh = presets.load_preset("github")
    assert gh["connector"]["name"] == "github"
    assert gh["server"]["transport"] == "stdio"
    assert gh["server"]["command"][0] == "npx"
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" in gh["env"]
    assert gh["tools"]["common"]  # representative tool surface


def test_unknown_preset_raises():
    with pytest.raises(KeyError):
        presets.load_preset("does-not-exist")


def test_preset_connection_resolves_env(monkeypatch):
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "ghp_test")
    conn = presets.preset_connection("github")
    assert conn["transport"] == "stdio"
    assert conn["command"] == "npx"
    assert conn["args"] == ["-y", "@modelcontextprotocol/server-github"]
    assert conn["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == "ghp_test"


def test_missing_env_flags_unset_required(monkeypatch):
    monkeypatch.delenv("GITHUB_PERSONAL_ACCESS_TOKEN", raising=False)
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" in presets.missing_env("github")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "x")
    assert presets.missing_env("github") == []


def test_required_env_per_preset():
    assert "NOTION_API_KEY" in presets.required_env("notion")
    assert "SLACK_BOT_TOKEN" in presets.required_env("slack")
