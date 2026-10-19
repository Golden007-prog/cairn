"""hello-cairn's run-hook listener: a receipt line per settled leg and finished run.

Glue, not an operator (ADR-0127 D2). The engine fires run hooks through the
`cairn.run_hooks` entry-point group (cairn/core/pack/run_hooks.py); this pack's
pyproject declares

    [project.entry-points."cairn.run_hooks"]
    hello-receipts = "cairn_pack_hello.hooks:receipts"

so installing the wheel is what turns it on. A dev pack loaded through
`CAIRN_PACK_PATH` declares no entry points, so nothing fires there.

Every `leg.finished`, `run.finished` and `run.failed` appends one JSON line to
`$CAIRN_HOME/hello-cairn/receipts.jsonl` (`CAIRN_HOME` defaults to `~/.cairn`). The
hook sees every run on the engine, not only this pack's, which is what a receipt log
is for. Only JSON scalars from the payload are written: `run.finished` also carries
the dispatcher's run store, which is an object, not data.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: The events a receipt is written for; the others pass through untouched.
EVENTS = ("leg.finished", "run.finished", "run.failed")


def receipts_path() -> Path:
    home = os.environ.get("CAIRN_HOME") or str(Path.home() / ".cairn")
    return Path(home).expanduser() / "hello-cairn" / "receipts.jsonl"


async def receipts(event: str, payload: dict[str, Any]) -> None:
    """``(event, payload) -> None``, the run-hook signature."""
    if event not in EVENTS:
        return
    line = {k: v for k, v in payload.items() if isinstance(v, (str, int, float, bool)) or v is None}
    line["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Hooks run on the engine's event loop; the file write goes to a worker thread so a
    # slow disk never stalls the dispatcher.
    await asyncio.to_thread(_append, receipts_path(), json.dumps(line, sort_keys=True))


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text + "\n")
