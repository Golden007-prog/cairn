"""#1293 — pack.toml of INSTALLED packs reaches governance.

Before this, `discover_operator_pack_manifests` walked only the in-tree
shipped tiers: every wheel/editable-installed pack (community, or SDK-built
via #1279) registered its ops with NO manifest — no risk class, invisible to
risk-keyed policy, and two env-dependent test reds whenever such a pack was
present. The walk now covers entry-point-backed distributions, wheel layout
first with the source/editable fallback (the #1279 shim's resolution order).
"""
from __future__ import annotations

import os
import sys
from importlib.metadata import EntryPoint

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import cairn.core.operator.loader as loader_mod
from cairn.core.operator.loader import discover_operator_pack_manifests
from cairn_sdk import compile_pipeline, pipeline, step


def _install_fake_pack(tmp_path, monkeypatch, pack: str):
    """Build an SDK pack (source layout: pack.toml at root, src/<pkg>/) and
    surface it EXACTLY as an installed dist would: an entry point whose module
    resolves into the pack's src tree."""

    @pipeline(series="m", pack=pack)
    class M:
        @step(risk="read_only")
        def probe(self):
            return {"ok": True}

    compiled = compile_pipeline(M)
    pack_dir = tmp_path / pack
    compiled.write(pack_dir)

    pkg = compiled._pkg
    monkeypatch.syspath_prepend(str(pack_dir / "src"))
    sys.modules.pop(pkg, None)          # a stale import must not shadow tmp_path

    real_entry_points = loader_mod.entry_points
    fake = EntryPoint(
        name=f"{pack}/_steps/{compiled.name}.probe",
        value=f"{pkg}.{compiled._steps_module_name}:Lifted_probe",
        group="cairn.operators",
    )

    def _eps(*, group):
        return list(real_entry_points(group=group)) + (
            [fake] if group == "cairn.operators" else [])

    monkeypatch.setattr(loader_mod, "entry_points", _eps)
    return compiled


def test_installed_pack_manifest_is_discovered_with_risk(tmp_path, monkeypatch):
    compiled = _install_fake_pack(tmp_path, monkeypatch, "m1293-demo")
    out = discover_operator_pack_manifests()
    op = compiled.operators[0]["name"]
    assert op in out, "installed pack's manifest entries must be discovered"
    assert out[op].risk == "read_only"   # governance can key on it


def test_shipped_tier_entries_survive_the_installed_walk(tmp_path, monkeypatch):
    """First-wins: the in-tree walk stays authoritative — an installed pack
    can never overwrite a shipped op's declared metadata."""
    before = discover_operator_pack_manifests()
    _install_fake_pack(tmp_path, monkeypatch, "m1293-other")
    after = discover_operator_pack_manifests()
    shipped = {k: v for k, v in before.items() if not k.startswith("m1293")}
    for name, entry in shipped.items():
        assert after[name] is not None and after[name].risk == entry.risk
