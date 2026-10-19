"""monitor/dispatch — launch what a human approved (#1162, closing #992's loop).

`monitor/propose` is the governed decision point: it pauses the watch at an
approval gate and records the verdict. Until now nothing acted on that verdict,
so "approved" meant a human then went and started the run by hand — the loop
was open at its last inch.

This is an explicit STEP, not an ambient subscriber watching for approved runs.
Dispatch is the moment cairn spends money because a number moved, so it belongs
in the flow where it can be read, gated, audited and tested like any other leg —
not in a listener that fires invisibly.

Two properties make it safe rather than an auto-spend hatch:

* it launches ONLY when `approved` is true — a rejected proposal is a no-op that
  says so, and the watch ends without spending;
* the launched run keeps its OWN governance. This authorises *starting* it; the
  run's cost gate, policy and approvals all still apply. One approval does not
  become a blanket exemption for everything downstream of it.

The launch itself goes through ``ctx.launch`` (ADR-0120 D7, #2455) — the dispatch
layer's handle over the launch door — never a dispatcher this pack builds.
"""
from __future__ import annotations

from cairn.core.operator import (
    Operation,
    OperationContext,
    ReportSpec,
    register_operator,
)

from operator_monitor_threshold.schemas.dispatch import DispatchInput, DispatchOutput

@register_operator(
    name="monitor/dispatch",
    version="0.1.0",
    capabilities=["monitor.dispatch"],
    implements=["contract://monitor.dispatch/1.0.0"],  # #2242 V1-E — cairn-contracts-monitor
)
class DispatchOp(Operation[DispatchInput, DispatchOutput]):
    """Launch the approved proposal — a pipeline or a template."""

    input_schema = DispatchInput
    output_schema = DispatchOutput
    # #1539 — governance declarations; the [operator.*] table is generated from these
    supports_mock = True
    risk = 'paid_compute'
    reports = ReportSpec.NONE  # ADR-0103: one launch call, nothing to report

    async def run(self, inp: DispatchInput, ctx: OperationContext) -> DispatchOutput:
        if not inp.approved:
            return DispatchOutput(
                dispatched=False, reason="proposal was not approved — nothing launched")
        ref = (inp.proposed_workflow or "").strip()
        if not ref:
            return DispatchOutput(
                dispatched=False, reason="approved, but no proposed_workflow to launch")

        # provenance: a run that exists because a metric moved must be traceable
        # to that metric, the threshold, and the estimate that was authorised.
        trigger = {
            "source": "watch", "metric": inp.metric, "value": inp.value,
            "threshold": inp.threshold, "approved_est_usd": inp.est_cost_usd,
        }

        # ADR-0120 D7 (#2455): the ONE way an operator starts governed work — the
        # dispatch layer's launch handle (admission, policy gates and budget apply;
        # `pipeline:<ref>` takes the launch door, any other ref the template door).
        # Absent handle = not inside a governed run (dev loop, tests): nothing is
        # launched, and this operator never builds a dispatcher of its own.
        if ctx.launch is None:
            return DispatchOutput(
                dispatched=False, launched_ref=ref,
                reason="no launch handle in this context — monitor/dispatch launches only "
                       "inside a governed run")
        run_id = await ctx.launch(ref, dict(inp.inputs or {}), trigger)
        return DispatchOutput(dispatched=True, launched_ref=ref, run_id=run_id)
