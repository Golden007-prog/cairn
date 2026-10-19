"""The licence door reads catalogues through the ``cairn.model_catalogs`` entry-point
group (ADR-0120 D4, #2414): absent = no catalogue, a broken provider fails open, and
the public core registers none. The platform's provider (the training catalogue) is
proven in packages/cairn-platform/tests/test_models_catalog_provider.py (#2421)."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from cairn.core.pack import model_catalogs as MC

REPO = Path(__file__).resolve().parents[2]
GATED_MODEL = "meta-llama/Llama-3.1-8B-Instruct"


def _ep(name, value, obj=None, error=None):
    def load():
        if error:
            raise error
        return obj
    return SimpleNamespace(name=name, value=value, load=load)


def _eps(monkeypatch, *eps):
    monkeypatch.setattr(MC, "entry_points", lambda group: list(eps) if group == MC.ENTRY_POINT_GROUP else [])


def test_no_provider_means_no_catalogue(monkeypatch):
    _eps(monkeypatch)
    assert MC.load_catalog_providers() == {}
    assert MC.lookup(GATED_MODEL) is None


async def test_without_a_catalogue_the_door_checks_nothing(monkeypatch):
    """ADR-0120 D4 'absent means': a base_model input is not checked against a catalogue."""
    from cairn.core.model_license_gate import check_license_for_inputs
    _eps(monkeypatch)
    v = await check_license_for_inputs({"base_model": GATED_MODEL})
    assert v.ok and not v.checked


def test_a_broken_provider_fails_open_and_is_recorded(monkeypatch):
    _eps(monkeypatch, _ep("bad", "x:y", error=ImportError("no x")), _ep("flat", "x:z", obj=42))
    assert MC.load_catalog_providers() == {}
    assert [(f["name"], f["error"]) for f in MC.catalog_load_failures()] == [
        ("bad", "ImportError: no x"), ("flat", "not callable")]


def test_first_provider_with_an_entry_wins_and_a_raising_one_is_skipped(monkeypatch):
    def boom(_):
        raise RuntimeError("down")
    _eps(monkeypatch,
         _ep("a-raises", "a:p", obj=boom),
         _ep("b-none", "b:p", obj=lambda _m: None),
         _ep("c-hit", "c:p", obj=lambda m: {"id": m, "gated": True}),
         _ep("d-later", "d:p", obj=lambda m: {"id": m, "gated": False}))
    assert MC.lookup("m") == {"id": "m", "gated": True}


def test_the_public_core_registers_no_catalogue():
    """The catalogue is the platform's (#2421 wave 2f: packages/cairn-platform declares
    `training`); the root distribution declares none, so on the public set alone the
    licence door checks nothing (ADR-0120 D4, "absent means")."""
    eps = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["entry-points"]
    assert "cairn.model_catalogs" not in eps
