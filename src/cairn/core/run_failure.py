"""What to tell a human when a run fails (#1594, ADR-0093 wave 2).

Every slice of the console journey specifies the happy path. Nothing specified
what a stranger sees when their first run fails — which is the single most
likely outcome for a stranger. Today the engine captures a cause
(``Run.error``, written by the dispatcher as ``str(exc)``) and no surface
renders it, so a failed run is a red badge and nothing else.

``str(exc)`` is not the answer either. A real one, verbatim::

    remote_job job 'builtin-68039' failed: FileNotFoundError: Couldn't find any
    data file at /nonexistent/definitely-not-here.jsonl.

An internal job id and a Python exception class, for someone who wants to know
what to do next. This module turns that into the shape the console already
speaks — :class:`cairn.core.ui_contract.Remediation` (``kind`` / ``what`` /
``fix``) — so a failure reads like the readiness rail a user already met before
the run, rather than introducing a second vocabulary for the same idea.

**It lives server-side on purpose.** The console is the first consumer, but
``cairn run`` should not have to reimplement the same table to print the same
advice, and a classification worth trusting is one that has exactly one
definition. The raw text is always preserved on ``detail``: the point is to
LEAD with something actionable, not to hide what happened.

Two rules keep it honest:

1. **Never guess past the evidence.** A pattern matches or it does not; the
   fallback says "the run failed" and shows the raw error rather than inventing
   a plausible cause. A confident wrong diagnosis is worse than none — it
   sends someone off to fix a thing that was not broken.
2. **A sandbox run never asks for credentials.** The bundled sample is
   promised to need no setup (it runs with operators mocked, ``?sandbox=true``,
   #826), so if IT fails, the failure is ours. Telling that user to add an API
   key contradicts the promise that got them to click, and the key would not
   have helped.
"""
from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field

from cairn.core.ui_contract import Remediation


class FailureExplanation(BaseModel):
    """A failed run, in terms a human can act on.

    ``cause``/``fix`` mirror :class:`Remediation`'s ``what``/``fix`` so the
    console renders a failure with the component it already uses for
    readiness. ``detail`` is the raw engine error, always kept.
    """

    #: Coarse family, for styling + telemetry: "provider" | "quota" | "input"
    #: | "network" | "missing_operator" | "gate" | "cancelled" | "unknown".
    kind: str = "unknown"
    #: One plain sentence: what went wrong, no exception classes, no job ids.
    cause: str = ""
    #: EXACTLY ONE next action. Empty when we genuinely do not know one —
    #: an empty fix is honest; a made-up one is not.
    fix: str = ""
    #: Where that action lives, as a console route the UI can link ("" = none).
    fix_href: str = ""
    #: The raw ``Run.error``. Never dropped — shown behind "technical detail".
    detail: str = ""
    #: True when re-running the same inputs could plausibly succeed (a blip, a
    #: rate limit), so the UI can offer retry as the primary action instead of
    #: sending someone to fix something that is not broken.
    retryable: bool = False

    def as_remediation(self) -> Remediation:
        """The same thing in the shape the readiness rail already renders."""
        return Remediation(kind=self.kind, what=self.cause, fix=self.fix)


