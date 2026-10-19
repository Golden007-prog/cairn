from __future__ import annotations

import json
import time
from typing import Any

import litellm
from litellm import acompletion

from cairn.core import obs
from cairn.core.provider_settings import get_settings
from cairn.core.obs.telemetry import current_run_id, set_span_attr_mirrored, span, trace_content, tracing_active
from cairn.services.llm.gateway import get_gateway
from cairn.core.llm.models import resolve_model_name


litellm.drop_params = True


def _gen_ai_system(model: str) -> str:
    """OTel GenAI ``gen_ai.system`` (provider) from a litellm model string."""
    m = (model or "").lower()
    if "claude" in m or m.startswith("anthropic"):
        return "anthropic"
    if m.startswith(("gpt", "o1", "o3", "o4")) or "openai" in m:
        return "openai"
    if "gemini" in m or "google" in m:
        return "google"
    if "/" in m:  # litellm "<provider>/<model>" form
        return m.split("/", 1)[0]
    return "litellm"


def _json_str(obj: Any) -> str:
    """Best-effort compact JSON for span content (never raises)."""
    try:
        return json.dumps(obj, default=str)[:1_000_000]  # generous cap; full prompt by design (#242 P3)
    except (TypeError, ValueError):
        return str(obj)


def _finish_reason(response: Any) -> str | None:
    """``choices[0].finish_reason`` off a litellm response (#476). Best-effort
    per provider — absent on some providers/mocks, in which case ``None``."""
    try:
        choices = getattr(response, "choices", None) or []
        if choices:
            return getattr(choices[0], "finish_reason", None)
    except Exception:  # noqa: BLE001 — capture must never break the call
        pass
    return None


def _cached_tokens(usage: Any) -> int | None:
    """Prompt-cache-hit token count off litellm usage (#476), when the
    provider reports it (``usage.prompt_tokens_details.cached_tokens``).
    Best-effort per provider — most providers/mocks don't set this."""
    try:
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            return getattr(details, "cached_tokens", None)
    except Exception:  # noqa: BLE001 — capture must never break the call
        pass
    return None


def _schema_valid(response_format: Any, response: Any) -> bool | None:
    """Did the completion parse as valid JSON against a requested
    ``response_format`` (#476)? Only meaningful (non-None) when
    ``response_format`` was actually requested for this call — otherwise the
    flag doesn't apply and stays ``None``. This checks JSON-parseability of
    the completion text only (a cheap, provider-agnostic proxy); it does not
    validate against the schema's field constraints."""
    if not response_format:
        return None
    text = _completion_text(response)
    if not text:
        return False
    try:
        json.loads(text)
        return True
    except (TypeError, ValueError):
        return False


def _completion_text(response: Any) -> str:
    """Extract the assistant text from a litellm response (best-effort)."""
    try:
        choices = getattr(response, "choices", None) or []
        if choices:
            msg = getattr(choices[0], "message", None)
            content = getattr(msg, "content", None) if msg is not None else None
            if content:
                return content
            # tool-call / structured replies carry no plain content
            tool_calls = getattr(msg, "tool_calls", None) if msg is not None else None
            if tool_calls:
                return _json_str([getattr(tc, "function", tc) for tc in tool_calls])
    except Exception:  # noqa: BLE001 — span content must never break the call
        pass
    return ""


#: The LiteLLM gateway's own per-call accounting, as litellm's client forwards it
#: (``llm_provider-`` prefixed) and as the proxy sends it.
_GATEWAY_COST_HEADERS = ("llm_provider-x-litellm-response-cost", "x-litellm-response-cost")


def _reported_cost(response: Any) -> float | None:
    """The cost the call itself reported (#1441): the gateway's
    ``x-litellm-response-cost`` header first, else litellm's per-call
    ``response_cost``. ``None`` when neither is present or positive -- a reported
    0.0 for a call that used tokens is "unmapped", not "free", so it falls through
    to the registry / price-map lookup in ``obs.record_llm``."""
    try:
        hidden = getattr(response, "_hidden_params", None) or {}
        headers = hidden.get("additional_headers") or {}
        candidates = [headers.get(h) for h in _GATEWAY_COST_HEADERS] + [hidden.get("response_cost")]
        for raw in candidates:
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
    except Exception:  # noqa: BLE001 -- capture must never break the call
        pass
    return None


def _is_openai_model(model: str) -> bool:
    """True when litellm will route ``model`` to OpenAI: no provider prefix
    (litellm's default provider) or an explicit ``openai/`` prefix."""
    m = str(model or "")
    return "/" not in m or m.lower().startswith("openai/")


