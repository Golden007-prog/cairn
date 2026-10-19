"""A real operator test harness (ADR-0092 principle 3, #1544).

Pack tests used to hand-roll the invocation::

    ctx = OperationContext(pipeline_id="t", node_name="n")
    out = await MyOp().run(MyInput(...), ctx)

which calls the operator body and NOTHING ELSE. Everything the engine actually
does around an operator was skipped: config layering (ADR-0068), the #905
governance-strip, input validation against ``input_schema``, secret scoping
(§24.20.13) and the mock decision. So an operator could pass its own suite and
still be broken under the engine — and, worse, a regression in the shared
layering code could not fail a single pack's tests.

:func:`run_operator_for_test` drives the REAL path instead:
:func:`cairn.core.operator.runtime.invoke_operator_core` — the ONE invoke path
#1542 extracted, the same function the template operation node and the pipeline
``task:`` leg call. If config layering regresses, a pack test that passes
``config=`` goes red.

What it does NOT cover, deliberately: binding resolution (``$state.*`` /
``$nodes.*``) and executor picking / the approval chokepoint live ABOVE
``invoke_operator_core``. A test that needs those wants
``invoke_operator_inprocess`` / ``invoke_operator`` with a real
``OperationBinding`` — this harness is for "run this operator the way the engine
runs it", not "compile this graph".
"""
from __future__ import annotations

from typing import Any, Type

from cairn.core.operator.protocol import Operation, SecretStore
from cairn.core.operator.reports import (  # ADR-0103
    PARAMETERS_EVENT_TYPE,
    ListSink,
    Reporter,
    ReportSpec,
)
from cairn.core.operator.runtime import invoke_operator_core
from cairn.core.operator.secrets import ScopedSecretStore


class _DictSecretBackend:
    """An in-memory :class:`SecretStore` backing for tests.

    Wrapped in the REAL :class:`ScopedSecretStore`, so the §24.20.13 scoping
    rule (an undeclared key raises ``KeyError`` before the backend is touched)
    is exercised exactly as in production — only the *backing* is a dict
    instead of the profile-bound provider.
    """

    def __init__(self, values: dict[str, str]) -> None:
        self._values = dict(values)

    def get(self, name: str) -> str:
        if name not in self._values:
            raise KeyError(f"secret {name!r} not supplied to run_operator_for_test")
        return self._values[name]

    def available_keys(self) -> list[str]:
        return sorted(self._values)


async def run_operator_for_test(
    op_cls: Type[Operation],
    input: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    workflow_config: dict[str, Any] | None = None,
    run_config: dict[str, Any] | None = None,
    secrets: dict[str, str] | None = None,
    mocked: bool = False,
    ctx_overrides: dict[str, Any] | None = None,
    pipeline_id: str = "test-run",
    node_name: str = "test-node",
    report_sink: list[tuple[str, dict[str, Any]]] | None = None,
) -> Any:
    """Invoke ``op_cls`` the way the engine does and return its output model.

    :param input: the resolved operator input as a plain dict — what a binding
        would have produced. Validated against ``op_cls.input_schema`` by the
        real path, so a wrong shape fails here exactly as it would in a run.
    :param config: the operator's own ADR-0068 config values (NOT keyed by
        operator name — the harness keys them). This is the env/**profile**
        layer, the one that owns governance knobs.
    :param workflow_config: the saved per-workflow layer (ADR-0070).
    :param run_config: the per-run ``config_overrides`` layer — highest
        precedence of the three. Passing all three in one call is how a test
        pins the layering ORDER, not just that some config arrived.
    :param secrets: ``{KEY: value}`` made readable through ``ctx.secrets``. The
        keys double as the binding's declared ``secrets:`` list, so scoping is
        enforced for real: an operator reading an unlisted key raises.
    :param mocked: take the operator's ``mock()`` path (``cairn run --mock``).
    :param ctx_overrides: extra :class:`OperationContext` attributes to set —
        e.g. ``{"compute": ResolvedConnection(...)}`` for a remote-job operator,
        or a stub ``artifacts`` store. Unknown names fail loud.

    :returns: an instance of ``op_cls.output_schema``. The real path returns the
        state-bound dict; the harness re-validates it so assertions stay
        attribute-shaped (``out.verdict``) — and a malformed output is caught.
    """
    op_name = getattr(op_cls, "name", op_cls.__name__)
    overrides = dict(ctx_overrides or {})
    # ADR-0103 — under test the reporter is STRICT (an undeclared name raises)
    # and writes to an in-memory sink, so a pack test sees exactly what the
    # engine would have appended, including the runtime's ``parameters`` event.
    if "report" not in overrides:
        overrides["report"] = Reporter(
            spec=getattr(op_cls, "reports", None) or ReportSpec.NONE,
            node=node_name, sink=ListSink(report_sink), strict=True,
        )
    secret_keys: list[str] | None = None
    if secrets:
        secret_keys = sorted(secrets)
        scoped: SecretStore = ScopedSecretStore(_DictSecretBackend(secrets), secret_keys)
        overrides.setdefault("secrets", scoped)

    out_dict = await invoke_operator_core(
        op_cls,
        dict(input),
        pipeline_id=pipeline_id,
        node_name=node_name,
        profile_config={op_name: dict(config)} if config else None,
        workflow_config={op_name: dict(workflow_config)} if workflow_config else None,
        run_overrides={op_name: dict(run_config)} if run_config else None,
        secret_keys=secret_keys,
        is_mocked=mocked,
        ctx_overrides=overrides or None,
    )
    return op_cls.output_schema.model_validate(out_dict)


__all__ = ["assert_reports_conform", "run_operator_for_test"]


async def assert_reports_conform(
    op_cls: Type[Operation],
    input: dict[str, Any],
    **kwargs: Any,
) -> list[tuple[str, dict[str, Any]]]:
    """ADR-0103 conformance: a ``reports`` declaration cannot be decorative.

    Runs the operator through :func:`run_operator_for_test` (mocked when it
    ``supports_mock``, live otherwise — pass ``mocked=`` to choose) and asserts
    that every declared series and scalar name was actually emitted, and that
    an operator declaring stages emitted at least one ``progress`` event.
    Returns the captured ``(type, payload)`` events for further assertions.
    The runtime's ``parameters`` event must be present for every operator.
    """
    spec = getattr(op_cls, "reports", None)
    assert isinstance(spec, ReportSpec), (
        f"{getattr(op_cls, 'name', op_cls.__name__)}: no `reports` declaration on the class (ADR-0103)"
    )
    events: list[tuple[str, dict[str, Any]]] = []
    if "mocked" not in kwargs:
        kwargs["mocked"] = bool(getattr(op_cls, "supports_mock", False))
    await run_operator_for_test(op_cls, input, report_sink=events, **kwargs)
    types = [t for t, _ in events]
    assert PARAMETERS_EVENT_TYPE in types, "the runtime did not record the step's parameters"
    seen_series: set[str] = set()
    seen_scalars: set[str] = set()
    for typ, payload in events:
        if typ == "metrics":
            seen_series.update((payload.get("series") or {}).keys())
        elif typ == "scalars":
            seen_scalars.update((payload.get("values") or {}).keys())
    missing_series = sorted(spec.series_names - seen_series)
    missing_scalars = sorted(set(spec.scalars) - seen_scalars)
    assert not missing_series, f"declared series never emitted: {missing_series}"
    assert not missing_scalars, f"declared scalars never emitted: {missing_scalars}"
    if spec.stages:
        assert "progress" in types, "stages declared but no progress event was emitted"
    return events
