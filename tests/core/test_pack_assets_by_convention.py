"""Asset dirs resolve BY CONVENTION; `[assets]` becomes exceptions-only (#1106 D).

All seven shipped packs carry an `[assets]` block whose entries almost always
restate the obvious — `templates = "templates"`, `schemas = "schemas"`. Fourteen
optional path fields exist so an author can say what the directory layout already
says. Convention removes the ceremony: a standard dir that EXISTS is discovered,
and `[assets]` is reserved for genuine exceptions (a renamed or shared dir).

Strictly additive: an explicit entry always wins, so every shipped manifest keeps
its exact current behaviour.

Scope note: this is item D-1 of #1106. The other slimming items (kind enum
consolidation, `visibility`, flat `[requires]`, dropping `[runtime]`) and all of
E (asset removals) are separate — the issue stays open.
"""
from __future__ import annotations

import textwrap

import pytest

from cairn.core.pack.manifest import load_manifest

_BASE = '''
[pack]
name = "p"
version = "0.1.0"
description = "d"
'''


def _pack(tmp_path, body: str, dirs=(), name="p"):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    for sub in dirs:
        (d / sub).mkdir(parents=True, exist_ok=True)
    (d / "pack.toml").write_text(textwrap.dedent(body), encoding="utf-8")
    return load_manifest(d / "pack.toml")


# ── discovery ────────────────────────────────────────────────────────────────

def test_a_standard_dir_that_exists_is_discovered_without_declaration(tmp_path):
    m = _pack(tmp_path, _BASE, dirs=["templates", "schemas"])
    assert m.templates == "templates"
    assert m.schemas == "schemas"


def test_a_standard_dir_that_does_NOT_exist_stays_none(tmp_path):
    """Convention discovers what is there; it never invents a path."""
    m = _pack(tmp_path, _BASE, dirs=["templates"])
    assert m.templates == "templates"
    assert m.schemas is None
    assert m.rag is None


def test_every_conventional_kind_is_discovered(tmp_path):
    dirs = ["templates", "schemas", "flows", "rag", "evals", "examples",
            "skills", "policies"]
    m = _pack(tmp_path, _BASE, dirs=dirs)
    for kind in dirs:
        assert getattr(m, kind) == kind, f"{kind} not discovered"


# ── explicit always wins (back-compat) ───────────────────────────────────────

def test_an_explicit_entry_overrides_convention(tmp_path):
    m = _pack(tmp_path, _BASE + '\n[assets]\ntemplates = "tpl"\n',
              dirs=["templates", "tpl"])
    assert m.templates == "tpl", "a declared path must win over the standard dir"


def test_explicit_wins_even_when_the_standard_dir_exists_and_the_target_does_not(tmp_path):
    """A declared-but-missing dir must NOT silently fall back to convention —
    that would mask a typo by loading a different directory than the author
    named."""
    m = _pack(tmp_path, _BASE + '\n[assets]\ntemplates = "typo"\n',
              dirs=["templates"])
    assert m.templates == "typo"


def test_shipped_packs_keep_their_exact_resolution(tmp_path):
    """The guard that matters: convention must change NOTHING for the packs we
    ship, all seven of which declare [assets] today."""
    from cairn.core.pack import ensure_loaded
    from cairn.core.pack.registry import get_packs

    ensure_loaded()
    for pack in get_packs():
        m = getattr(pack, "manifest", None)
        if m is None:
            continue
        # every discovered value must name a REAL directory
        for kind in ("templates", "schemas", "flows", "rag", "evals", "examples"):
            val = getattr(m, kind, None)
            if val:
                assert isinstance(val, str) and val, f"{m.name}.{kind} empty"


# ── the point: a pack needs no [assets] at all ───────────────────────────────

def test_a_pack_with_no_assets_block_resolves_everything(tmp_path):
    """The headline: delete the block, keep the behaviour."""
    m = _pack(tmp_path, _BASE, dirs=["templates", "schemas", "evals"])
    assert (m.templates, m.schemas, m.evals) == ("templates", "schemas", "evals")


def test_an_empty_assets_block_is_still_legal(tmp_path):
    m = _pack(tmp_path, _BASE + "\n[assets]\n", dirs=["templates"])
    assert m.templates == "templates"


def test_a_file_named_like_a_standard_dir_is_not_discovered(tmp_path):
    """Only DIRECTORIES count — a stray `templates` file must not become an
    asset root."""
    d = tmp_path / "pf"
    d.mkdir()
    (d / "templates").write_text("not a dir", encoding="utf-8")
    (d / "pack.toml").write_text(textwrap.dedent(_BASE), encoding="utf-8")
    assert load_manifest(d / "pack.toml").templates is None


def test_unknown_assets_keys_are_still_rejected(tmp_path):
    """The closed schema stays closed — convention is not a licence to typo."""
    with pytest.raises(Exception):
        _pack(tmp_path, _BASE + '\n[assets]\nnonsense = "x"\n')
