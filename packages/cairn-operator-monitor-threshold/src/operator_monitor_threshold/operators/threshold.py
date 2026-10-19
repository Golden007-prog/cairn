"""monitor/threshold — the generic signal/threshold detector (#992, ADR-0076 D4).

Reads a numeric metric from a pluggable source (inline value / JSON file / HTTP
GET) and compares it to a threshold. Zero-dependency (stdlib `json` + `urllib`),
so the base install can do data-shaped / drift triggers with no vendor SDK. The
`crossed` flag is the trigger condition — a scheduled WATCH skill runs this and,
when crossed, pauses for approval (a human-gated proposed run, ADR-0010/0012).

Vendor drift detectors (Evidently / NannyML / whylogs) provide the SAME
`monitor.threshold` capability as official packs + extras; this is the builtin
default. read_only: it observes a signal, never mutates.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any

from cairn.core.operator import (
    Operation,
    OperationContext,
    ReportSpec,
    register_operator,
)

from operator_monitor_threshold.source_guard import check_path, check_url
from operator_monitor_threshold.schemas.threshold import (
    ThresholdInput,
    ThresholdOutput,
)

_COMPARATORS = {
    "gt": lambda v, t: v > t,
    "gte": lambda v, t: v >= t,
    "lt": lambda v, t: v < t,
    "lte": lambda v, t: v <= t,
}


def _dig(obj: Any, dotted: str) -> Any:
    """Walk a dotted key path into nested JSON (e.g. 'metrics.psi')."""
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(f"key {dotted!r} not found (missing {part!r})")
        cur = cur[part]
    return cur


def _read_value(inp: ThresholdInput) -> tuple[float, str]:
    """Resolve the metric value + where it came from. Priority: inline > file > http.

    #1657 — the path and the URL come from the console's New-watch form, so both
    are checked before they are opened. Unconstrained they let any console user
    make the ENGINE read any file it can read and reach any address it can
    reach. See :mod:`operator_monitor_threshold.source_guard`.
    """
    if inp.value is not None:
        return float(inp.value), "inline"
    if inp.source_path:
        path = check_path(inp.source_path)
        with open(path, encoding="utf-8") as f:
            return float(_dig(json.load(f), inp.source_key)), "file"
    if inp.source_url:
        # stdlib only (no httpx dep in the builtin). Bounded timeout.
        url = check_url(inp.source_url)
        with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310 — scheme + address checked by check_url
            return float(_dig(json.loads(resp.read().decode("utf-8")), inp.source_key)), "http"
    raise ValueError("no signal source: set one of value / source_path / source_url")


@register_operator(
    name="monitor/threshold",
    version="0.1.0",
    capabilities=["monitor.threshold"],
    implements=["contract://monitor.threshold/1.0.0"],  # #2242 V1-E — cairn-contracts-monitor
)
class ThresholdOp(Operation[ThresholdInput, ThresholdOutput]):
    """Read a metric and flag whether it crossed a threshold."""

    input_schema = ThresholdInput
    output_schema = ThresholdOutput
    # #1539 — governance declarations; the [operator.*] table is generated from these
    supports_mock = True
    reports = ReportSpec.NONE  # ADR-0103: one read + compare, nothing to report

    async def run(self, inp: ThresholdInput, ctx: OperationContext) -> ThresholdOutput:
        cmp = _COMPARATORS.get(inp.comparator)
        if cmp is None:
            raise ValueError(f"comparator must be one of {sorted(_COMPARATORS)}, got {inp.comparator!r}")
        value, source = _read_value(inp)
        crossed = bool(cmp(value, inp.threshold))
        return ThresholdOutput(
            metric=inp.metric, value=value, threshold=inp.threshold,
            comparator=inp.comparator, crossed=crossed, source=source,
        )

    def mock(self, inp: ThresholdInput, ctx: OperationContext) -> ThresholdOutput:
        # supports_mock: a secretless CI / dry-run gets a deterministic "crossed"
        # so a watch template's downstream path is exercisable without a live signal.
        val = float(inp.value if inp.value is not None else inp.threshold + 1.0)
        cmp = _COMPARATORS.get(inp.comparator, _COMPARATORS["gt"])
        return ThresholdOutput(
            metric=inp.metric, value=val, threshold=inp.threshold,
            comparator=inp.comparator, crossed=bool(cmp(val, inp.threshold)), source="mock",
        )
