"""§24.3 P2.e — the compiler threads the owning pack's identity (``from_pack``)
into subgraph resolution.

This is the slice that makes "internal vs public template" real: a pack can
subgraph its OWN unlisted internal template (the within-pack visibility rule),
while that same template stays private to other packs. Before this, the
compiler resolved subgraph refs with ``from_pack=None`` (permissive), so the
within-pack rule could never fire and internals had to be exported to be used.

Covers:
  - ``pack_owning_template()`` — the reverse path→pack lookup the compiler uses.
  - Compiler integration: an orchestrator subgraphing its own *unlisted*
    internal via the namespaced self-form compiles; a *different* pack
    referencing that same private template fails to compile.

These tests use real registered packs (not the resolver monkeypatch in
``test_subgraph_mapping.py``) precisely so the live resolver runs with the
``from_pack`` the compiler derives from each template's path.
"""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.engine.compiler import compile_template
from cairn.core.pack import Pack, PackManifest, clear, register_pack
from cairn.core.pack.registry import pack_owning_template


# ─── fixture helpers ───────────────────────────────────────────────

_CHILD_BODY = (
    "---\n"
    "name: {name}\n"
    "version: 0.0.1\n"
    "flow:\n"
    "  type: prompt\n"
    "  model: gpt-4o-mini\n"
    "---\n"
    "\n"
    "# System\n"
    "child body.\n"
)


def _orchestrator_body(subgraph_ref: str) -> str:
    # A state_machine whose root is a single bare subgraph_node (no schema,
    # no inputs/outputs mapping → the backward-compat path, so
    # _validate_subgraph_mapping is a no-op and we isolate the *resolution*
    # behavior under test).
    return (
        "---\n"
        "name: orch\n"
        "version: 0.0.1\n"
        "flow:\n"
        "  type: state_machine\n"
        "  source: behavior_tree\n"
        "  root:\n"
        "    type: subgraph_node\n"
        f"    template: {subgraph_ref}\n"
        "  nodes: {}\n"
        "---\n"
        "\n"
        "# System\n"
        "orch body.\n"
    )


def _make_pack(
    name: str,
    root: Path,
    *,
    files: dict[str, str],
    provides_templates: list[str],
    encapsulated: bool = True,
) -> Pack:
    """Create ``<root>/<name>/templates/*.md`` and register a Pack for it.

    ``files`` maps local template name → file body. ``provides_templates`` is
    the manifest export list — it may intentionally omit files on disk, which
    is exactly how a template becomes *internal*.
    """
    tpl_dir = root / name / "templates"
    tpl_dir.mkdir(parents=True, exist_ok=True)
    for local, body in files.items():
        (tpl_dir / f"{local}.md").write_text(body, encoding="utf-8")
    manifest = PackManifest(
        name=name, version="0.0.1",
        provides_declared=encapsulated,
        provides_templates=list(provides_templates),
    )
    pack = Pack(
        name=name, version="0.0.1", description="",
        templates_dir=str(tpl_dir),
        manifest=manifest,
    )
    register_pack(pack)
    return pack


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    monkeypatch.delenv("OBS_PROFILE", raising=False)
    clear()
    yield
    clear()


# ─── pack_owning_template ──────────────────────────────────────────


def test_pack_owning_template_finds_owner(tmp_path):
    _make_pack(
        "alpha", tmp_path,
        files={"check": _CHILD_BODY.format(name="check")},
        provides_templates=["check"],
    )
    tpl = tmp_path / "alpha" / "templates" / "check.md"
    assert pack_owning_template(tpl) == "alpha"


def test_pack_owning_template_none_outside_any_pack(tmp_path):
    _make_pack(
        "alpha", tmp_path,
        files={"check": _CHILD_BODY.format(name="check")},
        provides_templates=["check"],
    )
    stray = tmp_path / "loose" / "templates" / "x.md"
    stray.parent.mkdir(parents=True)
    stray.write_text("---\nname: x\n---\n", encoding="utf-8")
    assert pack_owning_template(stray) is None


def test_pack_owning_template_none_when_empty_registry(tmp_path):
    p = tmp_path / "x.md"
    p.write_text("---\nname: x\n---\n", encoding="utf-8")
    assert pack_owning_template(p) is None


# ─── compiler threads from_pack: own private internal is reachable ──


def test_orchestrator_subgraphs_own_unlisted_internal(tmp_path):
    """A pack's orchestrator references its own internal template (NOT in
    [provides]) via the namespaced self-form. The compiler derives
    ``from_pack`` from the orchestrator's path, so the within-pack rule makes
    the internal visible → it compiles."""
    _make_pack(
        "alpha", tmp_path,
        files={
            "orch": _orchestrator_body("alpha/__helper"),
            "__helper": _CHILD_BODY.format(name="alpha-helper"),
        },
        provides_templates=["orch"],   # __helper intentionally NOT exported
    )
    orch = tmp_path / "alpha" / "templates" / "orch.md"
    compiled = compile_template(orch)
    assert compiled.spec.flow.type == "state_machine"


# ─── compiler threads from_pack: cross-pack private is blocked ──────


def test_other_pack_cannot_subgraph_private_internal(tmp_path):
    """A *different* pack referencing alpha's unlisted internal fails to
    compile: the compiler derives ``from_pack=beta`` from beta's orchestrator
    path, and ``alpha/__helper`` is private to alpha."""
    _make_pack(
        "alpha", tmp_path,
        files={
            "orch": _orchestrator_body("alpha/__helper"),
            "__helper": _CHILD_BODY.format(name="alpha-helper"),
        },
        provides_templates=["orch"],
    )
    _make_pack(
        "beta", tmp_path,
        files={"orch": _orchestrator_body("alpha/__helper")},
        provides_templates=["orch"],
    )
    beta_orch = tmp_path / "beta" / "templates" / "orch.md"
    with pytest.raises(FileNotFoundError, match="private"):
        compile_template(beta_orch)
