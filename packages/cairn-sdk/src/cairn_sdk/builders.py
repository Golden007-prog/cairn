"""The 15-word authoring vocabulary → record builders (#1253, ADR-0087 P1).

Every word maps 1:1 to a record construct; nothing here executes. Rules the
compiler enforces are documented on :mod:`cairn_sdk.compiler`; this module only
captures the author's declarations.

Ref grammar (ADR-0087 / the vocabulary brief): a SLASH in a ``step`` ref means
shared code (``pack/op`` — a ``task`` leg); no slash means this pack's own flow
(``flow`` leg). Agentic legs come from :func:`flow.agentic` (``template`` leg).

P2 (#1254) lifts the remaining P1 fence: a ``def`` decorated with ``@step``
becomes a LIFTED private op at build (ref ``<pack>/_steps/<pipeline>.<step>``,
a normal task leg) — ``requires=[...]`` is mandatory whenever the body touches
``ctx.*`` (fail-closed, ADR-0053; the compiler's AST lint assists declaration
but never infers it — D5), and omitted ``risk`` defaults to ``external_write``.

Remaining fence: pipeline-level ``on = [cron(...), watch(...)]`` needs a
record-grammar home (schedule records are a separate store today) — captured
but compiled only to a build NOTE, never silently dropped. Step-level
``trigger=`` compiles today.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence, Union

from .expr import OutRef, Predicate, Ref


class _StepLike:
    """Shared identity plumbing: assignment name = step id (``__set_name__``)."""

    def __init__(self) -> None:
        self._id: Optional[str] = None

    def __set_name__(self, owner: type, name: str) -> None:
        if self._id is None:
            self._id = name

    @property
    def id(self) -> str:
        if self._id is None:
            raise CompileScopeError(
                f"{type(self).__name__} was never assigned a name in a pipeline "
                "class body — step identity IS the attribute name (D1)."
            )
        return self._id

    @property
    def out(self) -> OutRef:
        return OutRef(lambda: self.id, ())

    def __rshift__(self, other: "_StepLike | _Chain") -> "_Chain":
        return _Chain([self]) >> other


class CompileScopeError(TypeError):
    """An authoring construct outside P1's fences (with the phase that owns it)."""


class _LiftedStep(_StepLike):
    """A ``def`` that RUNS: the build lifts the body into a PRIVATE op in the
    pack wheel (ref ``<pack>/_steps/<pipeline>.<step>``) and the pipeline
    record references it as a normal task leg (#1254, ADR-0087 P2).

    ``requires`` names the ctx capabilities the body may touch — mandatory
    whenever the body uses ``ctx.*`` (fail-closed, ADR-0053). The runtime
    enforces the list on every access (``CapabilityNotDeclared``); the
    compiler's AST lint only assists the declaration (D5). Omitted ``risk``
    is ``external_write`` — the conservative default every op gets.
    """

    def __init__(self, fn: Any, *, requires: Optional[Sequence[str]] = None,
                 risk: Optional[str] = None, retries: Optional[int] = None,
                 retry: Optional[dict] = None, timeout: Optional[str] = None,
                 environment: Optional[str] = None, **wires: Any) -> None:
        super().__init__()
        self.fn = fn
        self.requires = list(requires) if requires is not None else None
        self.risk = risk or "external_write"
        self.retry = dict(retry) if retry else (
            {"max_attempts": int(retries)} if retries else None)
        self.timeout = timeout
        self.environment = environment
        self.trigger: Optional[str] = None
        self.wires = dict(wires)
        self.when_pred: Optional[Predicate] = None

    def __set_name__(self, owner: type, name: str) -> None:
        # the def's own name is the identity even when re-wrapped
        if self._id is None:
            self._id = name

    leg_kind = "task"


