"""The three service doors, asked on the PIPELINE surface (#2101, ADR-0099 §1).

ADR-0099 §1 says a requirement implied by configuration is DERIVED, refused
**before any record exists**, with the remedy named — and never auto-started.
Three doors already do exactly that: :mod:`cairn.core.job_channel_door`,
:mod:`cairn.core.dataflow.engine_door` (the flow-engine door, ADR-0134 D3c; it was the
SkyPilot door of ADR-0104 D5) and :mod:`cairn.core.pull_worker_door`.

They were asked in exactly two places — ``RunDispatcher.dispatch`` (template
runs) and ``preflight.build_preflight`` (dry run) — and a pipeline ``flow:`` leg
takes NEITHER path: :meth:`PipelineBridge._submit_flow_leg` calls
``resolve_engine()`` / ``engine.submit()`` directly. So on 2026-09-08 two
launches that had to be refused were admitted with HTTP 200 and died late at
submit, after the pipeline record, the leg record and the run all existed
(#2101). Every fine-tune pipeline is a flow leg, which makes this the surface
that matters most. This module is the missing ask; it adds no new derivation.

**Nothing here re-implements a door.** The doors are pure functions of
configuration plus store state and they own their own remedies; this module only
answers the two questions a door needs that only a *plan* can answer:

* **Which operators can this pipeline invoke?** A ``task:`` leg names one; a
  template leg is walked by ``collect_template_operators`` (what the dispatcher
  passes); a ``flow:`` leg's submission IS an operator — the kind-matched door
  ADR-0082 D7 defines (``ml/run`` for ``kind: ml``, ``dataflow/run`` for
  ``kind: data``), the same map the compiler reads to gate destructive flows.
  Both of those door operators implement ``submit``, so the job-channel and
  pull-worker doors recognise a flow leg as a remote job with no new predicate.
* **Which flow kinds will resolve an engine?** Only the kinds the pipeline's
  legs actually reach, so a data-only pipeline is not refused because
  ``ml_engine`` names an engine none of its legs will ever construct.

The engine's own late ``EngineError`` stays exactly as it is: this is a second,
earlier answer, not a replacement — the door cannot see a leg the plan does not
name, and a pipeline may be resumed long after it was admitted.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from cairn.core.dataflow.engine_door import ENGINE_OPERATORS

logger = logging.getLogger("cairn.core.dispatch.pipeline_doors")

#: flow kind → the operator that IS its submission (ADR-0082 D7, #1074). The
#: same pairing ``cairn.core.engine.compiler`` reads to gate a destructive flow
#: structurally; a flow leg is the door operator with the ref inlined.
FLOW_DOOR_OPERATOR: dict[str, str] = ENGINE_OPERATORS  # one map, owned by the flow-engine door (ADR-0134 D3c)

#: …and backwards: an operator whose presence means this launch resolves an
#: engine for that kind, whether it arrives as a `flow:` leg or as a node inside
#: a template leg.
_KIND_OF_DOOR_OPERATOR: dict[str, str] = {op: kind for kind, op in FLOW_DOOR_OPERATOR.items()}

#: The backend key holding the compute target for each kind — what
#: ``PipelineBridge._leg_target`` reads. Only ``data`` differs from the
#: pull-worker door's own default (``CAIRN_ML_TARGET``), so only ``data`` is here.
_TARGET_KEY_FOR_OPERATOR: dict[str, str] = {FLOW_DOOR_OPERATOR["data"]: "dataflow_target"}


def task_leg_executor_problem(op_cls: Any) -> Optional[str]:
    """#2182 — why ``op_cls`` cannot run as a ``task:`` leg, or ``None``.

    A task leg runs its operator in-process through ``invoke_operator_core``;
    it never consults ``pick_executor``. That is deliberate: the paid managed
    training legs (``train/runpod``, ``train/vast``) declare ``remote_job``
    first, and their in-process ``run()`` writes the durable job record the
    template path's inline remote_job executor does not. So an operator whose
    declared executors exclude ``in_process`` cannot be honoured on this
    surface, and is refused rather than run somewhere it did not ask to run.
    """
    from cairn.core.operator.executor import declared_executors  # noqa: PLC0415 — operator stack off the door's import path

    declared = declared_executors(op_cls)
    if "in_process" in declared:
        return None
    name = getattr(op_cls, "name", None) or getattr(op_cls, "__name__", "?")
    return (f"operator {name!r} declares executors {declared}, and a task: leg runs its operator "
            "in-process — reach it through a flow leg or a template leg instead (#2182)")


class ServiceDoorError(ValueError):
    """A launch refused by a service door (ADR-0099 §1) — before any record.

    A ``ValueError`` because every other refusal at the pipeline launch door is
    one, and both routers already treat that family as a client-visible refusal
    rather than a crash.
    """


@dataclass
class DoorRefusal:
    """One door's ``no``, with the legs it is about. ``reasons`` are the door's
    OWN strings — the remedy is written once, where the door knows it."""

    door: str
    legs: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def render(self) -> str:
        where = ""
        if self.legs:
            where = f" leg{'' if len(self.legs) == 1 else 's'} {', '.join(self.legs)} —"
        return f"{self.door} door:{where} " + "; ".join(self.reasons)


def leg_label(step: Any) -> str:
    """How a refusal names one leg: ``'train' (flow:training/sft-train)``."""
    ref = str(getattr(step, "ref", "") or "")
    return f"{str(getattr(step, 'id', '') or '?')!r}" + (f" ({ref})" if ref else "")


def leg_operators(step: Any) -> list[str]:
    """Every operator this leg can invoke — derived per leg kind, never declared.

    A ``gate:`` leg invokes nothing, so it asks nothing.
    """
    from cairn.core.dispatch.leg_executor import (  # noqa: PLC0415 - cycle: executor → engines
        TASK_PREFIX,
        flow_kind_of,
        leg_kind_of,
    )

    ref = str(getattr(step, "ref", "") or "")
    kind = leg_kind_of(ref)
    if kind == "flow":
        return [FLOW_DOOR_OPERATOR.get(flow_kind_of(ref), FLOW_DOOR_OPERATOR["data"])]
    if kind == "task":
        return [op] if (op := ref[len(TASK_PREFIX):].strip()) else []
    if kind == "template":
        from cairn.core.engine.workflow_config import (  # noqa: PLC0415 - off the hot import path
            collect_template_operators,
        )

        try:
            return [str(o) for o in collect_template_operators(ref)]
        except Exception:  # noqa: BLE001 — an unresolvable template is refused by assert_runnable, never here
            logger.debug("could not collect operators for leg %r", ref, exc_info=True)
            return []
    return []


def pipeline_operators(plan: Any) -> dict[str, list[str]]:
    """``operator name → the leg labels that invoke it``, over the whole plan."""
    by_op: dict[str, list[str]] = {}
    for s in list(getattr(plan, "steps", None) or []):
        label = leg_label(s)
        for op in leg_operators(s):
            legs = by_op.setdefault(op, [])
            if label not in legs:
                legs.append(label)
    return by_op


def engine_kinds(by_op: dict[str, list[str]]) -> list[str]:
    """The flow kinds this pipeline will resolve an engine for — empty when no
    leg reaches one (a pipeline of gates and agentic templates asks nothing)."""
    return sorted({k for op in by_op if (k := _KIND_OF_DOOR_OPERATOR.get(op))})


def _target_for(op: str) -> Optional[str]:
    """The compute target a leg invoking ``op`` dispatches to, or ``None`` for
    the pull-worker door's own default (``CAIRN_ML_TARGET``).

    Read through the ONE config plane, exactly as ``PipelineBridge._leg_target``
    does — a ``kind: data`` flow is bound by ``dataflow_target``, so a data leg
    must not be refused for an ``ml_target`` it will never see.
    """
    key = _TARGET_KEY_FOR_OPERATOR.get(op)
    if key is None:
        return None
    try:
        from cairn.core.backend_selection import selected  # noqa: PLC0415 - cycle: selection → profile → pack

        return str(selected(key, default="") or "")
    except Exception:  # noqa: BLE001 — an unreadable selection means "no target"
        return ""


def _legs_for(by_op: dict[str, list[str]], ops: list[str]) -> list[str]:
    out: list[str] = []
    for op in ops:
        for label in by_op.get(op, []):
            if label not in out:
                out.append(label)
    return out


async def check_pipeline_services(plan: Any) -> list[DoorRefusal]:
    """Ask all three doors about this PLAN. Empty list = nothing is missing.

    Every refusal is collected rather than raised at the first: an operator on
    an under-provisioned box should be told all of what is missing, the way
    ``assert_runnable`` already reports every unresolvable leg at once.
    """
    by_op = pipeline_operators(plan)
    if not by_op:
        return []

    # The doors ask the operator registry whether a name is a remote job, and
    # an unloaded registry answers "no" for everything — which would make this
    # door silently fail open on exactly the surface it exists to cover.
    try:
        from cairn.core.operator.loader import ensure_loaded  # noqa: PLC0415 - off the hot import path

        ensure_loaded()
    except Exception:  # noqa: BLE001 — a partial registry is the doors' own fail-open case
        logger.debug("operator registry could not be loaded for the service doors", exc_info=True)

    names = sorted(by_op)
    refusals: list[DoorRefusal] = []

    # #2025 — the JOB-CHANNEL door. A leg that hands work to a remote box
    # streams its live events over CAIRN_JOB_CHANNEL; NATS with no broker means
    # the pipeline would train blind.
    try:
        from cairn.core.job_channel_door import (  # noqa: PLC0415 — the door probes sockets; keep it off the import path
            check_job_channel,
            remote_job_operators,
        )

        chan = check_job_channel(names)
        if not chan.ok:
            refusals.append(DoorRefusal("job-channel", _legs_for(by_op, remote_job_operators(names)),
                                        list(chan.reasons)))
    except Exception:  # noqa: BLE001 — a door that cannot be asked never fails a launch
        logger.debug("the job-channel door could not be asked for this pipeline", exc_info=True)

    # ADR-0134 D3c (was the SkyPilot door, #2073 / ADR-0104 D5) — the FLOW-ENGINE door,
    # narrowed to the kinds this pipeline's legs will actually resolve an engine for.
    kinds = engine_kinds(by_op)
    if kinds:
        try:
            from cairn.core.dataflow.engine_door import (  # noqa: PLC0415 — an engine's check may reach a cloud; keep it off the import path
                check_flow_engines,
            )

            eng = await check_flow_engines(kinds=kinds)
            if not eng.ok:
                refusals.append(DoorRefusal(
                    "flow-engine",
                    _legs_for(by_op, [FLOW_DOOR_OPERATOR[k] for k in kinds]),
                    list(eng.reasons),
                ))
        except Exception:  # noqa: BLE001 — as above
            logger.debug("the flow-engine door could not be asked for this pipeline", exc_info=True)

    # #565 (ADR-0104 D2) — the PULL-WORKER door, asked once per TARGET: the
    # environment binds `kind: data` legs to `dataflow_target` and everything
    # else to `ml_target`, and only the target that a leg will actually use can
    # decide whether that leg's work has to be pulled.
    try:
        from cairn.core.pull_worker_door import (  # noqa: PLC0415 — the door reads the job store; keep it off the import path
            check_pull_worker,
        )

        groups: dict[Any, list[str]] = {}
        for op in names:
            groups.setdefault(_target_for(op), []).append(op)
        for target, ops in sorted(groups.items(), key=lambda kv: str(kv[0])):
            pw = await check_pull_worker(ops, target=target)
            if not pw.ok:
                refusals.append(DoorRefusal("pull-worker", _legs_for(by_op, ops), list(pw.reasons)))
    except Exception:  # noqa: BLE001 — as above
        logger.debug("the pull-worker door could not be asked for this pipeline", exc_info=True)

    return refusals


async def assert_pipeline_services(plan: Any, *, pipeline_ref: str = "") -> None:
    """Refuse the launch when a service this plan needs is not there.

    Called from ``launch_pipeline_def`` beside the cost, approval and licence
    gates — before ``bridge.start_pipeline``, so a refused launch leaves no
    record, no leg and no run for an operator to interpret.
    """
    refusals = await check_pipeline_services(plan)
    if not refusals:
        return
    who = f"pipeline {pipeline_ref}" if pipeline_ref else "this pipeline"
    logger.info("%s refused at the service doors: %s", who, [r.door for r in refusals])
    raise ServiceDoorError(
        f"{who} cannot launch here — " + " | ".join(r.render() for r in refusals)
    )


__all__ = [
    "FLOW_DOOR_OPERATOR", "DoorRefusal", "ServiceDoorError", "assert_pipeline_services",
    "check_pipeline_services", "engine_kinds", "leg_label", "leg_operators",
    "pipeline_operators", "task_leg_executor_problem",
]
