"""Cost counterfactual — "what would this run have cost without cairn"
(ADR-0059 §D3 ``cost_baseline_usd``, epic #480 Slice F).

The "reduces cost, provably" answer the Insights dashboard (#481, ADR-0059
§D5) needs a number for: the **naive baseline** is what a run would have cost
if every LLM call in it had gone to the strong/frontier model, uncached, with
no deterministic gate short-circuiting it first — i.e. the "just always call
GPT-4-class-and-hope" alternative cairn's gating/caching/model-routing beats.
``cost_savings_usd = cost_baseline_usd - actual_usd``. Since #1441 the actual
cost prefers what each call REPORTED (the gateway's own figure, then litellm's
per-call cost) over the table lookup the baseline uses, so a run that used the
strong model throughout can show a small non-zero — even negative — saving.

**One pricing source, not a fork:** this module does NOT hand-maintain its
own price table. It reuses ``cairn.core.obs.cost._price`` (the table lookup
``record_llm`` falls back to when a call reports no cost of its own), so the
baseline is priced off the same
registry (``cairn.core.llm.models`` — yaml default → ``MODEL_STRONG_NAME`` env
override → a persisted role reassignment), falling through to litellm's price
map (``litellm.model_cost``) since #1441; this module just calls whatever
``_price`` does, so it inherits that for free (an unpriced model reads 0.0 here).

**Known gap (documented, not invented):** per-call token counts
(``prompt_tokens``/``completion_tokens`` per LLM call) are emitted only as
OTel span attributes and fleet metrics (``core/llm/client.py`` → ``obs.record_llm``),
which is the ClickHouse/Langfuse tier by design (ADR-0059 §D2 — unbounded,
aggregate-at-volume data never transits Postgres). ``Run``/``RunResult`` only
carry the already-aggregated ``llm_cost_usd`` float (``core/engine/schema.py``
``RunResult.llm_cost_usd`` → ``core/dispatch/dispatcher.py`` ``_aggregate_cost``).
So at the point a run finishes (where ``cost_baseline_usd`` would be persisted),
this module cannot reconstruct the exact per-call token mix that produced
``llm_usd`` — doing so would require plumbing token counts through the run
result schema (a schema change out of scope for this read-only console-API
slice) or joining against ClickHouse by ``trace_id`` (the #477/#478 continuous-
eval path, not built yet).

Two computation paths, both real (never fabricated):

  - :func:`compute_cost_baseline` — the precise path, given actual per-call
    ``LlmCallUsage`` records (token counts per call). Exact for callers that DO
    have per-call data (e.g. a future in-process capture hook, or a batch job
    over ClickHouse-joined spans). Unit-tested directly.
  - :func:`estimate_cost_baseline_from_actual` — the best-effort fallback used
    today by the dispatcher's completion hook: scales the run's already-
    aggregated ``llm_usd`` by the ratio of the strong model's blended rate
    (``_price(strong, 1000, 1000)``) to the *cheapest* role actually available
    (same blended rate at the same token mix), i.e. "how much MORE would this
    have cost had every token gone through the strong model instead of
    whatever roles actually served it". Degrades to ``None`` (not a fabricated
    0) when ``llm_usd`` is 0/absent — a run with no LLM cost has no
    counterfactual to report.
"""
from __future__ import annotations

from dataclasses import dataclass

from cairn.core.llm.models import resolve_model_name
from cairn.core.obs.cost import _price


@dataclass(frozen=True)
class LlmCallUsage:
    """One LLM call's token counts — the precise-path input.

    ``model`` is unused by :func:`compute_cost_baseline` itself (every call is
    priced at the strong model regardless of what it actually used) but is
    kept on the record for callers that want to compare actual-vs-baseline
    per call. Token counts mirror ``core/llm/client.py``'s
    ``usage.prompt_tokens``/``usage.completion_tokens``.
    """

    model: str
    prompt_tokens: int
    completion_tokens: int


def _strong_model_name(role: str = "strong") -> str:
    """The concrete model name the ``strong`` role (or a caller-given role)
    resolves to — reuses the model-role registry so the name always matches
    whatever the deployment actually has that role bound to (yaml default,
    ``MODEL_STRONG_NAME`` env override, or a persisted role reassignment)."""
    return resolve_model_name(role)


def compute_cost_baseline(
    calls: list[LlmCallUsage], *, strong_role: str = "strong"
) -> float:
    """Naive baseline: price every call's tokens at the strong-model rate via
    ``cairn.core.obs.cost._price`` (the SAME pricing function real calls are
    billed through), ignoring which model/cache/gate actually served it. This
    is the "no routing, no caching, always the frontier model" counterfactual.

    Returns 0.0 for an empty call list (no LLM activity → no counterfactual
    cost) — callers that want ``None`` for "not computed" instead of "computed
    to zero" should check ``calls`` before calling this.
    """
    strong_name = _strong_model_name(strong_role)
    total = sum(_price(strong_name, c.prompt_tokens, c.completion_tokens) for c in calls)
    return round(total, 6)


def estimate_cost_baseline_from_actual(
    llm_usd: float, *, actual_role: str = "fast", strong_role: str = "strong"
) -> float | None:
    """Best-effort fallback when per-call token data isn't reachable (the
    documented gap above): scale the run's already-aggregated ``llm_usd`` by
    the ratio of the strong model's price to ``actual_role``'s price, both
    computed via the SAME ``_price`` function at an identical token mix (so
    the ratio is exactly the models' relative $/token, independent of which
    mix is used to compute it).

    This is an approximation, not a reconstruction: it assumes the run's
    actual token mix would cost proportionally more/less under the strong
    model's rate — true unless the run's own input/output ratio is wildly
    skewed relative to the roles' own input/output rate skew. Good enough for
    an Insights-dashboard "provable direction" number; NOT a substitute for
    :func:`compute_cost_baseline` when real per-call data is available.

    Returns ``None`` (never a fabricated 0.0) when ``llm_usd`` is falsy — a
    run with no recorded LLM cost has no baseline to estimate.
    """
    if not llm_usd:
        return None
    strong_name = _strong_model_name(strong_role)
    actual_name = _strong_model_name(actual_role)
    # identical token mix on both sides — the ratio is purely the two models'
    # relative $/token, whatever mix is used to sample it.
    probe_in, probe_out = 1000, 1000
    strong_price = _price(strong_name, probe_in, probe_out)
    actual_price = _price(actual_name, probe_in, probe_out)
    if actual_price <= 0:
        return None
    ratio = strong_price / actual_price
    return round(llm_usd * ratio, 6)


__all__ = [
    "LlmCallUsage",
    "compute_cost_baseline",
    "estimate_cost_baseline_from_actual",
]
