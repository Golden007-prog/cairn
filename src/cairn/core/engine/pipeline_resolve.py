"""Resolve a ref to a pipeline (#1126, ADR-0084 D9).

D9 makes the pipeline the primary runnable noun, so ``cairn run <ref>`` must be
able to resolve one. The interesting case is **ambiguity**: a pack may legally
hold both ``flows/check.md`` and ``pipelines/check.yaml``. Guessing would
silently run the wrong thing, so a bare ambiguous ref raises and names the
``pipeline:`` prefix that disambiguates it.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Mapping, Optional

from cairn.core.engine.pipeline import PipelineDef, load_pipeline_def
from cairn.core.pack.registry import all_pipeline_dirs, get_packs
from cairn.core.refs import matches, pick  # ONE ref spelling (#2126)
from cairn.core.schema import default_registry as schema_registry

logger = logging.getLogger("cairn.core.engine.pipeline_resolve")

PIPELINE_PREFIX = "pipeline:"


class AmbiguousRefError(ValueError):
    """A bare ref that names both a pipeline and a flow/template."""


def all_pipeline_yaml_paths() -> list[Path]:
    """Every pipeline FILE across installed packs, parseable or not.

    The denominator :func:`all_pipeline_defs` is measured against (#1742). It
    reads the filesystem and nothing else, so a file that cannot be parsed still
    appears here — which is the whole point.
    """
    return [yml for d in all_pipeline_dirs() for yml in sorted(Path(d).glob("*.yaml"))]


#: ``(key, defs)`` of the last CLEAN discovery (#2340) — a process-local memo of
#: :func:`all_pipeline_defs`, not a store: nothing reads it but that function,
#: and :func:`invalidate_pipeline_discovery` drops it.
_DISCOVERY_MEMO: Optional[tuple[tuple, list[PipelineDef]]] = None

#: The pack dirs a parse reads: pipeline files, and — through ``$inherit``
#: (ADR-0106 D2) — the agentic ``.md`` and data/ml ``.yaml`` flows it adopts a
#: contract from (``flows/`` + the ``templates/`` and ``dataflows/`` aliases).
_DISCOVERY_DIRS = ("pipelines_dir", "flows_dir", "templates_dir", "dataflows_dir")


def invalidate_pipeline_discovery() -> None:
    """Forget the memo; the next :func:`all_pipeline_defs` re-parses everything."""
    global _DISCOVERY_MEMO
    _DISCOVERY_MEMO = None


def _dir_digest(d: str) -> Optional[str]:
    """sha256 over every file's relative path AND bytes under ``d``; ``None`` if absent.

    Content, not mtimes: a same-size rewrite inside one mtime tick (a coarse
    filesystem, a fast publish) must still be a different key."""
    if not os.path.isdir(d):
        return None
    h = hashlib.sha256()
    for root, dirs, files in os.walk(d):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(root, name)
            h.update(os.path.relpath(path, d).encode() + b"\0")
            try:
                with open(path, "rb") as f:
                    h.update(f.read())
            except OSError:
                h.update(b"\0unreadable")
            h.update(b"\0")
    return h.hexdigest()


def _discovery_key() -> tuple:
    """Everything a parse reads: the registered packs (an install or a
    re-registration replaces them), the schema registry (an agentic leg's input
    schema), the pipeline dirs exactly as the parse lists them, and the content
    of each of those plus every pack dir in :data:`_DISCOVERY_DIRS`.
    Compared with ``==``, which short-circuits on identity — the warm path."""
    packs = tuple(get_packs())
    pipeline_dirs = tuple(all_pipeline_dirs())  # the SAME call all_pipeline_yaml_paths makes
    dirs = dict.fromkeys([*pipeline_dirs, *(
        d for p in packs for a in _DISCOVERY_DIRS if (d := getattr(p, a, None)))])
    return (packs, tuple(schema_registry.all().items()), pipeline_dirs,
            tuple((d, _dir_digest(d)) for d in dirs))


def all_pipeline_defs() -> list[PipelineDef]:
    """Every parseable pipeline across installed packs.

    A malformed file is skipped with a warning rather than raising: one broken
    neighbour must not make a valid ref unresolvable. That leniency is right for
    RUNTIME DISCOVERY and wrong as the basis for a completeness check — see
    :func:`load_all_pipeline_defs_strict`, and #1742 for the gate it silently
    hollowed out.

    **Memoised on exactly what it reads (#2340).** A parse resolves every
    ``$inherit`` leg's contract (seconds per call), so a clean result is kept
    under :func:`_discovery_key` and served as deep copies — a caller that
    mutates its defs cannot reach the next caller. A result that SKIPPED a file
    is never kept: the failure may come from outside the key (a contract
    reader's environment), so the next call retries it and warns again.
    """
    global _DISCOVERY_MEMO
    key = _discovery_key()  # BEFORE the parse: a change mid-parse misses next call
    memo = _DISCOVERY_MEMO
    if memo is not None and memo[0] == key:
        return [d.model_copy(deep=True) for d in memo[1]]
    out: list[PipelineDef] = []
    skipped = False
    for yml in all_pipeline_yaml_paths():
        try:
            out.append(load_pipeline_def(yml))
        except Exception:  # noqa: BLE001 — a bad file is a lint problem
            logger.warning("skipping unparseable pipeline %s", yml)
            skipped = True
    _DISCOVERY_MEMO = None if skipped else (key, [d.model_copy(deep=True) for d in out])
    return out


class UnparseablePipelineError(ValueError):
    """A shipped pipeline file could not be parsed at all."""


def load_all_pipeline_defs_strict() -> list[PipelineDef]:
    """Every shipped pipeline, RAISING on the first file that cannot parse.

    Exists because :func:`all_pipeline_defs` skips what it cannot read, so a
    completeness check built on it gets quieter the worse the tree gets — the
    broken files are precisely the ones it stops seeing. A pipeline that fails
    to parse is not "unrunnable" to such a check, it is *invisible* (#1742,
    found when a pipeline violating ADR-0084 D5 passed the runnable gate while
    being unlaunchable).

    Same family as CLAUDE.md rule 7: a gate that cannot fail is not a gate.
    """
    out: list[PipelineDef] = []
    for yml in all_pipeline_yaml_paths():
        try:
            out.append(load_pipeline_def(yml))
        except Exception as e:  # noqa: BLE001 — re-raised with the file named
            raise UnparseablePipelineError(
                f"{yml} could not be parsed: {type(e).__name__}: {e}"
            ) from e
    return out


def _template_exists(ref: str) -> bool:
    """Does a flow/template of this ref exist? (Used only for the ambiguity check.)"""
    try:
        from cairn.core.engine.skill_engine import resolve_template_path  # noqa: PLC0415 - cycle

        return bool(resolve_template_path(ref))
    except Exception:  # noqa: BLE001 — "not found" and "broken" both mean no clash
        return False


def resolve_pipeline_ref(ref: str) -> Optional[PipelineDef]:
    """Resolve ``<pack>/<name>`` or ``pipeline:<pack>/<name>`` to a definition.

    Returns ``None`` when no pipeline matches (the caller then tries a
    flow/template). Raises :class:`AmbiguousRefError` when a BARE ref matches
    both a pipeline and a flow/template — explicit beats a silent wrong run.

    **Both spellings of a pack name resolve here (#2126).** A pipeline is
    stamped with its pack's DIRECTORY name (``dataset_readiness``, from
    :func:`~cairn.core.engine.pipeline.load_pipeline_def`) while a flow — and
    therefore the ref a reader copies out of ``GET /flows``, the console, or
    ``cairn ls`` — carries the ``pack.toml`` name (``dataset-readiness``). Until
    this, ``dataset_readiness/check`` ran and ``dataset-readiness/check`` was
    refused as a bare flow ref: the same string to a human, two addresses to the
    resolver, and the refusal named a wrapper that already existed.

    The match is by CANONICAL KEY, derived here rather than stamped at install
    (see :mod:`cairn.core.refs`) — a ref already written down in a preset key, a
    trigger or a launched pipeline's ``bundle_ref`` has to meet the same key as
    one typed today, and a stamp only canonicalises what is installed after it.
    An exact spelling still wins outright; a fold that matches TWO pipelines is
    refused, because "the first one the filesystem listed" is how a resolver
    answers confidently with the wrong pipeline.
    """
    raw = (ref or "").strip()
    explicit = raw.startswith(PIPELINE_PREFIX)
    target = raw[len(PIPELINE_PREFIX):] if explicit else raw
    if not target:
        return None

    by_ref = {f"{p.pack}/{p.name}": p for p in all_pipeline_defs()}
    chosen, tied = pick(target, by_ref)
    if len(tied) > 1:
        raise AmbiguousRefError(
            f"{target!r} names {len(tied)} installed pipelines — {tied!r}. "
            f"Their pack names differ only by `_` vs `-`, so they share one "
            f"address (#2126) and cairn will not guess: rename one of the packs."
        )
    if not chosen:
        return None
    match = by_ref[chosen]

    if not explicit and _template_exists(target):
        raise AmbiguousRefError(
            f"{target!r} names BOTH a pipeline and a flow/template. Use "
            f"`{PIPELINE_PREFIX}{target}` to run the pipeline, or the flow door "
            f"to run the flow — cairn will not guess which you meant."
        )
    return match


class PipelineNotRunnableError(NotImplementedError):
    """A pipeline whose legs cannot all resolve against what is installed.

    Subclasses :class:`NotImplementedError` because that is the contract
    ``assert_runnable`` has always had with its callers — the console's
    pipeline listing catches it to mark a pipeline "blocked"
    (``routers/pipelines.py``) and the launch path lets it propagate. Keeping
    the type means making the check real changes no call site.
    """


def _flow_def_exists(ref: str) -> bool:
    """Is there a data/ML flow def for ``<pack>/<name>``?

    Folds the underscore/hyphen pack-name split through the ONE normaliser the
    executors resolve with (:mod:`cairn.core.refs`, #2126) — this was its own
    three-spellings-in-order loop, the third copy of a rule that had already
    drifted twice."""
    from cairn.core.dataflow.defs import load_flow_defs  # noqa: PLC0415 - cycle

    return bool(matches(ref, load_flow_defs()))


def _operator_exists(name: str) -> bool:
    from cairn.core.operator.loader import ensure_loaded  # noqa: PLC0415 - cycle
    from cairn.core.operator.registry import default_registry  # noqa: PLC0415 - cycle

    ensure_loaded(strict=False)
    return name in default_registry.all()


def assert_runnable(d: PipelineDef, *, flow_defs: Optional[Mapping[str, Any]] = None) -> None:
    """Resolve every executable leg BEFORE anything admits or spends (#1538).

    For years this was a documented no-op ("today it accepts every valid
    def"), so a typo'd leg ref admitted, ran — and paid for — every earlier
    leg, then died mid-run with a resolution error. The agentic compile path
    already validated its refs at compile time (``compiler.py``); the pipeline
    tier now gets the same discipline at its admission seam. Both existing
    callers gate on this: ``launch_pipeline`` (before inputs are even
    checked) and the console's pipeline listing (renders the failure as the
    pipeline's "blocked" reason).

    Every miss is collected before raising, so a pipeline with three bad refs
    reports three fixes, not one per attempt.

    ``flow_defs`` (#2340) lets a caller checking MANY pipelines pass one
    :func:`~cairn.core.dataflow.defs.load_flow_defs` snapshot instead of a full
    flow-dir scan per flow leg; ``None`` (every launch) scans per leg as before.
    """
    problems: list[str] = []
    # None → the per-leg scan every launch has always made, through the same name
    flow_exists = (_flow_def_exists if flow_defs is None
                   else lambda ref: bool(matches(ref, flow_defs)))

    def _walk(steps):
        for s in steps:
            yield s
            yield from _walk(getattr(s, "steps", None) or [])

    for leg in _walk(d.steps):
        kind = leg.leg_kind
        ref = leg.ref
        if kind == "template":
            if not _template_exists(ref):
                # #1543 — the symmetric half of the `flow:` hint below. Both
                # keywords name the SAME `<pack>/<name>` namespace, so the only
                # thing distinguishing them is the runtime; a leg that picked
                # the wrong one must be told which keyword its file wants.
                hint = (
                    f" (a flow '{ref}' DOES exist — if the leg means the "
                    f"engine's flows/{ref.rpartition('/')[2]}.yaml, the keyword "
                    f"must be `flow:`)"
                    if flow_exists(ref) else ""
                )
                problems.append(
                    f"step '{leg.name}': template '{ref}' does not resolve — "
                    f"is the pack installed, and does flows/"
                    f"{ref.rpartition('/')[2]}.md exist?{hint}"
                )
        elif kind == "flow":
            if not flow_exists(ref):
                hint = (
                    f" (a template '{ref}' DOES exist — if the leg means the "
                    f"agentic .md flow, the keyword must be `template:`)"
                    if _template_exists(ref) else ""
                )
                # #2069 — a def that FAILED to load is not a def nobody wrote.
                # Ask why before blaming the pack: one missing kernel entry
                # point un-runs every pipeline that uses the flow, and the
                # generic message names neither the stage nor the pack.
                from cairn.core.dataflow.defs import (  # noqa: PLC0415 — cycle: defs imports pack registry
                    unresolvable_flow_defs,
                )

                why = unresolvable_flow_defs().get(ref, "")
                problems.append(
                    f"step '{leg.name}': flow '{ref}' was found but REFUSED to "
                    f"load — {why}" if why else
                    f"step '{leg.name}': flow '{ref}' does not resolve — the "
                    f"engine executor only finds flows/*.yaml in installed "
                    f"packs{hint}"
                )
        elif kind == "task":
            if not _operator_exists(ref):
                problems.append(
                    f"step '{leg.name}': operator '{ref}' is not registered — "
                    f"is its operator pack installed?"
                )
    if problems:
        raise PipelineNotRunnableError(
            f"pipeline {d.pack}/{d.name} cannot launch:\n  - "
            + "\n  - ".join(problems)
        )


__all__ = [
    "PIPELINE_PREFIX", "AmbiguousRefError", "PipelineNotRunnableError",
    "all_pipeline_defs", "assert_runnable", "invalidate_pipeline_discovery",
    "resolve_pipeline_ref",
]
