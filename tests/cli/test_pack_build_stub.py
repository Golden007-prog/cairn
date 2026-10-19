"""`cairn pack build` needs the authoring SDK, which is the platform's (ADR-0135): without
`cairn_sdk` the public CLI mounts a hidden stub that says so, and never a command that fails
on import."""
from __future__ import annotations

import importlib
import importlib.util
import sys

import pytest
from typer.testing import CliRunner


@pytest.fixture
def pack_without_sdk(monkeypatch):
    real = importlib.util.find_spec

    def _no_sdk(name, *a, **k):
        return None if name == "cairn_sdk" else real(name, *a, **k)

    monkeypatch.setattr(importlib.util, "find_spec", _no_sdk)
    saved = sys.modules.pop("cairn.cli.commands.pack", None)
    mod = importlib.import_module("cairn.cli.commands.pack")
    yield mod
    if saved is not None:   # the module object `cairn.cli.main` mounted stays the one other tests see
        sys.modules["cairn.cli.commands.pack"] = saved
    else:
        sys.modules.pop("cairn.cli.commands.pack", None)


def test_without_the_sdk_build_is_a_hidden_stub_that_names_the_platform(pack_without_sdk):
    app = pack_without_sdk.app
    r = CliRunner().invoke(app, ["build", "x.py:Cls"])
    assert r.exit_code == 1 and "part of cairn-platform" in r.output
    assert "build" not in CliRunner().invoke(app, ["--help"]).output   # hidden: not advertised


def test_with_the_sdk_build_is_the_real_command():
    pytest.importorskip("cairn_sdk")
    saved = sys.modules.pop("cairn.cli.commands.pack", None)
    try:
        mod = importlib.import_module("cairn.cli.commands.pack")
        assert "build" in CliRunner().invoke(mod.app, ["--help"]).output
    finally:
        if saved is not None:
            sys.modules["cairn.cli.commands.pack"] = saved
