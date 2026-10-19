"""Demo tiers, read from the flow that owns them (ADR-0106 D5, #2134 S2).

A ``[[demo."<flow>"]]`` block is *per-flow sample data* — the inputs the "Try
with sample data" control drops into a launch form, plus the golden that scores
that tier. ``pack.toml`` states **facts about the artifact** (identity, compat,
asset dirs, the operator catalogue); a specific dataset for a specific flow is
not one of them, and D4's test — *which file is responsible for this?* — puts it
next to the flow. So a flow declares its own::

    ---                                  # an agentic flow: YAML front matter
    name: check
    demos:
      - tier: easy
        title: "Clean bundled sample — PASSES the gate"
        inputs: {path: examples/sample.jsonl}
        eval: {uses: eval/subset, inputs: {...}}
    ---

    name: intake                         # a data/ml flow: the mapping itself
    kind: data
    demos:
      - tier: easy
        inputs: {path: examples/sample.jsonl}

**One shape, two files, on purpose.** The block is identical in both; only the
enclosing syntax differs, because that is the difference between the two flow
formats and inventing a second spelling for the data/ml case would give the
launch dialog two things to learn. Every shipped demo happens to belong to an
agentic flow today, so the ``.yaml`` half is exercised by fixtures rather than
by a pack — it exists so that the wrapper of a data/ml flow can inherit tiers
the same way, which is what D5 promises.

**Read from both places for one release.** :func:`demos_for` prefers the flow's
own block and falls back to ``pack.toml``'s ``[demo.*]``, warning once per pack.
A big-bang would have broken any third-party pack the moment they upgraded the
engine, to save one release of a fifteen-line fallback.

The parse itself is the manifest's, verbatim: same closed key set, same tier
enum, same ``[demo.*.eval]`` recipe parser, same errors. A second parser is how
``tier: easy`` ends up meaning one thing in ``pack.toml`` and another in a flow.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from cairn.core.pack.manifest import DemoSpec, ManifestError, _parse_one_demo

logger = logging.getLogger(__name__)

#: The key a flow declares its tiers under, in front matter or in its mapping.
DEMOS_KEY = "demos"

#: Packs already warned about the legacy location, so the deprecation speaks
#: once per pack per process rather than once per contract build.
_WARNED_LEGACY_DEMOS: set[str] = set()


def flow_file_of(pack: Any, flow: str) -> Optional[Path]:
    """The file that declares ``flow`` in ``pack``, or ``None``.

    Agentic first, then data/ml — a tie is impossible on a pack that passed the
    D9 lint, and preferring the agentic file on one that did not matches what
    the contract builder resolves.
    """
    from cairn.core.pack.loader import flow_files_of  # noqa: PLC0415 - cycle

    files = [f for f in flow_files_of(pack) if f.stem == flow]
    for f in files:
        if f.suffix == ".md":
            return f
    return files[0] if files else None


def flow_demos(pack: Any, flow: str) -> list[DemoSpec]:
    """The tiers ``flow`` declares in its own file. ``[]`` when it declares none.

    Raises :class:`~cairn.core.pack.manifest.ManifestError` on a malformed block
    — the same error, naming the same keys, that ``pack.toml`` would have raised
    — because a demo that silently does not load is a launch dialog that quietly
    loses its samples.
    """
    from cairn.core.pack.loader import flow_front_matter  # noqa: PLC0415 - cycle

    path = flow_file_of(pack, flow)
    if path is None:
        return []
    raw = flow_front_matter(path).get(DEMOS_KEY)
    if raw in (None, [], {}):
        return []
    entries = raw if isinstance(raw, list) else [raw]
    return [_parse_one_demo(flow, e, path) for e in entries]


def manifest_demos(pack: Any, flow: str) -> list[DemoSpec]:
    """The tiers ``pack.toml`` declares for ``flow`` — the SUPERSEDED location."""
    manifest = getattr(pack, "manifest", None)
    return list((getattr(manifest, "demos", None) or {}).get(flow) or [])


def demos_for(pack: Any, flow: str) -> list[DemoSpec]:
    """Every declared tier for ``flow``: its own block, else ``pack.toml``.

    Not merged. Two files listing tiers for one flow is the ownership question
    D5 exists to settle, so the flow WINS outright and the manifest's copy is
    ignored — a merge would make the effective tier list a thing neither file is
    responsible for, which is the shape of bug ADR-0106 D4 is about.
    """
    if own := flow_demos(pack, flow):
        return own
    legacy = manifest_demos(pack, flow)
    if legacy:
        name = getattr(pack, "name", "?")
        if name not in _WARNED_LEGACY_DEMOS:
            _WARNED_LEGACY_DEMOS.add(name)
            logger.warning(
                "pack %r declares [demo.%s] in pack.toml — demo tiers belong to "
                "the flow (ADR-0106 D5). Move the block to a `demos:` list in "
                "the flow file; pack.toml is read for one more release.",
                name, flow,
            )
    return legacy


def demo_flows(pack: Any) -> dict[str, list[DemoSpec]]:
    """``{flow name: tiers}`` for every flow in ``pack`` that declares any.

    The listing view (``cairn eval-recipe``, the catalog card) needs the whole
    set, and after D5 that set is spread across the flow files rather than
    sitting in one table.
    """
    from cairn.core.pack.loader import flow_files_of  # noqa: PLC0415 - cycle

    out: dict[str, list[DemoSpec]] = {}
    for f in flow_files_of(pack):
        if specs := flow_demos(pack, f.stem):
            out[f.stem] = specs
    manifest = getattr(pack, "manifest", None)
    for flow, specs in (getattr(manifest, "demos", None) or {}).items():
        out.setdefault(flow, list(specs))
    return out


__all__ = [
    "DEMOS_KEY",
    "ManifestError",
    "demo_flows",
    "demos_for",
    "flow_demos",
    "flow_file_of",
    "manifest_demos",
]
