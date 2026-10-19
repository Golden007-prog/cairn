"""
Notifier — the 5th integration seam (fire-and-forget output).

Notifiers differ from Executors in three ways:

  1. **No saga.** No dry_run, no verify, no compensate. Just send.
  2. **No policy gating.** Notifications are observation, not state change.
  3. **No HITL.** Notifiers run after the investigation is settled.

Use cases:
  - Append every investigation to ``~/.cairn/inbox.jsonl`` so a developer's
    Claude Code session can read it via ``UserPromptSubmit`` hook
  - Post an incident digest to an ``#incidents`` channel
  - Forward findings to PagerDuty as a comment on the open incident
  - Pipe to a self-observability sink (every result → ClickHouse for our own
    Phase 8 dashboards)

This module provides:
  - ``Notifier`` Protocol
  - ``NotifierRegistry``
  - The built-in generic notifiers ``StdoutNotifier`` (debug) and
    ``FileNotifier`` (JSONL append for Claude integration).

Vendor notifiers ship as service-provider operators bound via the
``notify.sink`` profile binding (ADR-0032) — e.g. ``cairn-operator-notify-slack``.
``default_notifiers()`` resolves the bound provider and appends it to the
stdout/file defaults; unbound = no external notifier (no vendor code in core).

Runner calls ``notifier_registry.notify_all(result)`` after each
RunResult. Failures are isolated — one bad notifier doesn't
break the others or the investigation.
"""

from __future__ import annotations

import json
import logging
from cairn.core import settings as _settings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from cairn.core.engine.schema import RunResult

logger = logging.getLogger("cairn.notifier")


# ─── NotifyResult ────────────────────────────────────────────────────


@dataclass
class NotifyResult:
    """Outcome of a single notifier call."""
    notifier: str
    ok: bool
    detail: str = ""


# ─── NotifyEvent — the second payload shape (#1791) ──────────────────

#: The governance moments a human must be told about, in the bridge's own
#: vocabulary. ``awaiting_approval`` / ``awaiting_advance`` are StepState
#: statuses; ``pipeline_failed`` is the PipelineRecord status.
GATE_EVENT_KINDS = ("awaiting_approval", "awaiting_advance", "pipeline_failed")


@dataclass(frozen=True)
class NotifyEvent:
    """A pipeline moment worth interrupting a human for (#1791).

    **Why this is not a RunResult.** The existing :meth:`Notifier.notify` takes
    a completed :class:`RunResult` — a run that finished, with actions, outcomes
    and a duration. A pause has none of those: nothing ran, nothing was decided,
    there is no duration. Synthesising a RunResult for it would put a
    fabricated run record into every notifier's digest (and into
    ``inbox.jsonl``), which is exactly the kind of lie a governance product
    cannot ship. So the event gets its own small shape, and notifiers opt into
    it with a second method (see :class:`Notifier`).

    The fields are the answer to "why am I being paged and what do I type":
    WHICH pipeline + step, WHICH authority demanded the gate, at what risk and
    cost, and the exact commands that release it.
    """

    kind: str                       # one of GATE_EVENT_KINDS
    pipeline_id: str
    step_id: str = ""
    pipeline_name: str = ""
    operator: str = ""              # the leg's ref (`task:`/`flow:` target)
    risk: str = ""
    #: WHICH authority demanded the gate — operator / profile / policy /
    #: binding, or the step's own `approval:` / `trigger: manual` declaration.
    #: Without it "needs approval" is unactionable (#55).
    authority: tuple[str, ...] = ()
    reason: str = ""
    #: Cost context when the pipeline carries any: the admitted budget and any
    #: `show:` field the gate author surfaced that names a cost. Never invented.
    cost: dict[str, Any] = field(default_factory=dict)
    #: The exact release commands, rendered by the caller (CLI line + API line).
    commands: tuple[str, ...] = ()
    #: One pause = one notification. ``(pipeline, step, awaiting_since)`` for a
    #: pause; ``(pipeline, ended_at)`` for a failure.
    dedupe_key: str = ""
    #: #2453 (B) — the destination, resolved by CORE from ``[profile.notify]`` in
    #: :func:`cairn.core.gate_notify.emit_gate_event`; a notifier delivers, it does
    #: not read the profile. ``""`` / ``None`` = the notifier's own default.
    channel: str = ""
    webhook_url: Optional[str] = None

    def title(self) -> str:
        if self.kind == "pipeline_failed":
            return "pipeline failed"
        if self.kind == "awaiting_advance":
            return "waiting for a manual advance"
        return "approval needed"

    def render(self) -> str:
        """The message body every channel posts. Deliberately plain text with
        Slack-compatible ``*bold*`` — the delivery layer decides the transport,
        core decides the CONTENT, so the authority line and the release command
        cannot go missing on one channel and not another."""
        name = self.pipeline_name or self.pipeline_id
        lines = [f"*cairn — {self.title()}*"]
        where = f"pipeline `{name}` ({self.pipeline_id})"
        if self.step_id:
            where += f" · step `{self.step_id}`"
        lines.append(where)
        if self.authority:
            lines.append(f"_Demanded by:_ {', '.join(self.authority)}")
        if self.operator:
            lines.append(
                f"_Operator:_ {self.operator}"
                + (f" · _risk:_ {self.risk}" if self.risk else "")
            )
        elif self.risk:
            lines.append(f"_Risk:_ {self.risk}")
        if self.reason:
            lines.append(f"_Reason:_ {self.reason[:400]}")
        if self.cost:
            lines.append(
                "_Cost:_ " + " · ".join(f"{k}={v}" for k, v in self.cost.items())
            )
        if self.commands:
            lines.append("_Release it:_")
            lines.extend(f"    {c}" for c in self.commands)
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "pipeline_id": self.pipeline_id,
            "step_id": self.step_id,
            "pipeline_name": self.pipeline_name,
            "operator": self.operator,
            "risk": self.risk,
            "authority": list(self.authority),
            "reason": self.reason,
            "cost": dict(self.cost),
            "commands": list(self.commands),
        }


