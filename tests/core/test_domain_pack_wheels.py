"""Every domain pack is its own DATA wheel, `cairn-pack-<name>` (#2420, ADR-0121).

The pack's `pyproject.toml` declares the pack's `cairn.packs` entry point and
force-includes the manifest, the shim and every asset dir under the import name
`cairn_pack_<dir>`; the root `pyproject.toml` declares no pack and installs each
wheel as a non-editable member of the `platform` extra, exactly like
`cairn-contracts-*`. A new pack dir, a new asset dir or a root entry point that
sneaks back fails here.

The example pack (`examples/hello-cairn`, import name `cairn_pack_hello`) has the same
wheel shape and is checked with them, so the public tree, which has no other domain pack,
proves the shape too (#2424). Only the platform's packs are installed by the root.
"""
from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import pytest

from tests import _layout

EXAMPLE = _layout.REPO / "examples" / "hello-cairn"
ROOT = tomllib.loads((_layout.REPO / "pyproject.toml").read_text())
#: Not pack content: process files, docs and the Python/packaging files themselves.
NOT_ASSETS = {"__pycache__", "__init__.py", "pack.toml", "pyproject.toml", "tests", "docs",
              "README.md", "CHANGELOG.md", "LICENSE", "DESIGN.md", ".gitignore"}


#: The platform's domain packs (none in the public tree).
DOMAIN_PACKS = sorted(d for d in _layout.DOMAIN_PACKS.iterdir() if (d / "pack.toml").is_file()) if _layout.DOMAIN_PACKS else []
WHEEL_PACKS = [EXAMPLE, *DOMAIN_PACKS]


def _import_name(d: Path) -> str:
    return "cairn_pack_hello" if d == EXAMPLE else f"cairn_pack_{d.name}"


def _project(d: Path) -> dict:
    return tomllib.loads((d / "pyproject.toml").read_text())


@pytest.mark.parametrize("d", WHEEL_PACKS, ids=lambda d: d.name)
def test_pack_wheel_declares_its_own_entry_point(d: Path):
    manifest = tomllib.loads((d / "pack.toml").read_text())["pack"]
    proj = _project(d)["project"]
    imp = _import_name(d)
    assert proj["name"] == "cairn-pack-" + imp.removeprefix("cairn_pack_").replace("_", "-")
    assert proj["version"] == manifest["version"]
    assert proj["entry-points"]["cairn.packs"] == {manifest["name"]: f"{imp}:pack"}


@pytest.mark.parametrize("d", WHEEL_PACKS, ids=lambda d: d.name)
def test_pack_wheel_force_includes_every_asset_dir(d: Path):
    """The wheel is the directory: a new asset dir on disk must be shipped too."""
    on_disk = {p.name for p in d.iterdir() if p.name not in NOT_ASSETS}
    inc = _project(d)["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert set(inc) == {"__init__.py", "pack.toml"} | on_disk, (
        f"force-include ≠ disk: missing {sorted(on_disk - set(inc))}, stale {sorted(set(inc) - on_disk - {'__init__.py', 'pack.toml'})}")
    assert all(v == f"{_import_name(d)}/{k}" for k, v in inc.items()), inc


@pytest.mark.parametrize("d", WHEEL_PACKS, ids=lambda d: d.name)
def test_pack_shim_builds_the_pack_without_the_cairn_packs_package(d: Path):
    """What the wheel installs is `cairn_pack_<dir>`, not `cairn.packs.<dir>`: the shim
    must build the Pack from the manifest beside it, wherever it is imported from."""
    name = _import_name(d)
    spec = importlib.util.spec_from_file_location(
        name, d / "__init__.py", submodule_search_locations=[str(d)])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.pack.name == tomllib.loads((d / "pack.toml").read_text())["pack"]["name"]


def test_root_declares_no_pack():
    assert "cairn.packs" not in ROOT["project"].get("entry-points", {}), "the root declares no pack (#2420)"


@pytest.mark.parametrize("d", DOMAIN_PACKS, ids=lambda d: d.name)
def test_root_installs_the_domain_pack_wheel(d: Path):
    name = _project(d)["project"]["name"]
    assert name in ROOT["project"]["optional-dependencies"]["platform"]
    assert f"cairn/packs/{d.name}" in ROOT["tool"]["uv"]["workspace"]["members"]
    assert ROOT["tool"]["uv"]["sources"][name] == {"workspace": True, "editable": False}, "data wheels install non-editable"
    assert "cairn/packs" in ROOT["tool"]["hatch"]["build"]["targets"]["wheel"]["exclude"], "the engine wheel ships no pack"


def test_every_pack_dir_ships_as_a_wheel():
    """A pack directory without a `pyproject.toml` is registered nowhere (the #2183 shape)."""
    assert not [d.name for d in WHEEL_PACKS if not (d / "pyproject.toml").is_file()]
