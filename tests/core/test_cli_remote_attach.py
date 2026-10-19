"""`cairn remote` ephemeral-local attach (#1195 tail, ADR-0085 D7).

The contract under test is the DECISION, not the subprocess: an ephemeral
engine boots only when no endpoint was explicitly configured anywhere AND the
default one is unreachable. An explicitly configured endpoint that is down
must still fail loudly — never silently substitute a local engine for
someone's prod URL.
"""
from __future__ import annotations

import contextlib

from cairn.cli.commands import remote


def _no_config(monkeypatch, tmp_path):
    monkeypatch.setenv("OBS_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("OBS_REMOTE_ENDPOINT", raising=False)
    monkeypatch.delenv("OBS_REMOTE_TOKEN", raising=False)


def test_explicit_endpoint_detection(monkeypatch, tmp_path):
    _no_config(monkeypatch, tmp_path)
    assert not remote._explicit_endpoint(None, None)
    assert remote._explicit_endpoint("http://x:1", None)
    monkeypatch.setenv("OBS_REMOTE_ENDPOINT", "http://y:2")
    assert remote._explicit_endpoint(None, None)
    monkeypatch.delenv("OBS_REMOTE_ENDPOINT")
    # a saved profile endpoint counts as explicit too
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "config.toml").write_text(
        '[profiles.default]\nendpoint = "http://z:3"\n', encoding="utf-8"
    )
    assert remote._explicit_endpoint(None, None)


def test_session_goes_ephemeral_only_when_unconfigured_and_dead(monkeypatch, tmp_path):
    _no_config(monkeypatch, tmp_path)
    booted = []

    @contextlib.contextmanager
    def fake_ephemeral():
        booted.append(True)
        yield "http://127.0.0.1:1234", "tok"

    monkeypatch.setattr(remote, "_ephemeral", fake_ephemeral)
    monkeypatch.setattr(remote, "_reachable", lambda ep: False)

    with remote._session(None, None, None) as (ep, tok, local):
        assert local and booted and ep == "http://127.0.0.1:1234" and tok == "tok"


def test_session_stays_put_when_default_engine_is_up(monkeypatch, tmp_path):
    _no_config(monkeypatch, tmp_path)
    monkeypatch.setattr(remote, "_reachable", lambda ep: True)
    monkeypatch.setattr(
        remote, "_ephemeral",
        lambda: (_ for _ in ()).throw(AssertionError("must not boot")),
    )
    with remote._session(None, None, None) as (ep, tok, local):
        assert not local and ep == remote._DEFAULT_ENDPOINT and tok is None


def test_session_never_substitutes_for_an_explicit_endpoint(monkeypatch, tmp_path):
    _no_config(monkeypatch, tmp_path)
    monkeypatch.setattr(remote, "_reachable", lambda ep: False)
    monkeypatch.setattr(
        remote, "_ephemeral",
        lambda: (_ for _ in ()).throw(AssertionError("must not boot")),
    )
    # dead but EXPLICIT → yielded as-is; the command then fails loudly
    with remote._session("http://prod.example:9090", None, None) as (ep, _tok, local):
        assert not local and ep == "http://prod.example:9090"