# ─── Protocol + Registry ─────────────────────────────────────────────


@runtime_checkable
class Notifier(Protocol):
    """Fire-and-forget output target.

    Implementations should:
      - Be idempotent enough that double-firing doesn't cause harm
      - Never raise — return ``NotifyResult(ok=False, ...)`` on failure
      - Be fast (< 2s); slow notifiers should buffer and return ok=True

    ``notify_event`` (#1791) is **optional**: it is not part of the structural
    contract, and :meth:`NotifierRegistry.notify_event_all` skips a notifier
    that lacks it rather than feeding it a fabricated RunResult. A notifier
    written before events existed therefore keeps working untouched, and stays
    honest — it simply is not an event target.
    """
    name: str

    async def notify(self, result: RunResult) -> NotifyResult: ...


class NotifierRegistry:
    """Holds the active notifiers; the Runner fans out RunResults."""

    def __init__(self) -> None:
        self._notifiers: list[Notifier] = []

    def register(self, notifier: Notifier) -> None:
        self._notifiers.append(notifier)

    def register_many(self, notifiers: list[Notifier]) -> None:
        self._notifiers.extend(notifiers)

    def list_names(self) -> list[str]:
        return [n.name for n in self._notifiers]

    def __len__(self) -> int:
        return len(self._notifiers)

    async def notify_all(self, result: RunResult) -> list[NotifyResult]:
        """Call every registered notifier. Never raises."""
        outcomes: list[NotifyResult] = []
        for n in self._notifiers:
            try:
                outcome = await n.notify(result)
            except Exception as e:
                outcome = NotifyResult(notifier=n.name, ok=False, detail=f"raised: {e}")
                logger.exception("notifier %s raised", n.name)
            outcomes.append(outcome)
        return outcomes

    async def notify_event_all(self, event: NotifyEvent) -> list[NotifyResult]:
        """Fan a :class:`NotifyEvent` out (#1791). Never raises.

        A notifier without ``notify_event`` is SKIPPED, not adapted: there is no
        honest RunResult to build from a pause, so pretending otherwise is worse
        than one channel staying quiet."""
        outcomes: list[NotifyResult] = []
        for n in self._notifiers:
            fn = getattr(n, "notify_event", None)
            if fn is None:
                continue
            try:
                outcome = await fn(event)
            except Exception as e:  # noqa: BLE001 — a bad destination never breaks the run
                outcome = NotifyResult(notifier=n.name, ok=False, detail=f"raised: {e}")
                logger.exception("notifier %s raised on event", n.name)
            outcomes.append(outcome)
        return outcomes


# ─── Concrete notifiers ──────────────────────────────────────────────


