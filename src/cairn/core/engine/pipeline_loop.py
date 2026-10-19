"""Bounded loop unrolling (#1129, ADR-0084 D7).

Closed loops — check → clean → check again, train → eval → train again — are the
thing an ops platform is asked for and the thing most likely to burn money. Three
properties make them safe:

* **bounded** — ``max_iterations`` is mandatory (enforced at parse, #1124);
* **escalating** — ``on_exhausted: gate`` by default, so a loop that cannot
  converge asks a human rather than failing silently or spending forever;
* **acyclic** — iteration N+1 is materialized only *after* N's ``until``
  evaluates false. The plan stays a DAG, the audit trail stays linear, and
  #1130's resume reads the persisted iteration counter.

Unrolling is a pure function of (definition, iteration): it never mutates the
definition, which is the installed content-hashed artifact.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

#: Every outcome of one loop turn. Exhaustive by design — an unhandled case is a
#: pipeline that stalls with nobody told.
ACTION_KINDS = ("continue", "exit", "gate", "fail")


@dataclass
class LoopAction:
    """What the bridge should do after one iteration of a loop."""

    kind: str
    iteration: int = 0
    steps: list[Any] = field(default_factory=list)
    reason: str = ""


def _suffix(name: str, iteration: int) -> str:
    """Iteration 1 keeps the authored id, so a single-pass loop reads like an
    ordinary sequence and wires such as ``check.verdict`` resolve unchanged.
    Later iterations are suffixed so each attempt is its own governed run rather
    than overwriting the previous one in the audit trail."""
    return name if iteration <= 1 else f"{name}#{iteration}"


def unroll_iteration(loop_step: Any, iteration: int) -> list[Any]:
    """Materialize one iteration's steps. Pure — the definition is untouched."""
    body = list(getattr(loop_step, "steps", None) or [])
    names = {getattr(s, "name", "") for s in body}
    out = []
    for s in body:
        copy = s.model_copy(deep=True)
        copy.id = _suffix(getattr(s, "name", ""), iteration)
        # rewrite intra-iteration deps: iteration 2's `check` must wait on
        # iteration 2's `clean`, not iteration 1's.
        copy.depends_on = [
            _suffix(d, iteration) if d in names else d
            for d in (getattr(s, "depends_on", None) or [])
        ]
        out.append(copy)
    return out


def evaluate_until(expr: str, outputs: dict[str, Any], iteration: int = 1) -> bool:
    """Has the loop converged?

    Resolves ``<step>.<field>`` against the LATEST iteration's outputs (falling
    back to the authored id), then compares. An unresolvable or unparseable
    condition returns **False** — fail-safe direction: an unreadable condition
    must never silently end a loop as if it had converged. The bound stops it.

    #2380 — that holds for EVERY operator. ``!=`` used to compare the miss itself
    (``None != "fail"`` → True), so a path the leg never emits ended the loop
    after one iteration as "converged" and ``on_exhausted: gate`` never fired. A
    left side of ``None`` is treated as unresolved: ``_resolve`` cannot tell a
    missing key from an emitted null, and ``_literal`` has no null token, so no
    authorable condition compares against null — the fail-safe reading loses
    nothing.
    """
    raw = str(expr or "").strip()
    if not raw:
        return False
    for op in ("==", "!=", ">=", "<=", ">", "<"):
        left, sep, right = raw.partition(op)
        if not sep:
            continue
        got = _resolve(left.strip(), outputs, iteration)
        if got is None:
            return False
        want = _literal(right.strip())
        try:
            if op == "==":
                return got == want
            if op == "!=":
                return got != want
            if op == ">=":
                return float(got) >= float(want)
            if op == "<=":
                return float(got) <= float(want)
            if op == ">":
                return float(got) > float(want)
            return float(got) < float(want)
        except (TypeError, ValueError):
            return False
    # bare truthiness, e.g. `until: eval.pass`
    return bool(_resolve(raw, outputs, iteration))


def _resolve(ref: str, outputs: dict[str, Any], iteration: int) -> Any:
    # #1303 — the SDK (and lowered plan wires) render refs as `$steps.<id>.<f>`;
    # this resolver spoke only the bare `<id>.<f>` form, so an SDK-authored
    # `until` NEVER resolved → always False (fail-safe) → the loop ran every
    # iteration to exhaustion even when converged from the start.
    ref = ref.removeprefix("$steps.")
    step, _, rest = ref.partition(".")
    src = outputs.get(_suffix(step, iteration))
    if src is None:
        src = outputs.get(step)
    cur: Any = src
    for part in [p for p in rest.split(".") if p]:
        cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


def _literal(tok: str) -> Any:
    t = tok.strip().strip('"').strip("'")
    if t.lower() in ("true", "false"):
        return t.lower() == "true"
    try:
        return float(t) if "." in t else int(t)
    except ValueError:
        return t


