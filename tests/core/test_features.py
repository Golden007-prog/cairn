"""#1878 (thin carrier) — the resolved feature map and its env seeding."""
from __future__ import annotations

from cairn.core.features import FEATURE_IDS, LIGHT_OFF, resolve_features


def test_default_is_everything_on():
    f = resolve_features({})
    assert set(f) == set(FEATURE_IDS)
    assert all(f.values()), "an absent flag is ON — flag off is the only change from today"


def test_light_preset_turns_the_held_set_off_and_nothing_else():
    f = resolve_features({"OBS_LIGHT": "on"})
    assert {k for k, v in f.items() if not v} == set(LIGHT_OFF)
    assert f["playground"] and f["insights"] and f["gateway"]


def test_per_feature_override_wins_over_the_preset_both_ways():
    f = resolve_features({"OBS_LIGHT": "1", "OBS_FEATURE_PIPELINES": "on", "OBS_FEATURE_INSIGHTS": "off"})
    assert f["pipelines"] is True and f["insights"] is False and f["builder"] is False


def test_garbage_values_are_ignored():
    f = resolve_features({"OBS_LIGHT": "maybe", "OBS_FEATURE_JUDGES": "sometimes"})
    assert all(f.values())
