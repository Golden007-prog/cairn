"""Server-side feature gate (#1878) — a disabled feature answers 404.

Hiding a nav item is not a gate: the API is the promise. ``require_feature``
is a FastAPI dependency attached at ``include_router`` time to each router
that backs a flagged surface; it resolves the caller's feature map for the
active workspace (the same resolution ``/auth/me`` reports) and answers 404 —
not 403 — so a disabled feature is indistinguishable from one that does not
exist. In-flight work is never touched: the gate is on the HTTP door only.
"""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import Depends, HTTPException, status

from cairn.core.features import features_for
from cairn.core.triggers.auth import _oauth2_scheme, caller_identity
from cairn.core.workspace import current_workspace


def require_feature(flag: str):
    async def _gate(token: Annotated[Optional[str], Depends(_oauth2_scheme)]) -> None:
        ident = await caller_identity(token)   # 401 when auth is on and the token is bad
        feats = await features_for(workspace_id=current_workspace(), user_id=str(ident.get("username") or ""))
        if not feats.get(flag, True):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"{flag} is not enabled for workspace {current_workspace()!r}")
    _gate.__name__ = f"require_feature_{flag}"
    return _gate


__all__ = ["require_feature"]
