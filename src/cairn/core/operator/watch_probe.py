"""The registered-dataset change probe and the probe result shape (#1976, ADR-0131 D2).

Core's: one copy of "did this registered dataset gain a version", read by the
scheduler's public ``dataset`` watch (ADR-0131 D3) and by the platform's
``ingest/watch`` operator, which adds the ``s3`` and ``table`` probes on the same
:class:`Probe` shape.

**A probe never advances the marker.** It reports the marker the caller should
store once the change has been acted on; the caller stores it. A check that dies in
between repeats itself rather than skipping a change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Probe:
    """What one look at a source found."""

    changed: bool = False
    #: How much is new, in the source's own unit (versions / objects / rows).
    value: float = 0.0
    #: What to store ONCE the change has been admitted. Never stored by the probe.
    marker: dict[str, Any] = field(default_factory=dict)
    #: What the import should read. Empty when there is nothing to admit.
    import_path: str = ""
    #: The watermark to hand ``ingest/pull`` as ``since`` (table sources).
    since: str = ""
    truncated: bool = False
    reason: str = ""
    summary: str = ""


def probe_dataset(name: str, versions: list[dict], marker: dict) -> Probe:
    """A registered dataset gained versions.

    Nothing is imported: these bytes went through admission when the version was
    registered, and re-importing an already-governed dataset would create a
    second version of identical content for no reason. The change is the signal;
    the downstream workflow is the point.
    """
    latest = max((int(v.get("version") or 0) for v in versions), default=0)
    seen = int((marker or {}).get("version") or 0)
    if latest <= seen:
        return Probe(
            reason=f"dataset {name} is still at v{latest}",
            summary=f"dataset {name} unchanged at v{latest}.",
            marker={},
        )
    return Probe(
        changed=True, value=float(latest - seen), marker={"version": latest},
        summary=(
            f"dataset {name} moved v{seen or '—'} → v{latest} "
            f"({latest - seen} new version(s)); already admitted, nothing to import."
        ),
    )


__all__ = ["Probe", "probe_dataset"]
