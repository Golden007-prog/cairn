"""artifact/promote — moving a governed tag, as a GATED run (ADR-0081 P2b, #1052).

Promotion is the moment a data product becomes the one everything downstream
consumes, so it is the classic HITL external write. This operator reuses the
PROVEN chokepoint rather than inventing a second approval path: ``pack.toml``
declares ``requires_approval = true``, so the durable gate fires BEFORE the
tag moves (the ``model/register`` pattern, ADR-0034) — the run pauses in the
existing gate queue, a human approves, and only then does the tag change.

Why an operator and not a plain endpoint: cairn has NO generic pending-approval
store — approvals are run-scoped (`interrupt()` + `awaiting_approval`). Making
promotion a run reuses that whole machinery (queue · durable pause · resume ·
audit · lineage) instead of building a parallel one (#744 discipline, and the
verify-upstream anti-pattern table's "hand-coded HITL mechanism").
"""
from __future__ import annotations

from typing import Optional

from cairn.core.operator import Operation, OperationContext, ReportSpec, register_operator
from cairn.core.operator import ArtifactRegistry, build_artifact_store

from operator_dataflow.schemas.promote import ArtifactPromoteInput, ArtifactPromoteOutput

_store_instance: Optional[ArtifactRegistry] = None


def _store() -> ArtifactRegistry:
    global _store_instance
    if _store_instance is None:
        _store_instance = build_artifact_store()
    return _store_instance


def _reset_store(store: Optional[ArtifactRegistry] = None) -> None:
    """Test seam."""
    global _store_instance
    _store_instance = store


@register_operator(
    name="artifact/promote",
    version="0.1.0",
    capabilities=["artifact.promote"],
    implements=["contract://artifact.promote/1.0.0"],  # #2242 V1-E — cairn-contracts-artifact
)
class ArtifactPromoteOp(Operation[ArtifactPromoteInput, ArtifactPromoteOutput]):
    """Point a governed tag at a registered artifact (gated by pack.toml)."""

    input_schema = ArtifactPromoteInput
    output_schema = ArtifactPromoteOutput
    # #1539 — governance declarations; the [operator.*] table is generated from these
    risk = 'external_write'
    requires_approval = True
    supports_mock = True
    reports = ReportSpec.NONE  # ADR-0103: one tag move, nothing to report

    async def run(self, inp: ArtifactPromoteInput, ctx: OperationContext) -> ArtifactPromoteOutput:
        store = _store()
        # what the tag pointed at BEFORE — captured so the report (and a human
        # reading the audit trail) can see what this promotion displaced, and
        # so a rollback has an explicit target.
        previous = await store.resolve(inp.tag)
        rec = await store.promote(
            inp.tag, inp.artifact_id, actor=str(getattr(ctx, "pipeline_id", "") or "run"),
        )
        return ArtifactPromoteOutput(
            tag=inp.tag, artifact_id=rec.id, uri=rec.uri, promoted=True,
            previous_artifact_id=previous.id if previous else "",
        )

    def mock(self, inp: ArtifactPromoteInput, ctx: OperationContext) -> ArtifactPromoteOutput:
        return ArtifactPromoteOutput(
            tag=inp.tag, artifact_id=inp.artifact_id, promoted=False,
            uri="mock://not-promoted",
        )
