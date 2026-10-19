"""Every cairn environment variable the code reads has a row in the settings inventory (#2412).

``cairn/core/settings.py:INVENTORY`` is the one list of cairn's environment knobs
(``cairn/CLAUDE.md`` rule 4; ADR-0122 D3). Since the rename (ADR-0122 D1, #2429) the
names are ``CAIRN_*`` and the former ``OBS_*`` spelling is read only by
``settings.getenv``. This test reads the tree with ``ast`` (no cairn module is imported
except ``settings``) and fails:

* on a ``CAIRN_*`` name the code reads that has no inventory row and is not one of the
  :data:`NATIVE` names (born ``CAIRN_*``: the job/box environment cairn hands a worker,
  and knobs that never had an ``OBS_*`` spelling) — the message gives every ``file:line``;
* on an inventory row nothing reads (a dead row), unless its description starts with
  ``DEPRECATED``;
* on an ``OBS_*`` name anywhere in the code outside :data:`LEGACY_LITERALS` — the
  spelling is resolved in one place (ADR-0122 D2), never read directly.

What counts as a read: any exact name string literal in code outside a docstring. That
covers ``getenv`` / ``os.environ.get`` / ``environ[...]``, a parsed ``.env`` mapping's
``.get``, helper calls, module constants that hold a name, and the name tables that map
a profile key to its env var. A ``pydantic-settings`` field ``obs_<x>`` reads
``CAIRN_<X>`` (through its alias). A name that only appears inside a sentence (an error
message) is not a read. Names built at runtime are recorded as globs:
``f"CAIRN_FEATURE_{name.upper()}"`` is ``CAIRN_FEATURE_*``, and a prefix such as
``"CAIRN_METAFLOW_"`` is ``CAIRN_METAFLOW_*``. A glob is covered by any inventory row it
matches — a concrete row (``CAIRN_REALTIME_CONCURRENCY`` for ``CAIRN_*_CONCURRENCY``) or
a placeholder row (``CAIRN_FEATURE_<NAME>``).
"""

from __future__ import annotations

import pytest

from tests import _layout
from tests.core._env_scan import (  # noqa: F401 - the collector and its tables
    LEGACY, LEGACY_LITERALS, NATIVE, PREFIX, collect_reads, dead_rows, missing_rows,
    names_read_in_source, _inventory,
)

#: The public half (#2524): the engine, the operator packs present, and the client and SDK
#: sources. In the monorepo the engine directory also holds the domain and official packs,
#: so the union is checked there; the platform's own readers are in
#: ``test_env_inventory_platform.py``.
_CLIENTS = ("client/src", "sdk/src", "packages/cairn-client/src", "packages/cairn-sdk/src")
ROOTS = tuple(sorted({_layout.ENGINE, *(p / "src" for p in _layout.operator_packs() if (p / "src").is_dir()),
                      *(_layout.REPO / c for c in _CLIENTS if (_layout.REPO / c).is_dir()),
                      *((_layout.REPO / "ask" / "src",) if (_layout.REPO / "ask" / "src").is_dir() else ())}))
#: A row is dead only if NOTHING reads it, which only a tree holding every reader can say.
needs_every_reader = pytest.mark.skipif(not _layout.PRIVATE_TREES,
                                        reason="the platform's code reads some inventory rows")


# --------------------------------------------------------------------------- the gate


def _reads(prefix: str = PREFIX) -> dict[str, list[str]]:
    return collect_reads(ROOTS, prefix)


def _all_reads() -> dict[str, list[str]]:
    return {**_reads(), **_reads(LEGACY)}


def test_every_cairn_variable_the_code_reads_has_an_inventory_row():
    reads = {n: s for n, s in _reads().items() if n not in NATIVE}
    missing = missing_rows(reads, set(_inventory()))
    assert not missing, (
        f"{len(missing)} CAIRN_* name(s) are read but have no row in "
        "cairn/core/settings.py INVENTORY (cairn/CLAUDE.md rule 4, ADR-0122 D3):\n"
        + "\n".join(f"  {name}  read at {', '.join(sites)}" for name, sites in sorted(missing.items()))
    )


@needs_every_reader
def test_no_inventory_row_names_a_variable_nothing_reads():
    dead = dead_rows(_all_reads(), _inventory())
    assert not dead, (
        "inventory rows for cairn names nothing reads (delete the row, or start its "
        f"description with DEPRECATED): {dead}"
    )