#: (regex, kind, cause, fix, route, retryable). Ordered — first match wins, so
#: specific patterns precede general ones. Patterns are matched against the
#: engine's error text case-insensitively.
_RULES: tuple[tuple[str, str, str, str, str, bool], ...] = (
    # ── provider / credentials ──────────────────────────────────────────────
    (r"\b(api[_ -]?key|no such key|missing key|OPENAI_API_KEY|ANTHROPIC_API_KEY)\b",
     "provider",
     "This workflow calls a model, and no provider credential is configured.",
     "Connect a model provider, then run it again.",
     "/connectors", False),
    (r"\b(401|unauthorized|invalid[_ ]api[_ ]key|authentication[_ ]error|forbidden|403)\b",
     "provider",
     "The model provider rejected the credential this deployment is using.",
     "Check the provider credential on the Connectors page.",
     "/connectors", False),
    # ── quota / rate limit ──────────────────────────────────────────────────
    (r"\b(429|rate[_ ]?limit|quota|insufficient[_ ]quota|too many requests)\b",
     "quota",
     "The model provider refused the request — the account is rate-limited or out of quota.",
     "Wait and retry, or use a provider with remaining quota.",
     "", True),
    # ── input data ──────────────────────────────────────────────────────────
    (r"(FileNotFoundError|couldn'?t find any data file|no such file or directory)",
     "input",
     "The input file could not be found where the run looked for it.",
     "Check the path is reachable from the engine, then run it again.",
     "", False),
    (r"\b(ValidationError|is a required property|invalid input|schema)\b",
     "input",
     "The inputs did not match what this workflow expects.",
     "Correct the inputs on the run form and submit again.",
     "", False),
    # ── network / gateway ───────────────────────────────────────────────────
    (r"\b(connection (refused|error|reset)|timed? ?out|timeout|unreachable|"
     r"getaddrinfo|name or service not known|502|503|504)\b",
     "network",
     "The engine could not reach a service the run depends on.",
     "Check the service is up, then retry — nothing was charged for the failed attempt.",
     "", True),
    # ── deployment shape ────────────────────────────────────────────────────
    (r"(operator .* (is )?not (installed|registered)|no operator provid|unprovisioned|not provisioned)",
     "missing_operator",
     "This workflow needs a capability this deployment does not have installed.",
     "Check the workflow's readiness panel for the missing operator.",
     "", False),
    # ── governance ──────────────────────────────────────────────────────────
    # #1571 — the series gate refuses on the ESTIMATE, at admission, before any
    # spend: "this run's estimate $80.00 would exceed the $100.00 cap ($0.00
    # spent)". So this must not say money was spent — nothing was. And the fix
    # has a place to happen, so it links there rather than describing it.
    (r"\b(budget|cost cap|over budget|exceeds .* cap)\b",
     "gate",
     "The run was refused by its budget cap before it spent anything — its "
     "estimated cost would have taken the series over.",
     "Raise the series cap, or reduce the work this run was asked to do.",
     "/series", False),
    (r"\b(cancelled|canceled|aborted by)\b",
     "cancelled",
     "The run was cancelled before it finished.",
     "Start it again if that was not intended.",
     "", True),
)

#: Compiled once — this runs per failed row in a listing.
_COMPILED = tuple((re.compile(p, re.I), *rest) for p, *rest in _RULES)

#: What a sandbox failure says instead of anything credential-shaped (rule 2).
_SANDBOX_OVERRIDE_KINDS = frozenset({"provider", "quota"})


def explain_failure(
    error: Optional[str],
    *,
    sandbox: bool = False,
    status: str = "failed",
) -> Optional[FailureExplanation]:
    """Explain a failed run, or ``None`` if there is nothing to explain.

    ``sandbox`` marks a Playground run (operators mocked, no credentials in
    play). ``status`` lets a cancelled/expired run say so rather than being
    described as a failure.
    """
    if status not in {"failed", "cancelled", "expired"}:
        return None
    raw = (error or "").strip()

    if status == "expired":
        return FailureExplanation(
            kind="gate", detail=raw,
            cause="Nobody approved this run in time, so it was closed out.",
            fix="Run it again — the approval request expired, not the work.",
            retryable=True,
        )
    if status == "cancelled" and not raw:
        return FailureExplanation(
            kind="cancelled", detail=raw,
            cause="The run was cancelled before it finished.",
            fix="Start it again if that was not intended.",
            retryable=True,
        )
    if not raw:
        # Failed with no recorded cause. Say exactly that — the alternative is
        # inventing one, and "unknown" is a real, reportable state.
        return FailureExplanation(
            kind="unknown",
            cause="The run failed without recording a reason.",
            fix="",
            retryable=True,
        )

    for pattern, kind, cause, fix, href, retryable in _COMPILED:
        if not pattern.search(raw):
            continue
        if sandbox and kind in _SANDBOX_OVERRIDE_KINDS:
            # Rule 2 — the sample was promised to need no setup, and a
            # credential would not have helped: the operators were mocked.
            return FailureExplanation(
                kind="unknown", detail=raw, retryable=True,
                cause="The bundled sample failed inside cairn — this is not "
                      "something you need to configure.",
                fix="Retry the sample; if it keeps failing, this is a bug worth reporting.",
            )
        return FailureExplanation(kind=kind, cause=cause, fix=fix,
                                  fix_href=href, detail=raw, retryable=retryable)

    # Rule 1 — no pattern matched. Do not guess.
    if sandbox:
        return FailureExplanation(
            kind="unknown", detail=raw, retryable=True,
            cause="The bundled sample failed inside cairn — this is not "
                  "something you need to configure.",
            fix="Retry the sample; if it keeps failing, this is a bug worth reporting.",
        )
    return FailureExplanation(
        kind="unknown", detail=raw, retryable=True,
        cause="The run failed. The engine's message is below.",
        fix="",
    )


class RunFailure(BaseModel):
    """Envelope attached to a run payload as ``failure`` (absent when the run
    did not fail), so every surface reads one field."""

    explanation: FailureExplanation
    #: Inputs the run was launched with, so a retry can reuse them verbatim
    #: rather than sending the user back to the gallery to start over (#1594
    #: item 2). Empty when the run recorded none.
    retry_inputs: dict = Field(default_factory=dict)
