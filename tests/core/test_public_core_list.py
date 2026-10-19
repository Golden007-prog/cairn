"""`.circleci/public_core_tests.txt` is the positive list of tests/core files the CircleCI
`public` job runs on the public set alone (ADR-0120 D1, #2424). This keeps it well-formed:
every listed file exists, the list is sorted and unique, and the boundary suites that define
the public core are on it. Whether each file PASSES on the public set is CI's proof."""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LIST = REPO / ".circleci" / "public_core_tests.txt"
MUST = {"test_public_core_boundary.py", "test_architecture_boundaries.py", "test_router_providers.py",
        "test_model_catalogs.py", "test_domain_pack_wheels.py", "test_operator_contract_reexports.py"}


def _listed() -> list[str]:
    return [l.strip() for l in LIST.read_text().splitlines() if l.strip() and not l.startswith("#")]


def test_every_listed_file_exists():
    missing = [f for f in _listed() if not (REPO / "tests" / "core" / f).is_file()]
    assert not missing, f"delete from the list: {missing}"


def test_the_list_is_sorted_and_unique():
    files = _listed()
    assert files == sorted(set(files))


def test_the_boundary_suites_are_on_the_list():
    assert MUST <= set(_listed())


def test_a_held_back_file_is_not_also_listed():
    held = {l.split(":")[0].strip("# ").strip() for l in LIST.read_text().splitlines() if l.startswith("#   test_")}
    assert held and not held & set(_listed())
