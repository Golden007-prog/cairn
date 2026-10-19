"""PII / secret redaction — the core interface, chokepoint helper, and resolver.

Core owns the **interface** (`Redactor`), the **chokepoint** (the LLM gateway
calls `redact_messages` before anything leaves to the vendor), and the
**resolution** (which provider is bound). It owns **no detection logic** — even
the zero-dep regex default ships as a builtin service-provider operator
(`cairn-operator-redact-regex`). See ADR-0028.

**Optional to turn ON, not optional to ESCAPE (ADR-0028):**
  - No redactor bound  → `get_redactor()` returns `NoOpRedactor` (pass-through,
    zero cost). This is the default; `local-dev.toml` leaves `redact.text`
    unbound. A pack that touches sensitive data declares it needs `redact`.
  - A redactor bound    → the gateway always applies it; a pack cannot bypass
    a redactor the deployment activated. The data owner (deployment) controls
    redaction via the `redact.text` profile binding.

Resolution reuses the existing machinery: the bound operator name comes from
the active profile's `[profile.bindings]` (`redact.text`), and the provider
class comes from the operator registry. Cached — call `reset_redactor()` after
changing the binding (tests do).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger("cairn.obs.redact")

_BINDING_CAPABILITY = "redact.text"


@dataclass(frozen=True)
class RedactSpan:
    """One redacted region, in ORIGINAL-text coordinates (audit metadata)."""
    kind: str          # "email", "credit_card", "aws_key", ...
    start: int
    end: int


@dataclass
class RedactionResult:
    text: str
    spans: list[RedactSpan] = field(default_factory=list)

    @property
    def redacted(self) -> bool:
        return bool(self.spans)


@runtime_checkable
class Redactor(Protocol):
    """The core redaction contract. Implementations ship as service-provider
    operators (ADR-0028); core never holds detection logic."""
    async def redact_text(self, text: str) -> RedactionResult: ...


class NoOpRedactor:
    """Inert default — returned when no redactor is bound. Pass-through."""
    async def redact_text(self, text: str) -> RedactionResult:
        return RedactionResult(text=text, spans=[])


_NOOP: Redactor = NoOpRedactor()
_CACHE: Optional[Redactor] = None


def _bound_provider_name() -> Optional[str]:
    """The operator name bound to `redact.text` in the active profile, if any."""
    try:
        from cairn.core.pack.profile import load_profile_from_env
        return load_profile_from_env().bindings.get(_BINDING_CAPABILITY)
    except Exception as e:  # noqa: BLE001 — no profile / no binding = unbound
        logger.debug("no redact binding resolved: %s", e)
        return None


def get_redactor() -> Redactor:
    """The bound `Redactor`, or `NoOpRedactor` when unbound (ADR-0028). Cached.

    Fail-open: any resolution error degrades to the no-op so a misconfigured
    binding never breaks LLM calls (it's logged loud)."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    name = _bound_provider_name()
    if not name:
        _CACHE = _NOOP
        return _CACHE
    try:
        from cairn.core.operator import default_registry
        op_cls = default_registry.get(name)
        inst = op_cls() if isinstance(op_cls, type) else op_cls
        if not hasattr(inst, "redact_text"):
            logger.warning(
                "redact binding %r resolves to a non-Redactor operator — using no-op",
                name,
            )
            _CACHE = _NOOP
        else:
            logger.info("redactor bound: %s", name)
            _CACHE = inst  # type: ignore[assignment]
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.warning("redact binding %r unresolved (%s) — using no-op", name, e)
        _CACHE = _NOOP
    return _CACHE


def redactor_active() -> bool:
    """True when a real redactor is bound (not the inert no-op).

    The trace content-capture gate (#247) fails closed unless this is True — it
    won't export prompt/completion content in ``redacted`` mode if nothing would
    actually scrub it."""
    return get_redactor() is not _NOOP


def reset_redactor() -> None:
    """Drop the cached redactor (tests, or after a binding change)."""
    global _CACHE
    _CACHE = None


async def redact_text(text: str) -> RedactionResult:
    """Redact a single string via the bound redactor."""
    return await get_redactor().redact_text(text)


async def redact_messages(messages: list[dict]) -> list[dict]:
    """Chokepoint: redact the text content of chat messages before they leave
    to the LLM. No-op (returns the same list) when no redactor is bound."""
    r = get_redactor()
    if r is _NOOP:
        return messages
    out: list[dict] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str) and content:
            res = await r.redact_text(content)
            out.append({**m, "content": res.text} if res.redacted else m)
        else:
            out.append(m)
    return out
