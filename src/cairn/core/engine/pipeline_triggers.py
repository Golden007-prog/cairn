"""How a pipeline declares its own starts, and how they get armed at install
(#1159, ADR-0084 D9; ADR-0101 D1 / S7, #1987).

A trigger belongs to a PIPELINE, not to a loose YAML somewhere: installing the
pipeline is what arms it, and uninstalling it is what disarms it. Otherwise a
pack can be removed while the thing that fires it keeps firing — which is how
you get a schedule launching a pipeline that no longer exists.

Materialization is CONVERGENT, not additive. Schedule ids are derived from the
pipeline ref, so re-installing an edited pipeline updates its schedules in place
and deletes the ones it no longer declares. An additive install would silently
double the firing rate every time a pack was reinstalled.

**One grammar, two spellings** (ADR-0101 S7). A pipeline says how it starts the
way a flow does — ``triggers:`` with a ``kind:`` per entry — and the ``on:``
block it has had since #1159 is READ AS one of those entries, kind ``schedule``.
There is no second parser, no second validator and no second materialiser:
:func:`declarations_for` turns either spelling into the typed declarations of
:mod:`cairn.core.engine.triggers_grammar`, and :func:`materialise_pipeline_triggers`
hands them to that module's ``materialise_declared_triggers`` — the same
function S1 (#1983) arms a workflow with.

Two things are deliberately preserved rather than unified, because a pipeline
that works today has to keep working exactly as it does:

* **An ``on:`` row keeps its id.** ``pipeline--<pack>--<name>--<i>``, not the
  ``declared--…`` id a flow's declaration gets. Re-keying would leave the old
  row firing beside the new one, or (if swept) reset a pause an operator set.
* **An ``on:`` row still arms ENABLED.** A ``triggers:`` schedule materialises
  PAUSED unless it says ``enabled: true`` — the #1568 rule that installing a
  pack must not start a nightly bill — but flipping that for ``on:`` would
  silently stop a nightly that runs today. The legacy spelling keeps the legacy
  default; the new one gets the safe default. That difference is the second
  reason (after "there is only one of them worth writing") to write
  ``triggers:``.

``webhook`` and ``watch`` are armed for a pipeline since ADR-0131 (D1). They
were held back because a receiver's run path took the template door, which no
pipeline ref could pass; since #2115 the router starts through the one launch
door (``dispatch/start.py:unattended_starter`` → ``launch_ref``), which takes a
``pipeline:`` ref like ``/start`` does. A source watch arms per ADR-0131 D3: the
platform's ``watch-source`` row when that pipeline is installed, otherwise core's
own ``dataset`` watch; a kind that still cannot be armed comes back in
:attr:`MaterialiseResult.unarmed` with its reason — named, never silently
dropped (#1543).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from cairn.core.engine.pipeline import ARMABLE_ON_KINDS, on_entry_kind
from cairn.core.refs import spellings
from cairn.core.engine.triggers_grammar import (
    MaterialiseResult,
    ScheduleTrigger,
    declared_id,
    declared_prefix,
    materialise_declared_triggers,
    parse_declared_triggers,
)

logger = logging.getLogger("cairn.core.engine.pipeline_triggers")

#: `on:` entry kinds. `cron` is materialized here; webhook/watch are separate
#: surfaces and are reported as unsupported rather than silently ignored — a
#: trigger that looks declared but never fires is worse than an error.
#: #1543 — RE-EXPORTED from the model layer, not redeclared: the parser now
#: rejects an unarmable kind up front, and two copies of "what can be armed"
#: would eventually disagree about it.
SUPPORTED_ON = ARMABLE_ON_KINDS


class UnsupportedTriggerError(ValueError):
    """An `on:` entry names a kind this install path cannot arm yet.

    Since #1543 a parseable def can no longer carry one — ``parse_pipeline_def``
    rejects it first. This stays as the belt-and-braces guard for defs handed in
    from elsewhere (the signature is ``Any``, not ``PipelineDef``).
    """


def schedule_id_for(pack: str, name: str, index: int) -> str:
    """Deterministic id: same pipeline + same slot ⇒ same schedule row.

    This is what makes re-install converge instead of duplicating.
    """
    return f"pipeline--{pack}--{name}--{index}"


#: #1543 — the grammar reader lives in ``pipeline`` now; aliased so this module's
#: internal call sites (and any importer of the old private name) keep working.
_entry_kind = on_entry_kind


def plan_triggers(d: Any) -> list[dict]:
    """Validate + normalize a pipeline's `on:` entries into schedule rows.

    Raises :class:`UnsupportedTriggerError` naming the kind, so an author learns
    at install time rather than by noticing nothing ever ran.
    """
    out: list[dict] = []
    for i, entry in enumerate(getattr(d, "on", None) or []):
        if not isinstance(entry, dict):
            raise UnsupportedTriggerError(
                f"pipeline {d.pack}/{d.name}: `on:` entry {i} is not a mapping")
        kind = _entry_kind(entry)
        if not kind:
            raise UnsupportedTriggerError(
                f"pipeline {d.pack}/{d.name}: `on:` entry {i} declares no known "
                f"trigger — expected one of cron, webhook, watch")
        if kind not in SUPPORTED_ON:
            raise UnsupportedTriggerError(
                f"pipeline {d.pack}/{d.name}: `on: {kind}` is not armed at install "
                f"yet (only {', '.join(SUPPORTED_ON)}). Declaring it would look "
                f"wired while never firing — see #1159.")
        out.append({
            "index": i,
            "id": schedule_id_for(d.pack, d.name, i),
            "cron": str(entry["cron"]).strip(),
            "inputs": dict(entry.get("inputs") or entry.get("with") or {}),
            "environment": str(entry.get("environment") or ""),
            "description": str(entry.get("description") or ""),
        })
    return out


async def materialize(
    d: Any, *, store: Any, workspace_id: str = "default",
) -> list[str]:
    """Arm this pipeline's `on:` triggers. Returns the schedule ids now live.

    Convergent: rows this pipeline previously owned but no longer declares are
    deleted, so an edited pipeline does not leave an orphan firing on the old
    cron.
    """
    from cairn.core.storage.schedule_store import StoredSchedule  # noqa: PLC0415 - cycle

    ref = f"pipeline:{d.pack}/{d.name}"
    planned = plan_triggers(d)
    live: list[str] = []

    # rows minted under the pack's former directory spelling (#2527) go FIRST: a new row
    # carries the same unique `name`, which Postgres refuses while the old row exists.
    current = f"pipeline--{d.pack}--{d.name}--"
    for s in await store.list(workspace_id):
        sid = str(s.id)
        if not sid.startswith(current) and any(sid.startswith(p) for p in owned_prefixes(d.pack, d.name)
                                                if p.startswith("pipeline--")):
            await store.delete(s.id, workspace_id)
            logger.info("removed pipeline schedule %s minted under a former pack spelling", s.id)

    for row in planned:
        s = StoredSchedule(
            id=row["id"], workspace_id=workspace_id,
            name=row["description"] or f"{d.pack}/{d.name} @ {row['cron']}",
            cron=row["cron"], workflow=ref, inputs=row["inputs"],
            environment=row["environment"],
            description=row["description"] or f"declared by pipeline {d.pack}/{d.name}",
        )
        existing = await store.get(row["id"], workspace_id)
        if existing is not None:
            await store.delete(row["id"], workspace_id)   # replace in place
        await store.create(s)
        live.append(row["id"])

    # drop rows this pipeline used to own — an edit that removes an `on:` entry
    # must actually stop that schedule.
    for s in await store.list(workspace_id):
        if str(s.id).startswith(current) and s.id not in live:
            await store.delete(s.id, workspace_id)
            logger.info("removed stale pipeline schedule %s", s.id)

    return live


async def dematerialize(
    pack: str, name: str, *, store: Any, workspace_id: str = "default",
) -> int:
    """Disarm every schedule this pipeline owns. Returns how many were removed."""
    removed = 0
    for s in await store.list(workspace_id):
        if any(str(s.id).startswith(p) for p in owned_prefixes(pack, name)):
            await store.delete(s.id, workspace_id)
            removed += 1
    return removed


# ── ADR-0101 S7 (#1987): one grammar, two spellings ──────────────────────


def pipeline_ref_of(d: Any) -> str:
    """``pipeline:<pack>/<name>`` — the registry ref, and the ref a materialised
    row's ``workflow`` field carries so the fire path takes the pipeline door
    (``scheduler.fire_scheduled_workflow`` → ``launch_pipeline``)."""
    return f"pipeline:{getattr(d, 'pack', '')}/{getattr(d, 'name', '')}"


def owned_prefixes(pack: str, name: str) -> tuple[str, ...]:
    """Every store-id prefix this pipeline's rows may carry.

    Two, because there are two spellings: the #1159 id an ``on:`` entry keeps,
    and the ``declared--`` id S1 mints for a ``triggers:`` entry. Both are
    machine-minted — the console slugs a hand-made row's id from its NAME and
    can never produce either — which is what makes it safe for ``_converge`` to
    ADOPT a row under one of them whose ``declared_by`` is empty (a pre-S7 row,
    written before the field existed).

    :func:`dematerialize` needs the full list too: uninstalling a pipeline must
    disarm what it declared, whichever spelling it used.
    """
    return tuple(p for s in spellings(pack)
                 for p in (f"pipeline--{s}--{name}--", declared_prefix(f"pipeline:{s}/{name}")))


def schedule_trigger_from_on(entry: dict, *, pack: str, name: str) -> ScheduleTrigger:
    """Read one legacy ``on:`` entry AS a ``kind: schedule`` declaration.

    Field for field what :func:`plan_triggers` has always built, so the row that
    comes out the far end fires the same cron with the same inputs in the same
    environment. Two fields carry the legacy defaults on purpose (see the module
    docstring): ``description`` keeps #1159's "declared by pipeline …" fallback,
    and ``enabled`` is True because these schedules arm live today.
    """
    return ScheduleTrigger(
        kind="schedule",
        cron=str(entry.get("cron") or "").strip(),
        inputs=dict(entry.get("inputs") or entry.get("with") or {}),
        environment=str(entry.get("environment") or ""),
        description=str(entry.get("description") or "")
        or f"declared by pipeline {pack}/{name}",
        enabled=True,
    )


def declarations_for(d: Any) -> list[Any]:
    """This pipeline's declared starts, both spellings, as typed declarations.

    ``triggers:`` first (validated by the flow grammar), then ``on:`` read as
    schedule declarations. ``parse_pipeline_def`` refuses a pipeline that
    declares both, so in practice exactly one of the two lists is non-empty —
    the concatenation is what makes that a rule of the PARSER rather than an
    assumption baked into every reader.
    """
    pack, name = getattr(d, "pack", ""), getattr(d, "name", "")
    out = list(parse_declared_triggers(
        getattr(d, "triggers", None), where=f"pipeline {name!r} triggers"))
    out += [schedule_trigger_from_on(e, pack=pack, name=name)
            for e in (getattr(d, "on", None) or [])]
    return out


def declarations_for_ref(ref: str) -> list[Any]:
    """:func:`declarations_for`, from a ``pipeline:<pack>/<name>`` ref.

    The read side: what the console's Triggers route answers with. Resolution
    failures are the caller's to report — an unknown ref is a 404 there, not an
    empty list that would read as "this pipeline declares nothing".
    """
    from cairn.core.engine.pipeline_resolve import resolve_pipeline_ref  # noqa: PLC0415 - cycle: resolve ← pipeline_triggers

    d = resolve_pipeline_ref(ref)
    if d is None:
        raise LookupError(f"no installed pipeline {ref!r}")
    return declarations_for(d)


async def materialise_pipeline_triggers(
    d: Any, *, store: Any | None = None, trigger_store: Any | None = None,
    workspace_id: str = "default",
) -> MaterialiseResult:
    """Arm this pipeline's declared starts — S1's materialiser, S7's ids.

    Convergent in both directions and safe to call for a pipeline that declares
    NOTHING: that is exactly when a schedule it used to declare has to be
    removed, which is why ``enable_pipeline`` calls this unconditionally rather
    than only when a block is present.

    Every declared kind goes to the materialiser (ADR-0131 D1; see the module
    docstring for why ``webhook``/``watch`` were once held back here). What it
    cannot arm, it reports in :attr:`MaterialiseResult.unarmed` with the reason.
    """
    pack, name = getattr(d, "pack", ""), getattr(d, "name", "")
    ref = pipeline_ref_of(d)
    legacy = bool(getattr(d, "on", None))   # the parser refuses both spellings
    decls = declarations_for(d)
    # An entry the pipeline door cannot arm still CONSUMES ITS SLOT: ids carry
    # the declaration's index, so dropping a webhook out of the middle of the
    # list without keeping its slot would renumber the schedules after it and
    # re-create every one of them under a new id.
    held_back: tuple[str, ...] = ()  # ADR-0131 D1 — nothing is held back any more
    armable = [(i, decl) for i, decl in enumerate(decls)
               if getattr(decl, "kind", "") not in held_back]

    def _id_for(kind: str, pos: int) -> str:
        i = armable[pos][0]
        # `on:` keeps the #1159 id; `triggers:` takes the ordinary declared one.
        return schedule_id_for(pack, name, i) if legacy else declared_id(ref, kind, i)

    res = await materialise_declared_triggers(
        ref, [decl for _, decl in armable],
        workspace_id=workspace_id, schedule_store=store,
        # Webhook receiver rows (and, ADR-0131 D4, event rows) converge here.
        trigger_store=trigger_store,
        id_for=_id_for, owned_prefixes=owned_prefixes(pack, name),
    )
    # report what was actually held back, by kind — not a hardcoded "webhook"
    res.unarmed += [k for k in (getattr(d_, "kind", "") for d_ in decls) if k in held_back]
    return res


__all__ = [
    "SUPPORTED_ON", "UnsupportedTriggerError", "declarations_for",
    "declarations_for_ref", "dematerialize", "materialise_pipeline_triggers",
    "materialize", "owned_prefixes", "pipeline_ref_of", "plan_triggers",
    "schedule_id_for", "schedule_trigger_from_on",
]
