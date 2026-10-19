"""Process-local registry: ``(template_name, template_sha) → compiled graph``.

DBOS runs workflows in-process, but the un-pickleable ``CompiledStateGraph``
can't be a durable workflow argument — so the run-workflow looks the graph up
here by ``(name, sha)`` instead of receiving it. ``DBOSRuntime.invoke``
registers the graph before submitting.

On a *fresh* process (crash recovery) the registry starts empty, so a boot-time
``rebuild()`` must repopulate it before ``DBOS.launch()`` recovers PENDING runs —
that hook lands with the server-lifecycle wiring (an internal issue). For
single-process runs (CLI/tests) register-on-invoke is sufficient.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("cairn.runtime.dbos.registry")

_GRAPHS: dict[tuple[str, str], Any] = {}

# The run store the DBOS workflow writes to (#1865). ``run_template`` runs on
# DBOS's background loop and used to call ``default_store()`` for its approval
# surfacing and node events — which, with CAIRN_STORAGE unset, builds a FRESH
# in-memory store per call: the writes landed in an instance nobody read and a
# gated in-memory run could not be approved. The server registers the
# dispatcher's store at boot; None → ``default_store()`` (Postgres/SQLite share
# their database, so the fallback is only wrong for the volatile tier).
_RUN_STORE: Optional[Any] = None


def set_run_store(store: Any) -> None:
    """Register the process's run store for the workflow's writes (server boot)."""
    global _RUN_STORE
    _RUN_STORE = store


def run_store() -> Optional[Any]:
    """The registered run store, or None (caller falls back to ``default_store()``)."""
    return _RUN_STORE



def register(template_name: str, template_sha: str, compiled_graph: Any) -> None:
    """Make a compiled graph resolvable by the run-workflow."""
    _GRAPHS[(template_name, template_sha)] = compiled_graph


def lookup(template_name: str, template_sha: str) -> Any:
    """Return the compiled graph for ``(name, sha)``; raise if unregistered."""
    try:
        return _GRAPHS[(template_name, template_sha)]
    except KeyError as exc:
        raise KeyError(
            f"no compiled graph registered for ({template_name!r}, "
            f"{template_sha!r}). DBOSRuntime.invoke registers it before submit; "
            "after a restart the boot-time rebuild() must repopulate it before "
            "DBOS.launch() recovers the run."
        ) from exc


def clear() -> None:
    """Drop all registrations (tests use this to isolate state)."""
    global _RUN_STORE
    _GRAPHS.clear()
    _RUN_STORE = None


def rebuild() -> int:
    """Recompile every installed template into the registry — the boot-time
    repopulation a fresh process needs so ``DBOS.launch()`` can recover PENDING
    runs (only ``(name, sha)`` is persisted with a run, not the graph). Mirrors
    the Temporal worker's ``build_registry``. Returns the count registered;
    a template that fails to compile is logged and skipped (boot must not die).

    Imports are deferred: this loads all packs + operators (heavy) and is only
    called at server boot, and the compiler imports the runtime layer.
    """
    from cairn.core.engine.compiler import compile_template  # noqa: PLC0415
    from cairn.core.operator import load_all_operators  # noqa: PLC0415
    from cairn.core.pack import all_template_dirs, ensure_loaded  # noqa: PLC0415

    load_all_operators(strict=False)
    ensure_loaded()
    n = 0
    for tdir in all_template_dirs():
        for path in Path(tdir).glob("*.md"):
            try:
                compiled = compile_template(path)
                graph = getattr(compiled.runnable, "_compiled_graph", None)
                if graph is None:
                    continue
                sha = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
                register(compiled.name, sha, graph)
                n += 1
            except Exception:  # noqa: BLE001 — keep booting; skip a bad template
                logger.warning("dbos rebuild: failed to compile %s", path, exc_info=True)
    logger.info("dbos registry rebuilt: %d template(s)", n)
    return n
