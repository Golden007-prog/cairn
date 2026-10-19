"""The review records API (#1468) — read surface over gated decisions.

Workspace-scoped (#1639): both routes filter on the request's active workspace,
and a review id belonging to another workspace 404s rather than 403s.

Read-only by design: a review is OPENED when a HITL gate fires and DECIDED by
the same ``/pipelines/{id}/steps/{id}/approve`` call that releases the gate
(or closed out when the pipeline is cancelled / gate-blocked). Exposing a
decide route here would create a second approval surface beside the gate —
exactly what the owner ruling forbids.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException

from cairn.core.triggers.auth import auth_enabled, caller_identity, require_auth
from cairn.core.workspace import current_workspace

logger = logging.getLogger(__name__)


async def decision_actor(token: Optional[str], claimed: Optional[str], *, fallback: str) -> str:
    """Who decided a gate (#2310): the authenticated principal, never a body field.

    With auth on, the recorded approver is the token's identity, resolved the way
    ``GET /auth/me`` resolves it (the username the console shows as "Approver").
    A body/query ``approver`` is still ACCEPTED for compatibility (the CLI, SDK
    and older consoles send one) but ignored; a mismatch is logged so a client
    that tried to record someone else is visible. With auth off there is no
    principal to check against, so the claimed name (or ``fallback``) stands —
    keyless dev was always unauthenticated."""
    said = (claimed or "").strip()
    if not auth_enabled():
        return said or fallback
    who = str((await caller_identity(token)).get("username") or "")
    if said and said != who:
        logger.warning("gate decision: approver %r in the request ignored; recorded the caller %r", said, who)
    return who


def build_reviews_router(review_store: Any) -> APIRouter:
    r = APIRouter()

    @r.get("/reviews", dependencies=[Depends(require_auth)])
    async def list_reviews(status: Optional[str] = None, limit: int = 100) -> dict[str, Any]:
        rows = await review_store.list_reviews(
            status=status, limit=max(1, min(limit, 500)),
            workspace_id=current_workspace(),   # #1639 — never the unscoped read
        )
        return {"reviews": [x.model_dump(mode="json") for x in rows]}

    @r.get("/reviews/{review_id:path}", dependencies=[Depends(require_auth)])
    async def get_review(review_id: str) -> dict[str, Any]:
        # #1639 — another workspace's review id 404s exactly like an unknown one,
        # so the id itself never confirms that the review exists.
        got = await review_store.get_review(review_id, workspace_id=current_workspace())
        if got is None:
            raise HTTPException(status_code=404, detail=f"no review {review_id!r}")
        return got.model_dump(mode="json")

    return r
