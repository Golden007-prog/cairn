"""The engine has exactly ONE ref normaliser (#2126), wherever it is checked out (#2524).

Split from ``test_ref_key.py``, which needs the console's shared case table and the
platform's packs: this scan reads only the engine and the operator packs present, through
``tests/_layout.py``, so it runs on the public list. Paths are named in the monorepo
spelling in both layouts (``_layout.rel``), so the allowlist needs no rewrite.
"""
from __future__ import annotations

import re

from tests import _layout


#: A ref/pack normaliser is `_` → `-` on something ref-shaped. Files that own
#: the one implementation, plus the places the transformation means something
#: else entirely (a python identifier, an env var, a provider name).
_ALLOWED = {
    "cairn/core/refs.py",                       # THE implementation
    "cairn/core/oauth/broker.py",               # PROVIDER → env var name
    "cairn/core/engine/compiler.py",            # ref → python identifier
    "cairn/core/graph_view.py",                 # ref → compiler node id
    "cairn/cli/commands/init.py",               # scaffolding a NEW pack's name
    "cairn/cli/commands/export.py",             # ref → module path
    # connection name → env var; bucket label → metric key. Not refs.
    "cairn/operators/optional/dataflow_metaflow/src/operator_dataflow_metaflow/engines_metaflow.py",
    "cairn/operators/official/dataset_checks/src/operator_dataset_checks/dataflow_stages.py",
}
_NORMALISER = re.compile(r"""replace\(\s*["']_["']\s*,\s*["']-["']\s*\)"""
                         r"""|replace\(\s*["']-["']\s*,\s*["']_["']\s*\)""")


def test_the_engine_has_exactly_ONE_ref_normaliser():
    """The acceptance criterion, as a gate. This ticket exists because the
    normalisation lived in a router (``flow_key``) and in a resolver
    (``resolve_flow_key``) and in a listing (``cairn ls``) and in a contract
    reader (``ui_contract``) — four copies agreeing by hand until one of them
    was asked a question the others had already answered.

    Fails if a fifth copy is written, and fails if ``cairn/core/refs.py`` is
    deleted (nothing would be left to delegate to)."""
    assert (_layout.ENGINE / "core" / "refs.py").is_file(), \
        "the one normaliser is gone — every reader below it now agrees by hand"
    offenders = []
    files = [*_layout.ENGINE.rglob("*.py"), *(f for p in _layout.operator_packs() for f in p.rglob("*.py"))]
    for py in sorted(set(files)):
        rel = _layout.rel(py)
        if rel in _ALLOWED or "/tests/" in rel:
            continue
        for n, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            # A COMMENT that quotes the pattern is documentation, not a second
            # normaliser — and flagging one teaches the next reader to describe
            # the rule less clearly rather than to follow it. (Caught by this
            # gate firing on a comment that said "a hand-rolled `.replace(…)`
            # here was the ninth copy": the code beside it was already correct.)
            if line.lstrip().startswith("#"):
                continue
            if _NORMALISER.search(line):
                offenders.append(f"{rel}:{n}: {line.strip()}")
    assert offenders == [], (
        "a second ref normaliser — import `ref_key`/`pack_key` from "
        "cairn.core.refs instead (#2126):\n  " + "\n  ".join(offenders))
