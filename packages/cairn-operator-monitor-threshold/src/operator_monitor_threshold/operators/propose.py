"""monitor/propose — the human-gated proposal a crossed watch files (#992, D4).

When a watch's signal crosses its threshold, the on-brand cairn behavior is to
**propose a run for a human to approve — never auto-spend** (ADR-0010/0012). This
operator declares ``requires_approval = true`` (pack.toml), so the durable gate
fires BEFORE it runs: the watch run pauses ``awaiting_approval`` with the proposal
(what workflow, the metric that crossed, est cost) surfaced on the approval card.
Approve → it records the approved proposal; reject → the watch ends without
spending. It does NOT itself dispatch the proposed run (that auto-dispatch-on-
approve is a tracked follow-up) — it is the governed decision point.
"""
from __future__ import annotations

from cairn.core.operator import (
    Operation,
    OperationContext,
    ReportSpec,
    register_operator,
)

from operator_monitor_threshold.schemas.propose import ProposeInput, ProposeOutput


@register_operator(
    name="monitor/propose",
    version="0.1.0",
    capabilities=["monitor.propose"],
    implements=["contract://monitor.propose/1.0.0"],  # #2242 V1-E — cairn-contracts-monitor
)
class ProposeOp(Operation[ProposeInput, ProposeOutput]):
    """Record a threshold-crossing proposal — gated behind a human approval."""

    input_schema = ProposeInput
    output_schema = ProposeOutput
    # #1539 — governance declarations; the [operator.*] table is generated from these
    requires_approval = True
    reports = ReportSpec.NONE  # ADR-0103: records one already-decided proposal, nothing to report

    async def run(self, inp: ProposeInput, ctx: OperationContext) -> ProposeOutput:
        # Reaching run() means the approval gate passed (requires_approval fired
        # before this node). Record the approved proposal.
        return ProposeOutput(
            metric=inp.metric, value=inp.value, threshold=inp.threshold,
            proposed_workflow=inp.proposed_workflow, est_cost_usd=inp.est_cost_usd,
            approved=True,
        )
