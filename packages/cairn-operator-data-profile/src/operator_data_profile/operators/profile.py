"""data/profile — semantic data profiler (ADR-0074, tiers 1+2/3a).

Thin node over ``cairn.services.data.profile_data``: load the records (inline
sample OR a source), resolve the workflow's declared roles, and emit a
PASS / DEGRADE / BLOCK verdict with per-role coverage. ``side_effect_free`` so it
runs live in a Playground sandbox and as a cheap input-gate in real pipelines.
"""
from __future__ import annotations

from cairn.core.operator import Operation, OperationContext, ReportSpec, register_operator
from cairn.services.data import load_or_rows, profile_data

from operator_data_profile.schemas.profile import DataProfileInput, DataProfileOutput


@register_operator(name="data/profile", version="0.1.0", capabilities=["data.profile"],
                   implements=["contract://data.profile/1.0.0"])  # #2243 V2 — cairn-contracts-data
class DataProfileOp(Operation[DataProfileInput, DataProfileOutput]):
    """Resolve a workflow's data roles against the input → will it run here?"""

    input_schema = DataProfileInput
    output_schema = DataProfileOutput
    supports_mock = True
    side_effect_free = True  # reads + profiles in-memory; live in sandbox
    # ADR-0103: a thin, fast in-memory verdict over cairn.services.data — no
    # loop at this layer to report progress over, nothing to report.
    reports = ReportSpec.NONE

    async def run(self, inp: DataProfileInput, ctx: OperationContext) -> DataProfileOutput:
        records = load_or_rows(inp.source or None, list(inp.rows) or None, limit=inp.sample_limit)
        prof = profile_data(records, inp.expectation)
        return DataProfileOutput(
            ok=prof.ok, verdict=prof.verdict, record_count=prof.record_count,
            detected_format=prof.detected_format, missing_required=prof.missing_required,
            degraded=prof.degraded, errors=prof.errors, note=prof.note, roles=prof.roles,
        )

    def mock(self, inp: DataProfileInput, ctx: OperationContext) -> DataProfileOutput:
        return DataProfileOutput(
            ok=True, verdict="pass", record_count=len(inp.rows), detected_format="unknown",
            note="mock — profiling skipped", roles=[],
        )
