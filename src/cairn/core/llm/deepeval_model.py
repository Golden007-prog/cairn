"""The deepeval adapter that keeps judge traffic on cairn's gateway (#1730).

deepeval — and everything built on it (deepteam, the eval pack's G-Eval judge) —
calls a vendor SDK directly unless it is handed a ``DeepEvalBaseLLM``. This
factory returns one bound to :func:`cairn.core.llm.client.llm_complete`, so
redaction (ADR-0028), budget, cost metering and audit all apply ambiently.

**Why this lives in core rather than in a pack.** It was duplicated between
``cairn-operator-eval`` and ``cairn-operator-redteam``; cross-operator-pack imports are not
allowed, so the second copy was the ADR-0092 §1 debt coming due. Lifting it here
follows #1544, which moved ``as_ssh_params`` into ``cairn.core.connections`` for
exactly this reason. Packs may import ``cairn.core.*`` — the rule they must not
break is importing from *each other*.

**core gains no dependency on deepeval**: the import is lazy, inside the
factory, so ``import cairn`` works without the ``[eval]`` / ``[redteam]`` extra
and the operators degrade to ``checked=false`` as they already do.

**The one parameter that is not cosmetic — ``accept_kwargs``.** The two copies
had diverged, in opposite directions, and both behaviours are load-bearing:

* deepeval's judge path calls ``generate(prompt)`` and tolerates extra
  arguments, so the eval copy accepted ``*args, **kwargs``;
* **deepteam calls ``generate(prompt, schema=SyntheticDataList)`` and, on
  ``TypeError``, falls back to ``generate(prompt)`` + ``trimAndLoadJson``** —
  its documented path for models without native structured output. A model that
  ACCEPTS and ignores ``schema`` returns a plain string where a parsed object is
  expected, and deepteam then does ``res.data`` → ``AttributeError``, with no
  fallback left. Verified live 2026-08-26.

So the strict arity is a *feature* for deepteam and a bug for deepeval, and vice
versa. It is a named parameter here precisely so a future tidy-up cannot quietly
fold the strict variant back into the permissive one and reintroduce that crash.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any

__all__ = ["build_gateway_model", "content_of"]


def content_of(resp: Any) -> str:
    """Text out of a litellm/openai-shaped completion, liberally."""
    try:
        return resp.choices[0].message.content or ""
    except Exception:  # noqa: BLE001 — be liberal about response shape
        return str(resp)


def build_gateway_model(model: str = "", *, accept_kwargs: bool = True):
    """Return a ``DeepEvalBaseLLM`` whose every call goes through the gateway.

    ``model`` — a gateway-resolved model id; empty means the run's default.

    ``accept_kwargs`` — whether ``generate``/``a_generate`` tolerate extra
    arguments. Leave it ``True`` for deepeval's own scoring paths. Pass
    ``False`` for **deepteam**, whose structured-output fallback is triggered BY
    the ``TypeError`` a strict signature raises; a permissive signature there
    swallows ``schema=`` and crashes deepteam later on ``res.data`` (see the
    module docstring).

    Raises ``ImportError`` if deepeval is absent — callers catch it and report
    ``checked=false`` rather than failing the leg.
    """
    from deepeval.models.base_model import DeepEvalBaseLLM  # noqa: PLC0415 — optional extra

    from cairn.core.llm.client import llm_complete  # noqa: PLC0415 — cycle: client imports operator plumbing

    class _GatewayLLM(DeepEvalBaseLLM):
        """Drives deepeval through cairn's governed chokepoint."""

        def __init__(self, model_name: str) -> None:
            # Deliberately NOT calling super().__init__(): deepeval's base
            # constructor calls load_model(), whose default path builds an
            # OpenAI client and demands OPENAI_API_KEY — the very thing this
            # class exists to avoid.
            self._model = model_name or "default"

        def load_model(self):  # deepeval contract
            return self

        def get_model_name(self) -> str:
            return f"obs-gateway:{self._model}"

        # Force the plain-text path: the gateway returns content, not token
        # logprobs or native structured output.
        def supports_log_probs(self) -> bool:
            return False

        def supports_structured_outputs(self) -> bool:
            return False

        def supports_json_mode(self) -> bool:
            return False

        async def _complete(self, prompt: str) -> str:
            resp = await llm_complete(
                messages=[{"role": "user", "content": prompt}],
                model=self._model, temperature=0.0,
            )
            return content_of(resp)

        def _blocking(self, prompt: str) -> str:
            """Sync bridge. deepteam's simulate() is sync and cairn operators
            run inside a loop, so a bare asyncio.run() raises "cannot be called
            from a running event loop" — hop to a worker thread instead."""
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(self._complete(prompt))
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                return ex.submit(lambda: asyncio.run(self._complete(prompt))).result()

    # Two bodies, one factory. The signatures differ and that IS the contract —
    # see the module docstring before "simplifying" them into one.
    if accept_kwargs:
        class GatewayLLM(_GatewayLLM):
            async def a_generate(self, prompt: str, *args, **kwargs) -> str:
                return await self._complete(prompt)

            def generate(self, prompt: str, *args, **kwargs) -> str:
                return self._blocking(prompt)
    else:
        class GatewayLLM(_GatewayLLM):  # type: ignore[no-redef]
            # STRICT ARITY, on purpose. deepteam probes with
            # `generate(prompt, schema=…)` and needs the TypeError to fall back
            # to `generate(prompt)` + trimAndLoadJson. Accepting **kwargs here
            # is what makes deepteam crash on `res.data`.
            async def a_generate(self, prompt: str) -> str:
                return await self._complete(prompt)

            def generate(self, prompt: str) -> str:
                return self._blocking(prompt)

    return GatewayLLM(model)
