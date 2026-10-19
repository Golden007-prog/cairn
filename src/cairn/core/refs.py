"""One canonical spelling for a flow / pipeline ref (#2126).

One ref has three spellings, and joining on the raw string produces a silently
EMPTY answer that looks plausible:

* a pack REGISTERS under its ``pack.toml`` name — ``dataset-readiness`` — which
  is how :func:`~cairn.core.dataflow.defs.load_flow_defs` keys its defs and how
  :func:`~cairn.core.pack.registry.get_pack` is addressed;
* its DIRECTORY is ``dataset_readiness`` (a Python package name). Until #2527 the
  directory is what :func:`~cairn.core.engine.pipeline.load_pipeline_def` stamped on
  a source checkout's pipelines, so rows recorded before ``cairn pipeline
  respell-packs`` still carry it; every install now stamps the ``pack.toml`` name;
* a leg ref may keep its ADR-0084 D0 kind prefix (``flow:``, ``template:``,
  ``task:``, ``gate:``, ``pipeline:``).

That split bit three times in three days, each time as an empty list rather than
an error: the flow page's "Used by" and ``cairn ls``'s runnable surface (#2115),
``GET /flows``'s ``used_by`` (#2110), and pipeline-vs-flow resolution once
#2134 S2 landed the wrappers (``dataset-readiness/check`` was refused while
``dataset_readiness/check`` ran). Each was fixed where it was found, so by the
third the normaliser existed twice in two languages, agreeing by hand.

**This module is the one implementation.** It is deliberately pure — no imports
below the standard library, no registry read, no I/O — so the routers, the
resolvers, the CLI and the pack layer can all import it without any of them
pulling the engine onto the other's import path. The canonical form is
``<hyphen-lowered pack>/<hyphen-lowered name>`` with no prefix: hyphens because
that is the ``pack.toml`` spelling a pack chose for itself and the spelling the
API already publishes.

**A key is DERIVED, never stored.** Stamping a canonical spelling at install
would leave every ref written down before the stamp — a saved preset key, a
trigger's ``ref``, a launched pipeline's ``bundle_ref``, a pipeline file a human
typed — on the old side of the split, which moves the bug rather than closing
it. Deriving means a ref a human types, a ref a leg carries and a ref the
registry stores all meet at the same key with no migration.

**A tie is an error, not a coin flip.** :func:`matches` returns EVERY candidate
that folds onto one key, exact spelling first, so a caller that finds two must
say so. Returning "the first one" is how a resolver answers confidently with the
wrong pipeline; returning nothing is the failure mode this whole ticket is
about.
"""
from __future__ import annotations

from typing import Iterable

#: Every kind prefix a leg ref may carry (ADR-0084 D0), stripped before two refs
#: are compared. ``pipeline:`` is here too: a launched pipeline's ``bundle_ref``
#: carries it and the Runs list joins on it.
LEG_PREFIXES = ("flow:", "template:", "task:", "gate:", "pipeline:")


def bare_ref(ref: str) -> str:
    """The ref with its kind prefix stripped — ``flow:training/sft-train`` →
    ``training/sft-train``.

    The prefix is the leg KIND, not part of the identity: the same flow is
    ``flow:`` in a data leg's file and bare in a ``used_by`` row.
    """
    raw = str(ref or "").strip()
    for p in LEG_PREFIXES:
        if raw.startswith(p):
            return raw[len(p):].strip()
    return raw


def pack_key(name: str) -> str:
    """One spelling for a PACK across its directory and its ``pack.toml`` name.

    ``dataset_readiness`` (the directory, and every pipeline's ``pack``) and
    ``dataset-readiness`` (the registered name, and every flow def's key) are
    the same pack. ``training`` carries no separator, which is exactly why the
    split hid for so long — every single-word pack agrees with itself.
    """
    return str(name or "").strip().lower().replace("_", "-")


def spellings(pack: str) -> tuple[str, ...]:
    """Every spelling a pack's ids and refs were recorded under: its ``pack.toml`` name
    and, when that name has a hyphen, the Python-package spelling of its directory
    (``dataset_readiness``) that a source checkout recorded before #2527. Sweeps of rows a
    pipeline owns read both; nothing new is written in the second."""
    name = str(pack or "").strip()
    alt = name.replace("-", "_")
    return (name,) if alt == name else (name, alt)


def ref_key(ref: str) -> str:
    """THE canonical key for a flow or pipeline ref.

    ``flow:dataset_readiness/check`` ≡ ``dataset-readiness/check`` ≡
    ``dataset_readiness/check`` → ``dataset-readiness/check``.

    Total and pure: an unknown ref folds to a key that simply matches nothing,
    so a caller gets "no match" rather than a crash. It is a KEY, not a display
    ref — show the reader the spelling they wrote, join on this.
    """
    return pack_key(bare_ref(ref))


def same_ref(a: str, b: str) -> bool:
    """Do two refs name the same flow, whatever spelling each carries?

    Empty is never equal to empty: two rows that both failed to carry a ref are
    not "the same flow", and treating them as such is how an empty join looks
    like a hit.
    """
    key = ref_key(a)
    return bool(key) and key == ref_key(b)


def matches(want: str, candidates: Iterable[str]) -> list[str]:
    """Every candidate ref that folds onto ``want``'s key — EXACT spelling first.

    The order is the precedence a resolver needs. An exact spelling is
    unambiguous by construction, so it wins outright even when a differently
    spelled sibling folds onto the same key; only when nothing matches exactly
    does the fold decide, and then a result of length > 1 is a genuine tie the
    caller must refuse rather than silently resolve.

    Returns ``[]`` for an unknown ref (and for an empty one), which is a
    caller's cue to say "not installed" — never to guess.
    """
    key = ref_key(want)
    if not key:
        return []
    seen: list[str] = []
    for c in candidates:
        if c not in seen and ref_key(c) == key:
            seen.append(c)
    exact = bare_ref(want)
    return sorted(seen, key=lambda c: (bare_ref(c) != exact, c))


def pick(want: str, candidates: Iterable[str]) -> tuple[str, list[str]]:
    """``(the candidate ``want`` names, every candidate tied with it)``.

    The two-value shape exists so a resolver cannot accidentally skip the tie
    check: you get the answer and the evidence in one call, and a ``len(tied) >
    1`` you ignore is visible in the diff.

    ``("", [])`` when nothing matches. ``tied`` has length > 1 only when nothing
    matched EXACTLY and two differently spelled candidates fold onto one key —
    a packaging bug (two packs whose names differ only by ``_`` vs ``-``) that
    every caller must refuse loudly rather than resolve by list order.
    """
    hits = matches(want, candidates)
    if not hits:
        return "", []
    if bare_ref(hits[0]) == bare_ref(want):
        return hits[0], [hits[0]]     # exact — unambiguous by construction
    return hits[0], hits


__all__ = ["LEG_PREFIXES", "bare_ref", "matches", "pack_key", "pick", "ref_key",
           "same_ref", "spellings"]
