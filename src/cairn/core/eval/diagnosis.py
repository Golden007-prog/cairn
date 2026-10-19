"""Diagnosis write path — the integration seam between the taxonomy (#474) and
the eval ledger (#244), ADR-0059 §D6.

``cairn.core.eval.taxonomy`` is a pure, storage-free function
(``classify_failure``) that returns its OWN ``FailureLabel`` dataclass.
``cairn.core.storage.eval_ledger`` is a persistence-only store with a
DIFFERENT ``FailureLabel`` (a pydantic model shaped for the ``failure_labels``
row). Per the module docstrings on both sides, neither may import the other —
this module is deliberately the one place allowed to depend on both, so it
adapts one shape to the other and drives the write.

This is best-effort by construction (ADR-0059 D6 "own the verdict" doesn't
mean "own the availability of the diagnosis pipeline"): a taxonomy bug or a
ledger outage must never take down a run. Callers (the dispatcher's
completion hook) get an empty list back on any failure here, never an
exception.
"""

from __future__ import annotations

import logging
from typing import Any

from cairn.core.eval.taxonomy import FailureLabel as TaxonomyFailureLabel
from cairn.core.eval.taxonomy import classify_failure
from cairn.core.storage import get_storage
from cairn.core.storage.eval_ledger import FailureLabel as StorageFailureLabel

_log = logging.getLogger("cairn.eval.diagnosis")


def to_storage_label(label: TaxonomyFailureLabel, run_id: str) -> StorageFailureLabel:
    """Adapt a ``taxonomy.FailureLabel`` (pure dataclass) into the eval
    ledger's ``FailureLabel`` (pydantic row shape).

    ``failure_type`` goes from the ``FailureType`` enum member to its
    ``.value`` string (the ledger stores plain strings, ADR-0059 §D3).
    ``run_id`` is set here for clarity/testability, but
    :meth:`~cairn.core.storage.eval_ledger.EvalLedgerStore.add_failure_labels`
    overwrites it anyway to match its own ``run_id`` argument.
    """
    return StorageFailureLabel(
        run_id=run_id,
        failure_type=label.failure_type.value,
        classifier=label.classifier,
        evidence=dict(label.evidence),
    )


async def label_run_failures(
    run: dict[str, Any], events: list[dict[str, Any]], ledger: Any = None
) -> list[TaxonomyFailureLabel]:
    """Classify ``run``'s failure(s) and persist the labels to the eval ledger.

    ``ledger`` is the eval-ledger store to write to. Pass the store that OWNS
    the run — e.g. the platform's ``scoring`` run hook passes its own ledger with
    the dispatcher's run store — so labels are co-located with the run
    regardless of tier (for the in-memory tier the process singleton and a
    dispatcher's own backend are DISTINCT stores; for env-driven SQLite/Postgres
    they share the DB). Defaults to ``get_storage().eval_ledger`` for callers
    that already operate in the process-wide store context (the HTTP endpoint).

    Best-effort: any exception (a taxonomy bug, a store outage, a malformed
    run/event shape) is caught, logged as a warning, and swallowed — this
    must never break a run's completion. A clean run classifies to ``[]`` and
    persists nothing (:meth:`add_failure_labels` is still called with an
    empty list, which is a no-op for every backend).

    Returns the taxonomy-shaped labels that were (attempted to be) persisted,
    so a caller/test can inspect what was classified without a second
    ledger round-trip.
    """
    try:
        labels = classify_failure(run, events)
        adapted = [to_storage_label(label, run.get("id", "")) for label in labels]
        run_id = run.get("id", "")
        store = ledger if ledger is not None else get_storage().eval_ledger
        await store.add_failure_labels(run_id, adapted)
        return labels
    except Exception:  # noqa: BLE001 — best-effort diagnosis, never break the run
        _log.warning("label_run_failures: failed to classify/persist for run %s",
                     run.get("id", "<unknown>"), exc_info=True)
        return []


__all__ = ["to_storage_label", "label_run_failures"]
