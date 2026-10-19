"""A profile that pins the runtime must actually pin it (#1852).

Before: `durable_runtime = "dbos"` in a profile was accepted and silently
ignored — profile_backends resolved it for the UI but no consumer read it, and
settings.runtime() read only OBS_RUNTIME. That produced a false positive while
verifying #1847: a gated run completed "done" and read as DBOS having handled
it, when DBOS had never launched. A governance control that is accepted and
ignored is worse than one that is absent.
"""
from __future__ import annotations

import os
import time

import pytest

from cairn.core import settings
from cairn.core.deploy_check import check_deploy


def _profile(tmp_path, body: str, name: str = "p.toml"):
    f = tmp_path / name
    f.write_text("[profile]\nvisibility = \"internal\"\n" + body)
    return f


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("OBS_PROFILE", raising=False)
    monkeypatch.delenv("OBS_RUNTIME", raising=False)
    getattr(settings, "_PROFILE_RUNTIME_CACHE", {}).clear()   # tolerant, so the control shows the symptom


def test_no_profile_keeps_env_then_default(monkeypatch):
    assert settings.runtime() == "inprocess"
    monkeypatch.setenv("OBS_RUNTIME", "dbos")
    assert settings.runtime() == "dbos"


def test_profile_pins_the_runtime_when_env_is_unset(tmp_path, monkeypatch):
    """The #1852 case, exactly as it was hit."""
    monkeypatch.setenv("OBS_PROFILE", str(_profile(tmp_path, "[profile.backends]\ndurable_runtime = \"dbos\"\n")))
    assert settings.runtime() == "dbos"


def test_profile_wins_over_env(tmp_path, monkeypatch):
    """Documented precedence: profile → env → default (#1093)."""
    monkeypatch.setenv("OBS_PROFILE", str(_profile(tmp_path, "[profile.backends]\ndurable_runtime = \"dbos\"\n")))
    monkeypatch.setenv("OBS_RUNTIME", "temporal")
    assert settings.runtime() == "dbos"


def test_in_process_maps_to_runtimes_vocabulary(tmp_path, monkeypatch):
    """profile_backends spells it in_process; runtime() has always returned
    inprocess. The mapping is the seam, not a second spelling."""
    monkeypatch.setenv("OBS_PROFILE", str(_profile(tmp_path, "[profile.backends]\ndurable_runtime = \"in_process\"\n")))
    monkeypatch.setenv("OBS_RUNTIME", "dbos")
    assert settings.runtime() == "inprocess"


def test_profile_without_the_key_falls_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OBS_PROFILE", str(_profile(tmp_path, "[profile.backends]\nmemory = true\n")))
    monkeypatch.setenv("OBS_RUNTIME", "temporal")
    assert settings.runtime() == "temporal"


def test_a_typo_refuses_instead_of_falling_through(tmp_path, monkeypatch):
    """`dbso` used to be accepted and ignored — the environment would believe
    the runtime was pinned. It must fail, naming the file and the vocabulary."""
    pf = _profile(tmp_path, "[profile.backends]\ndurable_runtime = \"dbso\"\n")
    monkeypatch.setenv("OBS_PROFILE", str(pf))
    with pytest.raises(ValueError, match="in_process, temporal, dbos") as ei:
        settings.runtime()
    assert str(pf) in str(ei.value) and "#1852" in str(ei.value)


def test_unreachable_profile_path_falls_back_to_env(monkeypatch):
    """A container-only path seen from the host (the doctor's situation) is
    not an error — it is simply not consulted."""
    monkeypatch.setenv("OBS_PROFILE", "/app/.venv/lib/python3.13/site-packages/cairn/profiles/secure.toml")
    monkeypatch.setenv("OBS_RUNTIME", "dbos")
    assert settings.runtime() == "dbos"


def test_cache_invalidates_when_the_file_changes(tmp_path, monkeypatch):
    pf = _profile(tmp_path, "[profile.backends]\ndurable_runtime = \"dbos\"\n")
    monkeypatch.setenv("OBS_PROFILE", str(pf))
    assert settings.runtime() == "dbos"
    pf.write_text("[profile]\nvisibility = \"internal\"\n[profile.backends]\ndurable_runtime = \"temporal\"\n")
    os.utime(pf, ns=(time.time_ns() + 2_000_000_000,) * 2)   # force a distinct mtime
    assert settings.runtime() == "temporal"


# ── the doctor judges the same runtime the app will use ──────────────────

_VALID = {
    "CADDY_DOMAIN": "app.example.com",
    "POSTGRES_PASSWORD": "s3cret",
    "OBS_JWT_SECRET": "jwt",
    "OBS_API_PASSWORD_HASH": "",
}


def _by_name(results, name):
    return next(r for r in results if r.name == name)


def test_doctor_honours_a_profile_pinned_runtime(tmp_path):
    """dbos + memory checkpointer is refused (#1847) — but only if the doctor
    knows the profile pinned dbos. With OBS_RUNTIME unset and the profile
    pinning dbos, it must still refuse."""
    pf = _profile(tmp_path, "[profile.backends]\ndurable_runtime = \"dbos\"\n")
    env = {**_VALID, "OBS_PROFILE": str(pf)}          # no OBS_RUNTIME at all
    r = _by_name(check_deploy(env, set()), "checkpointer")
    assert r.status == "fail" and "CAIRN_RUNTIME=dbos" in r.detail


def test_doctor_fails_on_an_invalid_profile_runtime(tmp_path):
    pf = _profile(tmp_path, "[profile.backends]\ndurable_runtime = \"dbso\"\n")
    env = {**_VALID, "OBS_PROFILE": str(pf)}
    r = _by_name(check_deploy(env, set()), "runtime")
    assert r.status == "fail" and "#1852" in r.detail
