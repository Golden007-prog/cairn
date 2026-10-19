"""Surviving a bad row, and refusing to write one (#1601).

A single run whose ``results`` held a dict instead of a list made ``GET /runs``
return 500 — for EVERY run. Thirty-eight healthy rows were unreachable because
of one bad one. A corrupt row is a data problem; an unusable runs list is an
outage, and the console rendered it as "No runs yet", which is how a user who
had just completed a run got told they had never run anything.

Two halves, because either alone leaves the hole open:

**Read side** — :func:`hydrate_all` skips a row it cannot rebuild instead of
letting one exception take down the response. The listing stays useful and says
how many rows it could not read, rather than pretending they do not exist.

**Write side** — :func:`validated_update` closes the door that let the bad row
in. ``Run.results`` is typed ``list[RunResult]``, so a dict cannot survive
validation — but the stores update through
``current.model_copy(update=fields)``, and **model_copy does not validate**.
The wrong shape was written, serialized, and only raised on the way back out,
in a different process, with nothing left to say who wrote it. Validating the
changed fields at write time turns a silent corruption into a traceback at the
callsite that caused it.

Only the fields being CHANGED are validated. Re-validating the whole run on
every update would put a full parse of the results blob on the heartbeat path,
which is the hottest write there is, to check fields nobody touched.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any, Callable, Iterable, NamedTuple, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Rows the most recent listing in THIS task could not rebuild.
#: A ContextVar rather than instance state: a store is shared across concurrent
#: requests, so an attribute would report another request's count. Request
#: scope is task scope, which is exactly what a ContextVar gives.
#:
#: ⚠ It must be set by the ASYNC caller, never inside the worker. The sqlite
#: tier hydrates under ``asyncio.to_thread``, which hands the worker a COPY of
#: the context — a ``set`` in there is discarded on the way out. Setting it
#: inside :func:`hydrate_all` therefore worked on Postgres (called inline) and
#: silently did nothing on sqlite, which is worse than not working at all.
#: Hence: :func:`hydrate_all` REPORTS the count, :func:`record_skipped` stores
#: it, and each store calls the latter from its async ``list``.
_skipped: ContextVar[int] = ContextVar("cairn_hydration_skipped", default=0)


def skipped_rows() -> int:
    """How many rows the last listing in this task could not read (0 normally)."""
    return _skipped.get()


def record_skipped(n: int) -> None:
    """Publish a listing's skip count. Call from the ASYNC caller — see above."""
    _skipped.set(n)


class Hydrated(NamedTuple):
    """Rows rebuilt, and how many were too corrupt to rebuild."""

    items: list
    skipped: int


def hydrate_all(
    rows: Iterable[Any],
    hydrate_one: Callable[[Any], T],
    *,
    source: str = "runs",
    row_id: Callable[[Any], str] | None = None,
) -> Hydrated:
    """Rebuild every row, dropping — loudly — the ones that cannot be rebuilt.

    The alternative is what this replaces: a list comprehension where the first
    unparseable row raises and the caller returns 500 for the whole set.

    Returns the count rather than publishing it, so this stays safe to call from
    a worker thread (see :data:`_skipped`).
    """
    out: list[T] = []
    bad = 0
    for row in rows:
        try:
            out.append(hydrate_one(row))
        except Exception as exc:  # noqa: BLE001 — a corrupt row must not end the listing
            bad += 1
            ident = ""
            if row_id is not None:
                try:
                    ident = row_id(row)
                except Exception:  # noqa: BLE001
                    ident = "<unreadable id>"
            logger.warning(
                "%s: skipping a row that could not be rebuilt (%s): %s",
                source, ident or "<unknown id>", exc,
            )
    if bad:
        logger.warning(
            "%s: %d row(s) skipped this listing — the rows are corrupt on disk, "
            "not missing; see the per-row warnings above", source, bad,
        )
    return Hydrated(out, bad)


def validated_update(current: BaseModel, fields: dict[str, Any]) -> BaseModel:
    """``current.model_copy(update=fields)``, but with the changed fields checked.

    Raises :class:`pydantic.ValidationError` naming the offending field, at the
    callsite doing the write — instead of writing a value that only fails later,
    on read, for a different request.

    Unknown keys pass through untouched: ``model_copy`` accepts them and some
    callers rely on that, so this narrows the failure mode it was built for
    (a KNOWN field given the wrong shape) rather than changing update semantics.
    """
    for name, value in fields.items():
        field = type(current).model_fields.get(name)
        if field is None or field.annotation is None:
            continue
        try:
            _adapter_for(type(current), name, field.annotation).validate_python(value)
        except ValidationError as exc:
            raise ValidationError.from_exception_data(
                title=f"{type(current).__name__}.{name}",
                line_errors=[
                    {"type": e["type"], "loc": (name, *e.get("loc", ())), "input": e.get("input")}
                    for e in exc.errors()
                ],
            ) from None
    return current.model_copy(update=fields)


#: TypeAdapters are not free to build; updates hit the same few fields over and
#: over (status, ended_at, error, last_heartbeat_at), so build each once.
_ADAPTERS: dict[tuple[type, str], TypeAdapter] = {}


def _adapter_for(model: type, name: str, annotation: Any) -> TypeAdapter:
    key = (model, name)
    a = _ADAPTERS.get(key)
    if a is None:
        a = TypeAdapter(annotation)
        _ADAPTERS[key] = a
    return a
