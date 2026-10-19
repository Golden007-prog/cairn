"""#1657 — a watch may not read anything the engine can reach.

The path and the URL come straight from the console's New-watch form. Before
this, that meant any console user could make the ENGINE open any file it could
read and GET any address it could reach — including cloud instance metadata.
Fine while a deployment was one operator's own machine; ADR-0094 (self-serve
signup) changed the premise.
"""
from __future__ import annotations

import os

import pytest

from operator_monitor_threshold.source_guard import (
    ALLOW_PRIVATE_ENV,
    ROOTS_ENV,
    SourceRefused,
    check_path,
    check_url,
)


# ── files ────────────────────────────────────────────────────────────────────

def test_a_file_inside_an_allowed_root_is_read(tmp_path, monkeypatch):
    monkeypatch.setenv(ROOTS_ENV, str(tmp_path))
    f = tmp_path / "metrics.json"
    f.write_text("{}")
    assert check_path(str(f)) == f.resolve()


def test_a_file_outside_every_root_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(ROOTS_ENV, str(tmp_path / "allowed"))
    (tmp_path / "allowed").mkdir()
    with pytest.raises(SourceRefused, match="outside this deployment's allowed roots"):
        check_path("/etc/passwd")


def test_traversal_out_of_a_root_is_refused(tmp_path, monkeypatch):
    """`resolve()` normalises `..` before the comparison, so a path that merely
    STARTS inside a root does not get in."""
    root = tmp_path / "allowed"
    root.mkdir()
    monkeypatch.setenv(ROOTS_ENV, str(root))
    with pytest.raises(SourceRefused):
        check_path(str(root / ".." / "secret.json"))


def test_a_symlink_is_judged_on_where_it_LANDS(tmp_path, monkeypatch):
    """The sharp case: a link inside an allowed root pointing outside it. The
    check follows symlinks first, so it is rejected on its target."""
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "secret.json"
    outside.write_text('{"v": 1}')
    link = root / "innocent.json"
    link.symlink_to(outside)
    monkeypatch.setenv(ROOTS_ENV, str(root))
    with pytest.raises(SourceRefused):
        check_path(str(link))


def test_several_roots_are_honoured(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    monkeypatch.setenv(ROOTS_ENV, os.pathsep.join([str(a), str(b)]))
    (b / "m.json").write_text("{}")
    assert check_path(str(b / "m.json")).parent == b.resolve()


def test_the_default_root_is_the_cairn_home(monkeypatch):
    """Unset must not mean "anywhere" — that is the bug, not the default."""
    monkeypatch.delenv(ROOTS_ENV, raising=False)
    with pytest.raises(SourceRefused):
        check_path("/etc/hosts")


# ── urls ─────────────────────────────────────────────────────────────────────

def test_file_scheme_is_refused(monkeypatch):
    """The bypass that made locking source_path alone pointless: urllib opens
    `file://` happily, so a URL source could read local files regardless."""
    monkeypatch.delenv(ALLOW_PRIVATE_ENV, raising=False)
    with pytest.raises(SourceRefused, match="must be http"):
        check_url("file:///etc/passwd")


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",   # cloud instance metadata
    "http://127.0.0.1:8000/series",               # the engine's own control plane
    "http://localhost:5432/",
    "http://10.0.0.5/metrics.json",               # RFC1918
    "http://192.168.1.10/metrics.json",
])
def test_private_and_link_local_addresses_are_refused(url, monkeypatch):
    monkeypatch.delenv(ALLOW_PRIVATE_ENV, raising=False)
    with pytest.raises(SourceRefused, match="private or link-local"):
        check_url(url)


def test_private_addresses_can_be_allowed_deliberately(monkeypatch):
    """Watching an internal endpoint is a legitimate use — it just has to be a
    decision rather than the default."""
    monkeypatch.setenv(ALLOW_PRIVATE_ENV, "1")
    assert check_url("http://10.0.0.5/metrics.json") == "http://10.0.0.5/metrics.json"


def test_a_url_with_no_host_is_refused(monkeypatch):
    monkeypatch.delenv(ALLOW_PRIVATE_ENV, raising=False)
    with pytest.raises(SourceRefused, match="no host"):
        check_url("http:///metrics.json")


def test_a_public_address_passes(monkeypatch):
    monkeypatch.delenv(ALLOW_PRIVATE_ENV, raising=False)
    assert check_url("https://93.184.216.34/metrics.json").startswith("https://")


def test_an_unresolvable_host_is_refused_not_crashed(monkeypatch):
    monkeypatch.delenv(ALLOW_PRIVATE_ENV, raising=False)
    with pytest.raises(SourceRefused, match="does not resolve"):
        check_url("http://this-host-does-not-exist.invalid/m.json")
