"""#530: template LLM calls attribute cost into Run.cost (was 0.0)."""
import os
from types import SimpleNamespace

os.environ.setdefault("OPENAI_API_KEY", "sk-test")

from cairn.core import obs
from cairn.services.llm.factory import BudgetCallback, _extract_usage, build_chat_model


def test_build_chat_model_requests_streaming_usage():
    # the root cause: streamed agent calls omit usage without this
    m = build_chat_model("gpt-4o-mini")
    assert getattr(m, "stream_usage", False) is True


def test_extract_usage_from_usage_metadata():
    # streaming path: usage rides on the message's usage_metadata
    msg = SimpleNamespace(usage_metadata={"input_tokens": 120, "output_tokens": 30})
    resp = SimpleNamespace(llm_output=None, generations=[[SimpleNamespace(message=msg)]])
    assert _extract_usage(resp) == (120, 30)


def test_extract_usage_from_llm_output():
    resp = SimpleNamespace(llm_output={"token_usage": {"prompt_tokens": 50, "completion_tokens": 10}})
    assert _extract_usage(resp) == (50, 10)


def test_budget_callback_records_cost_into_scope():
    cb = BudgetCallback("gpt-4o-mini")
    msg = SimpleNamespace(usage_metadata={"input_tokens": 1000, "output_tokens": 500})
    resp = SimpleNamespace(llm_output=None, generations=[[SimpleNamespace(message=msg)]])
    with obs.cost_scope("t") as ctx:
        cb.on_llm_end(resp)
        # gpt-4o-mini priced in the table → non-zero cost (was 0.0 pre-#530)
        assert ctx.cost_usd > 0.0