def next_loop_action(
    loop_step: Any, *, iteration: int, outputs: dict[str, Any],
) -> LoopAction:
    """Decide what happens after iteration ``iteration`` finished.

    Convergence is checked FIRST: hitting the bound only counts as exhaustion if
    the loop also failed to converge on that final turn.
    """
    name = str(getattr(loop_step, "loop", "") or "loop")
    max_iter = int(getattr(loop_step, "max_iterations", 0) or 0)

    if evaluate_until(str(getattr(loop_step, "until", "") or ""), outputs, iteration):
        return LoopAction(kind="exit", iteration=iteration,
                          reason=f"loop {name!r} converged at iteration {iteration}")

    if iteration >= max_iter:
        on_exhausted = str(getattr(loop_step, "on_exhausted", "gate") or "gate")
        reason = (
            f"loop {name!r} did not converge after {max_iter} iteration(s); "
            f"condition: {getattr(loop_step, 'until', '')!r}"
        )
        # default `gate`: a human decides. Never silent failure, never more spend.
        return LoopAction(kind="fail" if on_exhausted == "fail" else "gate",
                          iteration=iteration, reason=reason)

    nxt = iteration + 1
    return LoopAction(
        kind="continue", iteration=nxt, steps=unroll_iteration(loop_step, nxt),
        reason=f"loop {name!r} advancing to iteration {nxt} of {max_iter}",
    )


__all__ = ["ACTION_KINDS", "LoopAction", "evaluate_until", "next_loop_action",
           "unroll_iteration"]


def parse_iteration_id(step_id: str) -> tuple[str, int]:
    """``("train", 3)`` for ``"train#3"``; ``("train", 1)`` for ``"train"``.
    The inverse of :func:`_suffix` — kept beside it so the two cannot drift."""
    base, sep, suf = str(step_id).rpartition("#")
    if sep and suf.isdigit():
        return base, int(suf)
    return step_id, 1


def declared_step_id(step_id: str, loops: dict[str, Any]) -> str:
    """The step id the plan DECLARED for ``step_id``: a loop body's later iteration
    (``check#2``) maps back to its authored body step (``check``); anything else —
    including an authored id that merely ends in ``#<digits>`` — is returned as is.
    #2344: records keyed by declared step (contract entries) must be found from every
    iteration, and never by an unrelated step that happens to share the prefix."""
    base, it = parse_iteration_id(step_id)
    if it > 1 and any(str(b.get("id") or "") == base
                      for ldef in (loops or {}).values() for b in ((ldef or {}).get("steps") or [])):
        return base
    return step_id


def iteration_view(outputs: dict[str, Any], step_id: str,
                   loops: dict[str, Any]) -> dict[str, Any]:
    """``outputs`` as seen from ONE consuming step — the wire-resolution half
    of the loop runtime (#1671).

    ``unroll_iteration`` rewrites ``depends_on`` to the suffixed ids but copies
    input wire STRINGS verbatim, so iteration 2's ``$steps.a.token`` used to
    look up ``outputs["a"]`` — iteration 1, forever. ``evaluate_until`` was the
    only iteration-aware resolver, which is why ``until:`` worked while the
    wires beside it silently froze.

    The rule (one rule, both directions): **a body-step name aliases to its
    latest COMPLETED instance**, capped at the consuming step's own iteration
    for names in its own loop.

    - intra-iteration: ``b#2`` reads ``a`` → ``a#2`` (done — deps were
      rewritten), never iteration 1's stale rows;
    - previous-iteration carry: ``a#2`` reads ``b`` (later in the body, so its
      #2 doesn't exist yet) → iteration 1's ``b`` — the N−1 handoff a
      continual-training chain needs (iteration 1 covers the no-predecessor
      case with ``$default(...)``);
    - a step AFTER the loop reads the FINAL iteration, not the first.

    Pure and cheap: returns ``outputs`` itself when no alias applies (the
    common no-loop case allocates nothing).
    """
    if not loops:
        return outputs
    authored, own_iter = parse_iteration_id(step_id)
    own_loop = ""
    body_names: dict[str, str] = {}  # body step name -> its loop name
    for lname, ldef in loops.items():
        for b in (ldef or {}).get("steps") or []:
            bid = str(b.get("id") or "")
            if bid:
                body_names[bid] = lname
                if bid == authored:
                    own_loop = lname
    aliases: dict[str, Any] = {}
    for bid, lname in body_names.items():
        cap = own_iter if lname == own_loop else None
        best = 0
        for done_id in outputs:
            base, it = parse_iteration_id(done_id)
            if base == bid and it > best and (cap is None or it <= cap):
                best = it
        if best > 1:  # iteration 1 is the bare id — already correct
            aliases[bid] = outputs[_suffix(bid, best)]
    if not aliases:
        return outputs
    return {**outputs, **aliases}
