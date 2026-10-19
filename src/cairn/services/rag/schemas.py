from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class SourceNode(BaseModel):
    score: float
    chunk_type: str
    source: str
    page: str
    preview: str


class QueryResponse(BaseModel):
    answer: Optional[str]
    sources: List[SourceNode]