def test_no_obs_name_is_read_outside_the_resolver():
    """ADR-0122 D2: ``settings.getenv`` is the one place the former spelling is read."""
    legacy = _reads(LEGACY)
    stray = {n: s for n, s in legacy.items() if n not in LEGACY_LITERALS}
    assert not stray, (
        "OBS_* names read directly — read CAIRN_<NAME> through cairn.core.settings.getenv "
        "(cairn.core.operator.getenv in an operator pack) instead:\n"
        + "\n".join(f"  {name}  at {', '.join(sites)}" for name, sites in sorted(stray.items()))
    )
    if not _layout.PRIVATE_TREES:
        return  # some documented literals are read by the platform's code
    assert set(LEGACY_LITERALS) <= set(legacy), (
        f"documented OBS_* literals no longer present (drop them): {set(LEGACY_LITERALS) - set(legacy)}")


def test_native_names_are_still_read_and_not_inventoried():
    """NATIVE only shrinks: a name the code stopped reading leaves it, and a name that gains
    an inventory row (and so an OBS_* alias) is no longer native."""
    reads = _reads()
    if _layout.PRIVATE_TREES:   # the box/job names are read by the platform's operator packs too
        assert NATIVE <= set(reads), sorted(NATIVE - set(reads))
    assert not NATIVE & set(_inventory()), sorted(NATIVE & set(_inventory()))


# --------------------------------------------------------------------------- the gate can fail


_SAMPLE = '''
"""Docstring naming CAIRN_DOCSTRING_ONLY is not a read."""
import os
LIMIT = getenv("CAIRN_DIRECT", "3")
FLAG = os.getenv("CAIRN_GETENV")
_ENV = "CAIRN_VIA_CONSTANT"
TABLE = {"engine": "CAIRN_IN_TABLE"}
PREFIXES = ("CAIRN_FORWARDED_",)
OLD = os.environ.get("OBS_STRAY")
class Settings(BaseSettings):
    obs_backend: str = "mock"
    openai_api_key: str = ""
def f(env, name, lane):
    """CAIRN_ALSO_DOCSTRING."""
    prefix = f"CAIRN_MCP_{name.upper()}_"
    getenv(prefix + "URL")
    getenv(f"CAIRN_FEATURE_{name.upper()}", environ=env)
    getenv(f"CAIRN_{lane.upper()}_CONCURRENCY")
    raise ValueError(f"CAIRN_IN_MESSAGE={name} is wrong")
    raise ValueError("set CAIRN_IN_SENTENCE first")
'''


def test_the_collector_sees_every_read_form_and_no_prose():
    found = {name for name, _ in names_read_in_source(_SAMPLE)}
    assert found == {
        "CAIRN_DIRECT", "CAIRN_GETENV", "CAIRN_VIA_CONSTANT", "CAIRN_IN_TABLE",
        "CAIRN_FORWARDED_*", "CAIRN_FEATURE_*", "CAIRN_*_CONCURRENCY", "CAIRN_MCP_*_URL",
        "CAIRN_BACKEND",
    }
    assert {name for name, _ in names_read_in_source(_SAMPLE, LEGACY)} == {"OBS_STRAY"}


def test_missing_and_dead_rows_can_fail():
    reads = {"CAIRN_A": ["x.py:1"], "CAIRN_FEATURE_*": ["y.py:2"], "CAIRN_*_CONCURRENCY": ["z.py:3"]}
    assert missing_rows(reads, {"CAIRN_A", "CAIRN_FEATURE_<NAME>", "CAIRN_REALTIME_CONCURRENCY"}) == {}
    assert missing_rows(reads, {"CAIRN_FEATURE_<NAME>"}) == {
        "CAIRN_A": ["x.py:1"], "CAIRN_*_CONCURRENCY": ["z.py:3"],
    }
    rows = {
        "CAIRN_A": "", "CAIRN_FEATURE_<NAME>": "", "CAIRN_REALTIME_CONCURRENCY": "",
        "CAIRN_GONE": "Nothing reads this.", "CAIRN_OLD": "DEPRECATED — kept for one release.",
        "SLACK_BOT_TOKEN": "",
    }
    assert dead_rows(reads, rows) == ["CAIRN_GONE"]


def test_every_source_root_exists():
    # A renamed directory would otherwise make the gate silently scan nothing.
    assert ROOTS and all(r.is_dir() for r in ROOTS), ROOTS
    assert _layout.ENGINE in ROOTS
