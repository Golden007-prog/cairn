"""Refuse a venv that holds more than the public set (ADR-0120 D1, #2424; ADR-0134 D1).

The public core is the root wheel's base dependencies: the engine, the builtin
operator packs and the contracts whose providers ship in the core, `cairn-client`,
`cairn-sdk`. CI installs exactly that (`uv sync --extra dev`, no `platform`, `ssh`
or `ask`) and runs this first, so a test that passes afterwards passed without a
domain pack, an official operator pack, a platform contract, `cairn-ask` or
asyncssh being importable. Exit 1 names every private distribution found.

`--extras a,b` names the root extras the CI installed on top. An optional operator
pack (ADR-0134 D1: `cairn/operators/optional/`, offered only through an extra) is
accepted only when one of those extras names it; nothing else is admitted by an extra.
"""
from __future__ import annotations

import argparse
import re
import sys
import tomllib
from collections.abc import Iterable
from importlib.metadata import distributions
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Distribution-name prefixes this repository's private set uses.
PRIVATE_PREFIXES = ("cairn-operator-", "cairn-contracts-", "cairn-pack-")
#: Distributions that are extras, never part of the base install.
PRIVATE_NAMES = {"cairn-ask", "asyncssh", "cairn-platform"}
#: What a selected extra may admit: an optional operator pack, nothing else (ADR-0134 D1).
EXTRA_ADMITS = "cairn-operator-"


def _norm(name: str) -> str:
    return name.lower().replace("_", "-")


def _req_name(req: str) -> str:
    return _norm(re.split(r"[\[<>=!~;\s]", req.strip(), maxsplit=1)[0])


def _project(root: Path) -> dict:
    return tomllib.loads((root / "pyproject.toml").read_text())["project"]


def public_set(root: Path = ROOT) -> set[str]:
    """The sibling distributions the root wheel depends on unconditionally."""
    return {_req_name(d) for d in _project(root)["dependencies"]}


def extra_set(extras: Iterable[str], root: Path = ROOT) -> set[str]:
    """The optional operator packs the selected root ``extras`` name. An extra the root
    does not declare is an error: a typo must not pass for an empty selection."""
    table = _project(root).get("optional-dependencies", {})
    unknown = sorted(set(extras) - set(table))
    if unknown:
        raise SystemExit(f"check_public_set: the root declares no extra {', '.join(unknown)}")
    workspace = _workspace_sources(root)
    return {n for e in extras for n in map(_req_name, table[e])
            if n.startswith(EXTRA_ADMITS) and n in workspace}


def _workspace_sources(root: Path = ROOT) -> set[str]:
    """The distributions the root builds from its own tree (``[tool.uv.sources]`` with
    ``workspace = true``). An extra admits an operator pack only when it is one of these —
    a public optional pack — never a name that merely shares the prefix (#2682 review)."""
    sources = tomllib.loads((root / "pyproject.toml").read_text()).get("tool", {}).get("uv", {}).get("sources", {})
    return {_norm(k) for k, v in sources.items() if isinstance(v, dict) and v.get("workspace")}


def private_installed(extras: Iterable[str] = (), root: Path = ROOT,
                      installed: Iterable[tuple[str, str]] | None = None) -> list[str]:
    """``installed`` (name, version) pairs, default the running venv's distributions."""
    allowed = public_set(root) | extra_set(extras, root)
    if installed is None:
        installed = ((d.metadata["Name"] or "", d.version) for d in distributions())
    found = []
    for raw, version in installed:
        name = _norm(raw)
        if name in allowed:
            continue
        if name in PRIVATE_NAMES or name.startswith(PRIVATE_PREFIXES):
            found.append(f"{name} {version}")
    return sorted(set(found))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--extras", default="", help="the root extras installed on top of the base set, comma-separated")
    a = ap.parse_args(argv)
    extras = [e.strip() for e in a.extras.split(",") if e.strip()]
    found = private_installed(extras, ROOT)
    if found:
        print("not a public-set venv — private distributions installed:", file=sys.stderr)
        for f in found:
            print(f"  {f}", file=sys.stderr)
        return 1
    print("public set only: no private distribution installed"
          + (f" (extras: {', '.join(extras)})" if extras else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