@dataclass
class StdoutNotifier:
    """Prints a one-line summary to stdout. Useful for demos / development."""
    name: str = "stdout"

    async def notify(self, result: RunResult) -> NotifyResult:
        line = (
            f"[notifier:stdout] {result.skill_name} on "
            f"{result.trigger.service or 'unknown'} → "
            f"actions={[a.candidate.id for a in result.actions]} "
            f"duration={result.duration_seconds:.2f}s"
        )
        print(line)
        return NotifyResult(notifier=self.name, ok=True, detail=line[:140])

    async def notify_event(self, event: NotifyEvent) -> NotifyResult:
        line = f"[notifier:stdout] {event.kind} {event.pipeline_id}/{event.step_id}"
        print(line)
        print(event.render())
        return NotifyResult(notifier=self.name, ok=True, detail=line[:140])


@dataclass
class FileNotifier:
    """Appends a JSONL record per investigation to a local file.

    Default path is ``~/.cairn/inbox.jsonl`` — the canonical location
    a developer's Claude Code session reads via ``UserPromptSubmit`` hook
    to surface new findings inline with their next prompt. (See Roadmap
    §3.6 — Claude integration.)
    """
    name: str = "file"
    path: Path = field(default_factory=lambda: _settings.inbox_path())

    async def notify(self, result: RunResult) -> NotifyResult:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            record = _digest_record(result)
            with self.path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(record, ensure_ascii=False) + "\n")
            return NotifyResult(
                notifier=self.name, ok=True, detail=f"appended to {self.path}"
            )
        except OSError as e:
            return NotifyResult(notifier=self.name, ok=False, detail=str(e))

    async def notify_event(self, event: NotifyEvent) -> NotifyResult:
        """Append the event to the same inbox — a paused pipeline is exactly the
        kind of thing the developer's next prompt should surface."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(event.as_dict(), ensure_ascii=False) + "\n")
            return NotifyResult(notifier=self.name, ok=True, detail=f"appended to {self.path}")
        except OSError as e:
            return NotifyResult(notifier=self.name, ok=False, detail=str(e))


# ─── Helpers ─────────────────────────────────────────────────────────


def _digest_record(result: RunResult) -> dict[str, Any]:
    """JSONL record shape — kept compact so the inbox file stays readable."""
    return {
        "skill": result.skill_name,
        "template": result.template_name,
        "service": result.trigger.service,
        "source": result.trigger.source,
        "matched_pattern": result.trigger.matched_pattern,
        "duration_seconds": round(result.duration_seconds, 2),
        "summary": (result.raw_output or {}).get("summary") if result.raw_output else None,
        "actions_proposed": [
            {"id": a.candidate.id, "type": a.candidate.type,
             "decision": a.decision.effect, "risk": a.candidate.risk}
            for a in result.actions
        ],
        "actions_outcomes": [
            {"id": o.candidate_id, "status": o.status,
             "verify": o.verify_result.status if o.verify_result else None,
             "compensated": o.compensated}
            for o in result.action_outcomes
        ],
    }


# ─── Factory ────────────────────────────────────────────────────────


def _resolve_notify_sink() -> Optional[Notifier]:
    """Resolve the profile-bound ``notify.sink`` provider, or None (ADR-0032).

    Mirrors ``obs.audit.get_audit_sink`` resolution: the operator name comes
    from the active profile's ``[profile.bindings]`` (``notify.sink``), the
    provider class comes from the operator registry. Fail-open — a misconfigured
    binding logs and degrades to no external notifier, never raising.
    """
    try:
        from cairn.core.pack.profile import load_profile_from_env
        name = load_profile_from_env().bindings.get("notify.sink")
    except Exception as e:  # noqa: BLE001 — no profile / no binding = unbound
        logger.debug("no notify.sink binding resolved: %s", e)
        return None
    if not name:
        return None
    try:
        from cairn.core.operator import default_registry
        op_cls = default_registry.get(name)
        inst = op_cls() if isinstance(op_cls, type) else op_cls
        if not hasattr(inst, "notify"):
            logger.warning(
                "notify.sink binding %r is not a Notifier (no notify()) — ignoring", name
            )
            return None
        logger.info("notify.sink bound: %s", name)
        return inst  # type: ignore[return-value]
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.warning("notify.sink binding %r unresolved (%s) — ignoring", name, e)
        return None


def default_notifiers() -> list[Notifier]:
    """Default notifier set for trigger_server / demo.

    The generic ``stdout`` + ``file`` notifiers are always present. An external
    notifier (Slack, etc.) is opt-in via the ``notify.sink`` profile binding
    (ADR-0032) — its provider ships as a service-provider operator
    (``cairn-operator-notify-slack``), so no vendor code lives in core. Unbound = just
    stdout + file.
    """
    notifiers: list[Notifier] = [StdoutNotifier(), FileNotifier()]
    bound = _resolve_notify_sink()
    if bound is not None:
        notifiers.append(bound)
    return notifiers