def step(ref: Any = None, *, requires: Optional[Sequence[str]] = None,
         risk: Optional[str] = None, retries: Optional[int] = None,
         retry: Optional[dict] = None, timeout: Optional[str] = None,
         environment: Optional[str] = None, trigger: Optional[str] = None,
         **wires: Any) -> Any:
    """USE a runnable — or MAKE a private one from a ``def``.

    - ``step("pack/op", ...)`` = shared op (task leg); ``step("name", ...)`` =
      this pack's own flow (flow leg). Keyword args are the leg's ``with:``
      wires; refs in them become data edges (D4). ``retries=N`` compiles onto
      the leg's H2 retry block (#1244) — the SDK ships no retry machinery.
    - ``@step(requires=[...], risk=..., retries=...)`` on a ``def`` (or bare
      ``@step``) lifts the body into a private op at build (#1254).
    """
    if callable(ref):                      # bare @step on a def
        return _LiftedStep(ref, requires=requires, risk=risk, retries=retries,
                           retry=retry, timeout=timeout, environment=environment,
                           **wires)
    if ref is None:                        # @step(requires=..., ...) decorator
        def _lift(fn: Any) -> _LiftedStep:
            return _LiftedStep(fn, requires=requires, risk=risk, retries=retries,
                               retry=retry, timeout=timeout,
                               environment=environment, **wires)
        return _lift
    if requires is not None or risk is not None:
        raise CompileScopeError(
            "requires=/risk= belong to a lifted def (or the op's own pack "
            "declaration) — a step CALLING a shared ref cannot re-scope it."
        )
    return _RefStep(str(ref), retry=retry, retries=retries, timeout=timeout,
                    environment=environment, trigger=trigger, **wires)


class _RefStep(_StepLike):
    """A step referencing an existing runnable (P1 form): slash = shared op
    (task leg), no slash = this pack's own flow (flow leg)."""

    def __init__(self, ref: str, *, retry: Optional[dict] = None,
                 retries: Optional[int] = None, timeout: Optional[str] = None,
                 environment: Optional[str] = None, trigger: Optional[str] = None,
                 **wires: Any) -> None:
        super().__init__()
        self.ref = str(ref)
        self.retry = dict(retry) if retry else (
            {"max_attempts": int(retries)} if retries else None)
        self.timeout = timeout
        self.environment = environment
        self.trigger = trigger
        self.wires = dict(wires)
        self.when_pred: Optional[Predicate] = None

    @property
    def leg_kind(self) -> str:
        return "task" if "/" in self.ref else "flow"


class gate(_StepLike):
    """STOP for an answer. Named HITL form: ``gate(approvers=..., show=[...])``
    (assignment name = gate name). Anonymous data form: ``gate(predicate)`` —
    the compiler synthesizes a stable audited name from the predicate (D7)."""

    def __init__(self, predicate: Optional[Predicate] = None, *,
                 approvers: Optional[str] = None, timeout: Optional[str] = None,
                 show: Sequence[Union[OutRef, str]] = ()) -> None:
        super().__init__()
        if predicate is not None and not isinstance(predicate, Predicate):
            raise CompileScopeError(
                "gate(...) takes a comparison over refs (e.g. "
                "gate(prep.out.rows >= 1000)) or keyword-only HITL config — "
                f"got {type(predicate).__name__}."
            )
        self.predicate = predicate
        self.approvers = approvers
        self.timeout = timeout
        self.show = list(show)
        self.when_pred: Optional[Predicate] = None


def when(predicate: Predicate, target: "step | loop") -> "step | loop":
    """ROUTE: a false predicate skips ``target`` (verdict ``routed_off``) and
    the pipeline continues (#1251). Routing, not stopping — that's ``gate``."""
    if not isinstance(predicate, Predicate):
        raise CompileScopeError("when(...) needs a comparison over refs.")
    target.when_pred = predicate
    return target


class loop(_StepLike):
    """The only cycle — bounded at parse (``max_iterations`` mandatory),
    escalating by default (``on_exhausted="gate"``)."""

    def __init__(self, *steps: step, until: Predicate,
                 max_iterations: Optional[int] = None,
                 on_exhausted: str = "gate") -> None:
        super().__init__()
        if max_iterations is None:
            raise CompileScopeError(
                "loop(...) requires max_iterations — unbounded loops fail lint "
                "by design (ADR-0087 / shipped loop grammar)."
            )
        self.steps = list(steps)
        self.until = until
        self.max_iterations = int(max_iterations)
        self.on_exhausted = on_exhausted
        self.when_pred: Optional[Predicate] = None


