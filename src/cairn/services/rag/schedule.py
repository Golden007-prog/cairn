"""RAG index scheduler — periodic re-index per corpus freshness (ADR-0023 slice 2).

A sibling to :class:`cairn.core.dispatch.scheduler.SkillScheduler`, reusing
the same Tier-1 in-process apscheduler — but firing ``index_corpus`` directly
rather than the skill/template path (indexing is a lifecycle side-effect, not an
agent pipeline; ADR-0023 Fork C resolution). Tier-2 durable scheduling (Temporal)
is the same-interface swap later, exactly as for SkillScheduler.

Each corpus whose ``[requires.rag.<name>].freshness`` maps to a periodic cron
becomes one apscheduler job. ``on_change`` (and ``manual``) get no schedule — the
startup pass covers them and the content-hash incremental makes re-runs cheap.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from cairn.core.storage.rag_catalog import RagCatalogStore
from cairn.services.rag.lifecycle import CorpusTarget, discover_corpora

logger = logging.getLogger("cairn.rag_scheduler")

# Named freshness → cron. Anything else that is 5 space-separated fields is
# treated as a raw cron expression; everything else (on_change/manual) → no job.
_FRESHNESS_CRON = {
    "hourly": "0 * * * *",
    "daily": "0 3 * * *",
    "weekly": "0 3 * * 0",
}


def freshness_to_cron(freshness: Optional[str]) -> Optional[str]:
    if not freshness:
        return None
    f = freshness.strip().lower()
    if f in ("on_change", "on-change", "manual", ""):
        return None
    if f in _FRESHNESS_CRON:
        return _FRESHNESS_CRON[f]
    if len(freshness.split()) == 5:  # raw cron passthrough
        return freshness
    logger.warning("[index] unrecognized freshness %r — no schedule", freshness)
    return None


class RagIndexScheduler:
    """Fires periodic corpus re-indexing via apscheduler."""

    def __init__(self, *, catalog: RagCatalogStore | None = None) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        self._catalog = catalog
        self._scheduler = AsyncIOScheduler()
        self._jobs: dict[str, str] = {}  # corpus_id → job_id

    def register_all(self, packs=None) -> int:
        """Register a cron job for each corpus with a periodic freshness."""
        from apscheduler.triggers.cron import CronTrigger

        registered = 0
        for target in discover_corpora(packs):
            cron = freshness_to_cron(target.freshness)
            if not cron:
                continue
            try:
                job = self._scheduler.add_job(
                    self._fire,
                    CronTrigger.from_crontab(cron),
                    args=[target],
                    id=f"rag-index:{target.corpus_id}",
                    name=f"rag-index {target.corpus_id} @ {cron}",
                    misfire_grace_time=300,
                    coalesce=True,
                    max_instances=1,
                    replace_existing=True,
                )
                self._jobs[target.corpus_id] = job.id
                logger.info("rag scheduler registered: corpus=%s cron=%s", target.corpus_id, cron)
                registered += 1
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "rag scheduler skipped corpus=%s reason=%s", target.corpus_id, exc
                )
        return registered

    def start(self) -> None:
        """Register jobs and start. Idempotent."""
        if self._scheduler.running:
            return
        self.register_all()
        self._scheduler.start()
        logger.info("rag scheduler started with %d job(s)", len(self._jobs))

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("rag scheduler stopped")

    def list_jobs(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for corpus_id, job_id in self._jobs.items():
            job = self._scheduler.get_job(job_id)
            nrt = getattr(job, "next_run_time", None) if job else None
            out.append({"corpus_id": corpus_id, "job_id": job_id, "next_run_time": str(nrt) if nrt else None})
        return out

    async def _fire(self, target: CorpusTarget) -> None:
        """Re-index one corpus. Errors are logged, never propagated."""
        from cairn.services.rag.indexer import index_corpus

        logger.info("rag scheduler firing corpus=%s", target.corpus_id)
        try:
            r = await index_corpus(
                target.corpus_id, target.root,
                catalog=self._catalog, collection=target.collection,
            )
            logger.info(
                "rag scheduler indexed corpus=%s status=%s (+%d/~%d/-%d)",
                target.corpus_id, r.status, r.documents_indexed,
                r.documents_skipped, r.documents_deleted,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("rag scheduler fire failed corpus=%s: %s", target.corpus_id, exc)
