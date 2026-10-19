"""Shared multi-format record loading + format detection (ADR-0074).

This lives in the SHARED services layer — not in an operator pack — because it is
consumed by operators in **both** tiers: `data/profile` (a builtin, which must
work on a base install) and `dataset_checks` / `data/expectations` (official). A
builtin cannot import an official pack, and operator packs must not import each
other's impl, so the one home both can depend on *down* is `cairn/services/`
(precedent: `services/rag/readers`).

`load_records(source)` turns any source — a local path, a `file://` URI, or a
Hugging Face Hub id — into a list of row dicts. The common wire formats
(`.jsonl`, `.json`, `.csv`) are read with the stdlib so the base install needs no
heavy dependency; `.parquet` and Hub ids lazy-import HF ``datasets`` (an optional
extra) and raise a clear error if it is absent. `detect_format(row)` recognises
the common fine-tuning row shapes without any dependency.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Iterator

# Wire formats we can read with the stdlib alone (no optional dep needed).
_STDLIB_SUFFIXES = {".jsonl", ".ndjson", ".json", ".csv", ".tsv"}


def _normalize_source(source: str) -> str:
    """Strip a ``file://`` scheme so ``Path(...).exists()`` works. A bare
    ``file:///tmp/x.jsonl`` otherwise mis-classifies as an HF Hub id."""
    s = source.strip()
    if s.startswith("file://"):
        s = s[len("file://"):]
    return s


def _drop_nulls(row: dict) -> dict:
    """Drop null-valued keys so each row carries only what it actually had —
    matches ``dataset_checks`` behaviour (columnar loaders null-pad)."""
    return {k: v for k, v in row.items() if v is not None}


def _iter_jsonl(path: Path) -> Iterator[dict]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if isinstance(obj, dict):
                yield _drop_nulls(obj)
            else:
                yield {"value": obj}


def _iter_csv(text: str, *, delimiter: str = ",") -> Iterator[dict]:
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    for row in reader:
        # DictReader gives str values; drop empty cells so coverage is honest.
        yield {k: v for k, v in row.items() if k is not None and v not in ("", None)}


def _parse_json_text(text: str) -> list[dict]:
    """A `.json` file: an array of objects, or a wrapper dict with a single
    list value (``{"data": [...]}``), or one object → a single row."""
    obj = json.loads(text)
    if isinstance(obj, list):
        return [_drop_nulls(r) if isinstance(r, dict) else {"value": r} for r in obj]
    if isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, list):
                return [_drop_nulls(r) if isinstance(r, dict) else {"value": r} for r in v]
        return [_drop_nulls(obj)]
    return [{"value": obj}]


def load_records(source: str, *, split: str = "train", limit: int | None = None) -> list[dict]:
    """Load any source into a list of row dicts.

    ``source`` — a local path, a ``file://`` URI, or an HF Hub id (``org/name``).
    ``.jsonl``/``.ndjson``/``.json``/``.csv``/``.tsv`` read via the stdlib;
    ``.parquet`` and Hub ids go through HF ``datasets`` (optional extra). Raises
    ``FileNotFoundError`` for a missing local path with a known suffix, and
    ``RuntimeError`` if a Hub/parquet source is requested without ``datasets``.
    """
    src = _normalize_source(source)
    p = Path(src)
    suffix = p.suffix.lower()

    if suffix in _STDLIB_SUFFIXES:
        if not p.exists():
            raise FileNotFoundError(f"no such data file: {src}")
        if suffix in (".jsonl", ".ndjson"):
            rows = _iter_jsonl(p)
        elif suffix in (".csv", ".tsv"):
            rows = _iter_csv(p.read_text(encoding="utf-8"), delimiter="\t" if suffix == ".tsv" else ",")
        else:  # .json
            rows = iter(_parse_json_text(p.read_text(encoding="utf-8")))
        out: list[dict] = []
        for i, r in enumerate(rows):
            if limit is not None and i >= limit:
                break
            out.append(r)
        return out

    # .parquet or an HF Hub id → the optional `datasets` dep (heavy).
    try:
        from datasets import load_dataset  # optional extra — lazy so base install is light
    except ModuleNotFoundError as e:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            f"reading {src!r} needs the `datasets` extra (parquet / HF Hub); "
            "install it or convert to jsonl/csv"
        ) from e
    if p.exists():
        ds = load_dataset("parquet" if suffix == ".parquet" else "json", data_files=src, split=split)
    else:
        ds = load_dataset(src, split=split)
    rows_list = [_drop_nulls(dict(row)) for row in ds]
    return rows_list[:limit] if limit is not None else rows_list


def load_or_rows(source: str | None, rows: list[dict] | None, *, limit: int | None = None) -> list[dict]:
    """Accept EITHER an inline ``rows`` list (e.g. a Playground upload sample)
    OR a ``source`` to load — whichever is provided. Inline wins."""
    if rows:
        return rows[:limit] if limit is not None else list(rows)
    if source:
        return load_records(source, limit=limit)
    return []


# ── format-family detection (fine-tuning row shapes; no dependency) ──────────
def detect_format(row: dict) -> str:
    """Recognise the common fine-tuning row shapes. Returns one of
    ``openai_chat`` | ``sharegpt`` | ``alpaca`` | ``completion`` | ``unknown``."""
    if isinstance(row.get("messages"), list):
        return "openai_chat"
    if isinstance(row.get("conversations"), list):
        return "sharegpt"
    if "instruction" in row and "output" in row:
        return "alpaca"
    if "text" in row or ("prompt" in row and "completion" in row):
        return "completion"
    return "unknown"


def detect_dataset_format(rows: list[dict]) -> str:
    """The dataset's format = the majority row format (first non-unknown wins
    ties). Empty → ``unknown``."""
    if not rows:
        return "unknown"
    counts: dict[str, int] = {}
    for r in rows[:200]:
        counts[detect_format(r)] = counts.get(detect_format(r), 0) + 1
    known = {k: v for k, v in counts.items() if k != "unknown"}
    if known:
        return max(known, key=lambda k: known[k])
    return "unknown"


def cell(row: dict, key: str, aliases: list[str] | None = None) -> Any:
    """Resolve a semantic role to a real column: exact key, then any alias,
    case-insensitively. Returns None if unresolved."""
    if key in row:
        return row[key]
    lut = {k.lower(): k for k in row}
    for candidate in [key, *(aliases or [])]:
        real = lut.get(candidate.lower())
        if real is not None:
            return row[real]
    return None