async def llm_complete(
    messages: list[dict[str, str]],
    model: str,
    temperature: float = 0.3,
    tools: list[dict] | None = None,
    response_format: Any = None,
    stream: bool = False,
    **kwargs,
):
    settings = get_settings()
    model = resolve_model_name(model)

    # Redaction chokepoint (ADR-0028): redact message content before anything
    # leaves to the vendor. No-op pass-through unless a redactor is bound via
    # the `redact.text` profile binding.
    messages = await obs.redact_messages(messages)

    job_tag = kwargs.pop("job_tag", None)

    params: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": stream,
        **kwargs,
    }
    if tools:
        params["tools"] = tools
    if response_format:
        params["response_format"] = response_format

    # The OpenAI key is OpenAI's: a litellm "<provider>/<model>" id for another
    # provider (deepseek/…, anthropic/…) reads its own key from the env, and
    # forcing this one onto it fails auth with a misleading "invalid api key"
    # (#1439 — the curation judge on deepseek/deepseek-v4-flash, direct
    # gateway). The proxy gateway overwrites api_key anyway.
    if settings.openai_api_key and _is_openai_model(model):
        params["api_key"] = settings.openai_api_key

    params = get_gateway().apply(params, job_tag=job_tag or "")

    attrs = {
        "llm.model": model,
        "llm.temperature": temperature,
        "llm.message_count": len(messages),
        "llm.has_tools": bool(tools),
        "llm.structured_output": bool(response_format),
        # OTel GenAI semantic conventions (#242 Phase 3, subsumes #79) — the
        # attributes Langfuse maps to render the call. Content attrs
        # (``gen_ai.prompt`` / ``gen_ai.completion``) go through the trace
        # content-capture gate (#247): CAIRN_TRACE_CONTENT off | redacted | raw.
        "gen_ai.system": _gen_ai_system(model),
        "gen_ai.request.model": model,
        "gen_ai.request.temperature": temperature,
    }
    # #1465 — join LLM traces to the cairn run they served (contextvar set by
    # the executor, else the env the remote-job paths export). Absent = unset.
    _run = current_run_id()
    if _run:
        attrs["cairn.run_id"] = _run
    # Only touch the redactor when a span will actually emit (tracing active).
    prompt_content = await trace_content(_json_str(messages)) if tracing_active() else None
    if prompt_content is not None:
        attrs["gen_ai.prompt"] = prompt_content
    with span("llm.complete", attrs) as s:
        t0 = time.perf_counter()
        response = await acompletion(**params)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        # Usage is available only on non-stream responses; stream usage
        # arrives at end-of-stream and would need an aggregating wrapper.
        # Cost for streamed calls is currently uncounted — known gap.
        usage = getattr(response, "usage", None) if not stream else None

        finish_reason = _finish_reason(response)
        cached_tokens = _cached_tokens(usage) if usage else None
        schema_valid = _schema_valid(response_format, response)

        if s is not None:
            # completion content → Langfuse "output", through the same #247 gate.
            completion_content = await trace_content(_completion_text(response))
            if completion_content is not None:
                # Mirrors onto OpenInference `output.value` too (#476) — same
                # span, extra attribute, no new exporter.
                set_span_attr_mirrored(s, "gen_ai.completion", completion_content, openinference_key="output.value")
            if usage:
                pt = getattr(usage, "prompt_tokens", 0)
                ct = getattr(usage, "completion_tokens", 0)
                s.set_attribute("llm.prompt_tokens", pt)
                s.set_attribute("llm.completion_tokens", ct)
                s.set_attribute("llm.total_tokens", getattr(usage, "total_tokens", 0))
                # GenAI semconv token names (what Langfuse reads for usage/cost)
                s.set_attribute("gen_ai.usage.input_tokens", pt)
                s.set_attribute("gen_ai.usage.output_tokens", ct)
                s.set_attribute("llm.token_count.prompt", pt)  # OpenInference (#476)
                s.set_attribute("llm.token_count.completion", ct)  # OpenInference (#476)
            # #476 additive span attrs — best-effort, only set when observed.
            set_span_attr_mirrored(s, "gen_ai.response.finish_reason", finish_reason)
            set_span_attr_mirrored(s, "cache.cached_tokens", cached_tokens)
            set_span_attr_mirrored(s, "llm.schema_valid", schema_valid)

        if usage:
            # No-op outside a request scope; BudgetExceeded propagates up
            # and surfaces as an SSE error frame from the routes layer.
            obs.record_llm(
                model=model,
                in_tok=getattr(usage, "prompt_tokens", 0) or 0,
                out_tok=getattr(usage, "completion_tokens", 0) or 0,
                ms=elapsed_ms,
                finish_reason=finish_reason,
                cached_tokens=cached_tokens,
                time_to_first_token_ms=None,  # best-effort per provider: needs a streaming wrapper (not wired here)
                schema_valid=schema_valid,
                usd=_reported_cost(response),
            )

        # Audit seam (ADR-0028): record the data-egress to the vendor. No-op
        # fast path unless an audit sink is bound via `audit.sink`.
        await obs.audit_emit(
            "llm.complete",
            target=model,
            message_count=len(messages),
            has_tools=bool(tools),
            prompt_tokens=getattr(usage, "prompt_tokens", 0) if usage else None,
            completion_tokens=getattr(usage, "completion_tokens", 0) if usage else None,
        )
        return response