class _FlowBuilder(_StepLike):
    """MAKE a flow here: ``flow.data("prepare", **cfg).stage(...)...`` — emits
    ``flows/<name>.yaml`` AND stands in the pipeline as the leg that runs it."""

    def __init__(self, kind: str, name: str, **wires: Any) -> None:
        super().__init__()
        self.kind = kind
        self.flow_name = name
        self.wires = dict(wires)
        self.stages: list[dict] = []
        self.when_pred: Optional[Predicate] = None

    def stage(self, ref: str, *, checkpoint: bool = False,
              resources: Optional[dict] = None,
              **config: Any) -> "_FlowBuilder":
        # H6a (#1248) — `checkpoint=True` declares this stage's working set
        # durable (ADR-0088 D1); everything else is stage config.
        #
        # ADR-0089 (#1350) — `resources={"cpu": 8, "memory_mb": 32000, "gpu": 1}`
        # is the engine-neutral StageResources hint the contract already carries;
        # engines map it natively (Metaflow @resources, k8s requests/limits).
        # A dedicated kwarg rather than bare cpu=/gpu= because stage CONFIG keys
        # are arbitrary — `cpu` could legitimately BE a kernel's config field.
        self.stages.append({"stage": ref, "config": dict(config),
                            "checkpoint": checkpoint,
                            "resources": dict(resources) if resources else None})
        return self


class _AgenticLeg(_StepLike):
    """An agentic flow leg (template) — the body lives in ``flows/<name>.md``.

    #1301 — ``prompt=`` IS that body: the build emits the md (frontmatter +
    prose, the single schema-validated prompt-node shape — no unbounded tool
    loop, by design). Without it the author must ship ``flows/<name>.md``
    themselves; the build says so in a note rather than emitting a leg that
    points at nothing silently.
    """

    def __init__(self, name: str, *, model: Optional[str] = None,
                 prompt: Optional[str] = None, output: Optional[str] = None,
                 reads: Sequence[OutRef] = (), **wires: Any) -> None:
        super().__init__()
        self.flow_name = name
        self.model = model
        self.prompt = prompt
        self.output = output          # optional output-schema name
        self.reads = list(reads)
        self.wires = dict(wires)
        self.when_pred: Optional[Predicate] = None


class _FlowNamespace:
    def data(self, name: str, **wires: Any) -> _FlowBuilder:
        return _FlowBuilder("data", name, **wires)

    def ml(self, name: str, **wires: Any) -> _FlowBuilder:
        return _FlowBuilder("ml", name, **wires)

    def agentic(self, name: str, **kw: Any) -> _AgenticLeg:
        return _AgenticLeg(name, **kw)


flow = _FlowNamespace()


class _Chain:
    """``graph = (a >> b >> gate(...) >> c)`` — CONTROL edges. D4: these may
    only ADD ordering; contradicting or omitting a data edge is a compile error."""

    def __init__(self, items: list[Any]) -> None:
        self.items = items

    def __rshift__(self, other: Any) -> "_Chain":
        if isinstance(other, _Chain):
            return _Chain(self.items + other.items)
        return _Chain(self.items + [other])


class parallel:
    """CONCURRENT chain position: ``a >> parallel(b, c) >> d`` orders b and c
    after a, d after BOTH, and adds NO edge between b and c. Pure fan-out /
    fan-in shorthand — a member consuming a sibling's output is a compile
    error (the picture would lie, D4)."""

    def __init__(self, *steps: Any) -> None:
        if len(steps) < 2:
            raise CompileScopeError(
                "parallel(...) needs at least two steps — one step is just "
                "a chain position."
            )
        self.steps = list(steps)

    def __rshift__(self, other: Any) -> _Chain:
        return _Chain([self]) >> other


class cron:
    """Entry trigger (P1: captured; compiled to a build note — see module doc)."""

    def __init__(self, expr: str) -> None:
        self.expr = expr


class watch:
    """Data-landing trigger (P1: captured; compiled to a build note)."""

    def __init__(self, uri: str, *, debounce: str = "") -> None:
        self.uri, self.debounce = uri, debounce


def pipeline(*, env: str = "", series: str = "", budget_usd: Optional[float] = None,
             approvers: Optional[str] = None, notify: Sequence[str] = (),
             pack: str = ""):
    """Class decorator: stamps pipeline-level governance onto the class for the
    compiler. The class is a VIEW — ``cairn pack build`` compiles it to the
    record, and the record is what runs (D1/D6). ``pack`` names the pack the
    build targets — lifted ``@step`` defs need it for their op refs
    (``<pack>/_steps/<pipeline>.<step>``); defaults to the pipeline name."""

    def _wrap(cls: type) -> type:
        cls.__cairn_pipeline__ = {
            "env": env, "series": series, "budget_usd": budget_usd,
            "approvers": approvers, "notify": list(notify), "pack": pack,
        }
        return cls

    return _wrap


__all__ = [
    "CompileScopeError", "cron", "flow", "gate", "inp", "loop", "parallel",
    "pipeline", "step", "watch", "when",
]

from .expr import inp  # noqa: E402  — re-export for the one-line author import
