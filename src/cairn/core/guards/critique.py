"""
Self-critique / reflection loop — a deterministic quality gate over LLM output.

The old LangGraph agent had a ``self_critique_node``: a second LLM call that
reviewed the synthesis, and looped back to re-synthesize when confidence was low
(bounded by a pass counter). The templates_v2 rewrite dropped it. This restores
it as an **opt-in, flow-agnostic** post-processing loop driven by
:class:`~cairn.core.engine.schema.CritiqueConfig`.

Flow (only when ``spec.critique.enabled``):

    output = template.run(input)
    for pass in range(min(critique.max_passes, budget.max_iterations)):
        verdict = critique_llm(output)            # structured CritiqueVerdict
        if verdict.acceptable or verdict.confidence >= threshold:
            break
        output = template.run(input + critique feedback)   # revise

The critique LLM is built through :func:`build_chat_model`, so it inherits the
gateway routing and the per-investigation cost budget like every other call. The
full verdict trail is attached to the output as ``critique_trail`` for audit.

Note: a revise pass re-invokes the *whole* template (re-fetching for agent_loop).
That's intentional — a critique like "you never checked traces" can only be acted
on by re-running — but it's why ``max_passes`` defaults to 1 and is hard-capped by
``budget.max_iterations``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from pydantic import BaseModel, Field

from cairn.core.engine.compiled import CompiledTemplate
from cairn.core.clients.llm_client import build_chat_model
from cairn.core.engine.schema import CritiqueConfig

_log = logging.getLogger("cairn.critique")


class CritiqueVerdict(BaseModel):
    """Structured review of a template's output."""

    acceptable: bool = Field(description="Is the output good enough to ship as-is?")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence the output is correct/complete")
    issues: list[str] = Field(default_factory=list, description="Concrete problems found")
    suggested_revision: Optional[str] = Field(
        default=None, description="What the next attempt should do differently"
    )


_CRITIQUE_SYSTEM = (
    "You are a senior incident reviewer auditing another agent's investigation "
    "output. Judge it for: factual support (are claims backed by cited evidence?), "
    "completeness (did it miss an obvious data source?), and plausibility of the "
    "root cause. Be strict — an unsupported or hand-wavy RCA is NOT acceptable. "
    "Return your verdict in the required structured form."
)


def _output_text(template_output: Any) -> str:
    """Extract the reviewable text from a template output dict."""
    if isinstance(template_output, dict):
        return str(template_output.get("output", "")) or ""
    return str(template_output or "")


async def _critique_once(output_text: str, config: CritiqueConfig) -> CritiqueVerdict:
    """One structured critique LLM call. Isolated so tests can monkeypatch it."""
    llm = build_chat_model(config.model, temperature=0.0).with_structured_output(
        CritiqueVerdict
    )
    from langchain_core.messages import HumanMessage, SystemMessage

    messages = [
        SystemMessage(content=_CRITIQUE_SYSTEM),
        HumanMessage(content=f"Run output to review:\n\n{output_text}"),
    ]
    return await llm.ainvoke(messages)


def _revise_input(input_data: dict[str, Any], verdict: CritiqueVerdict) -> dict[str, Any]:
    """Augment the original input with critique feedback for a revise pass."""
    feedback_lines = ["", "── Reviewer feedback on your previous attempt ──"]
    feedback_lines += [f"- {issue}" for issue in verdict.issues]
    if verdict.suggested_revision:
        feedback_lines.append(f"Do this differently: {verdict.suggested_revision}")
    feedback = "\n".join(feedback_lines)
    revised = dict(input_data)
    revised["user_message"] = str(revised.get("user_message", "")) + "\n" + feedback
    return revised


async def run_critique_loop(
    compiled: CompiledTemplate,
    template_output: dict[str, Any],
    input_data: dict[str, Any],
    *,
    max_iterations_cap: Optional[int] = None,
) -> dict[str, Any]:
    """Run the opt-in critique/revise loop. No-op unless ``spec.critique.enabled``.

    ``max_iterations_cap`` (from ``budget.max_iterations``) hard-caps the number of
    revise passes regardless of ``critique.max_passes``.
    """
    config: Optional[CritiqueConfig] = getattr(compiled.spec, "critique", None)
    if config is None or not config.enabled:
        return template_output

    passes = config.max_passes
    if max_iterations_cap is not None:
        passes = min(passes, max_iterations_cap)
    if passes <= 0:
        return template_output

    output = template_output
    trail: list[dict[str, Any]] = []
    accepted = False
    for i in range(passes):
        verdict = await _critique_once(_output_text(output), config)
        trail.append(verdict.model_dump())
        if verdict.acceptable or verdict.confidence >= config.confidence_threshold:
            accepted = True
            break
        _log.info(
            "critique pass %d/%d: revising (confidence=%.2f, %d issue(s))",
            i + 1, passes, verdict.confidence, len(verdict.issues),
        )
        # Revise: re-invoke the whole template with feedback injected.
        output = await compiled.run(_revise_input(input_data, verdict))

    if not isinstance(output, dict):
        output = {"output": output}
    output = dict(output)
    output["critique_trail"] = trail
    output["critique_passes"] = len(trail)
    output["critique_accepted"] = accepted
    return output
