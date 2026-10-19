"""Catalog curation — which templates and pipelines a *human surface* offers
(#1566, ADR-0093 P0).

The gallery is the first screen after login. Before this module it listed every
discoverable template flat, so four integration-test smokes, a pipeline's report
leg, and three DEPRECATED templates sat next to the flagship workflows as
equally-runnable cards. A new user cannot tell `training/train-remote-smoke`
(a dress rehearsal for the SSH dispatch path) from `training/finetune`.

**This is a different axis from :mod:`cairn.core.pack.visibility`.** That module
answers *may pack X call capability Y* (§24.3 encapsulation — an access rule,
enforced at the lookup seam, where a violation is an error). This one answers
*do we put this on the shelf* — a curation rule, applied at render time, where
a "violation" is only clutter. Both are about what you see, so the word
"visibility" is overloaded; they are deliberately separate modules because a
private capability may back a very public template and vice versa.

The tiers::

    public      on the shelf. The default — a template says nothing, it lists.
    internal    real and runnable, but not product surface: smoke tests,
                pipeline legs, campaign machinery. Hidden from the gallery,
                the enable list, and the pipeline picker. Still runnable by
                ref, still returned by the API, still listed (with its tier)
                by the `cairn ask` template inventory.
    deprecated  superseded. Hidden from *offers* (you cannot newly enable it),
                but an existing install keeps working and says so.

Two rules make this cheap to keep honest:

1. **Default public.** Curation is opt-in, so a new product workflow is never
   invisible because its author forgot a field. The failure mode we accept is
   clutter, not a workflow nobody can find.
2. **Smoke templates are internal by construction.** A template already tagged
   ``kind:integration-test`` is internal without saying so twice. That tag
   predates this module (four templates carried it), and it is the tag an
   author reaches for anyway when they add the next dress rehearsal — so the
   next smoke test does not need to remember this file exists.

The API stays COMPLETE: ``/packs`` and the installed-workflow list annotate
every entry with its tier rather than dropping rows. Filtering is the human
surface's job (the console gallery), so an operator debugging "where did my
template go" can always see everything from the API and the CLI.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Mapping

logger = logging.getLogger(__name__)

PUBLIC = "public"
INTERNAL = "internal"
DEPRECATED = "deprecated"

#: Every legal value of the ``visibility`` field.
VISIBILITY_VALUES: frozenset[str] = frozenset({PUBLIC, INTERNAL, DEPRECATED})

#: What a record with no ``visibility`` field means (see rule 1 above).
DEFAULT_VISIBILITY = PUBLIC

#: A template carrying this tag is internal without declaring it (rule 2).
INTEGRATION_TEST_TAG = "kind:integration-test"


def normalize(value: Any) -> str:
    """Coerce a declared ``visibility`` to a known tier.

    Unknown values fall back to :data:`DEFAULT_VISIBILITY` with a warning rather
    than raising: this reads UNTYPED template frontmatter, and a typo in one
    pack's markdown must not take down the whole gallery listing. Typed records
    (:class:`cairn.core.engine.pipeline.PipelineDef`) validate strictly instead
    — there a typo is caught at parse, which is where you want it.
    """
    if value is None:
        return DEFAULT_VISIBILITY
    v = str(value).strip().lower()
    if v in VISIBILITY_VALUES:
        return v
    logger.warning(
        "unknown visibility %r — treating as %r (expected one of %s)",
        value, DEFAULT_VISIBILITY, ", ".join(sorted(VISIBILITY_VALUES)),
    )
    return DEFAULT_VISIBILITY


def visibility_of(meta: Mapping[str, Any] | None) -> str:
    """The curation tier of a template, from its frontmatter ``meta``.

    An explicit ``visibility:`` always wins — including an explicit
    ``visibility: public`` on a template that is also tagged as an integration
    test, which is how you say "yes, I really do mean to ship this one".
    """
    if not meta:
        return DEFAULT_VISIBILITY
    declared = meta.get("visibility")
    if declared is not None:
        return normalize(declared)
    tags = meta.get("tags") or []
    if isinstance(tags, (list, tuple)) and any(
        isinstance(t, str) and t.strip().lower() == INTEGRATION_TEST_TAG for t in tags
    ):
        return INTERNAL
    return DEFAULT_VISIBILITY


def is_listed(visibility: Any) -> bool:
    """Does an ALREADY-INSTALLED record belong on a human-facing shelf?

    Public and deprecated do; internal does not. Deprecated stays listed
    because hiding a workflow somebody has installed and may be running does
    not un-deprecate it — it just removes the surface that could tell them to
    migrate. The console pairs this with a banner naming the successor.
    """
    return normalize(visibility) != INTERNAL


def effective_visibility(declared: Any, *, hidden: bool = False) -> str:
    """The tier a HUMAN SURFACE sees, after an admin's per-workspace override (#2173).

    An admin who hides a pipeline or a flow is not editing the pack — they are
    saying "not on this workspace's shelf". So the override lands on the axis
    that already answers that question rather than beside it (ADR-0106 D4: one
    owner per concept): hidden ⇒ :data:`INTERNAL`, which every consumer of
    :func:`is_listed` and :func:`is_offerable` already understands. The DECLARED
    tier is served unchanged next to this one, so the admin screen can tell
    "hidden by admin" from "internal by pack" — and so un-hiding restores the
    pack's own answer instead of a guess.

    It is CURATION, not governance: the launch door does not consult it, a
    hidden ref still runs by ref from the API and the CLI, and its past runs
    still render. Hiding something that must not run is a policy (ADR-0106 D7),
    written where policies are.
    """
    return INTERNAL if hidden else normalize(declared)


def is_offerable(visibility: Any) -> bool:
    """Can a user newly ENABLE or launch this? Only ``public``.

    Deliberately stricter than :func:`is_listed`: the two differ exactly at
    ``deprecated``, which keeps working where it is already installed but must
    never be offered again as a fresh choice. Used by "Available to enable"
    and the new-pipeline picker.

    Both are functions rather than inlined ``== "public"`` comparisons so that
    a fourth tier (``preview``, say) has one place to decide each question
    instead of N scattered checks across the console, the CLI, and the API.
    """
    return normalize(visibility) == PUBLIC


# ── card copy ────────────────────────────────────────────────────────────────
#
# A template's `description` is written for two audiences at once: the first
# sentence is card copy, the rest is reference. Both the installed-workflow
# list and `/packs` render the front of it, so the trimming lives here — when
# it lived in only one of them, the gallery's "Available to enable" cards
# showed raw frontmatter while the installed cards showed a clean blurb, and
# the same template read differently depending on which shelf it sat on.

#: A parenthetical that OPENS with an issue or ADR ref — "(#992, ADR-0076 D4)",
#: "(ADR-0081 P1c, #1050)". Anchored on the opening token so prose asides
#: ("(no LLM, no GPU)") survive untouched.
_ARCHAEOLOGY = re.compile(r"\s*\((?:#\d+|ADR-\d+)[^)]*\)")


def card_blurb(description: Any, *, limit: int = 160) -> str:
    """The first sentence of ``description``, fit to be read by a human who is
    CHOOSING a workflow.

    Drops the engineering archaeology templates carry inline: an ADR number is
    the right thing to write next to the code and the wrong thing to show
    someone picking from a shelf (#1566 item 3).

    This is a net, not the fix. A blurb that only reads well because of this
    trimming is a description that still needs rewriting — the descriptions
    themselves are the copy.
    """
    flat = " ".join(str(description or "").split())
    flat = _ARCHAEOLOGY.sub("", flat)
    if not flat:
        return ""
    blurb = re.split(r"(?<=[.!?])\s", flat, maxsplit=1)[0]
    if len(blurb) > limit:
        blurb = blurb[: limit - 3].rsplit(" ", 1)[0] + "…"
    return blurb
