"""Ambient knowledge retrieval + injection + citations — Slice 3 (#497, ADR-0061).

A template declares knowledge role slots (``TemplateSpec.knowledge``). This
module composes the corpora bound to those roles (Slice 2's ``resolve_role``),
retrieves from each **via the built ``rag/search`` operator** (never LlamaIndex —
that stays in the ingest path), fuses + re-ranks the hits across corpora, and
renders a citation-carrying context block for injection into the LLM call
(``compile_template(extra_context=…)``). Every hit is cited by its source pack +
corpus + locus so the injected knowledge stays auditable (feeds run provenance
+ the #472 Citations facet).

Deterministic + best-effort: no bound corpora / no query / a corpus that errors
→ that corpus contributes nothing; an empty compose returns ``("", [])`` and the
workflow runs unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from cairn.services.rag.roles import resolve_role

#: A retrieval callable: (corpus_name, query, top_k) -> list[(score, payload)].
#: Defaults to the rag/search operator; injectable so tests stay hermetic.
SearchFn = Callable[[str, str, int], Awaitable[list[tuple[float, dict]]]]


@dataclass(frozen=True)
class Citation:
    """One retrieved chunk's provenance — what a workflow must cite."""

    source_pack: str
    corpus: str
    source: str = ""   # document path/id from the ingested payload
    page: str = ""     # page label / locus, when present
    score: float = 0.0
    preview: str = ""  # first ~300 chars of the chunk

    def to_dict(self) -> dict:
        return {
            "source_pack": self.source_pack, "corpus": self.corpus,
            "source": self.source, "page": self.page,
            "score": round(self.score, 4), "preview": self.preview,
        }


@dataclass(frozen=True)
class KnowledgeContext:
    """The result of a compose: a markdown block (for ``extra_context``) + the
    citations backing it. ``text`` is ``""`` when nothing was retrieved."""

    text: str = ""
    citations: list[Citation] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.citations)


async def _operator_search(corpus: str, query: str, top_k: int) -> list[tuple[float, dict]]:
    """Retrieve via the built ``rag/search`` operator (embedder + vector store —
    no LlamaIndex). Resolved by ref through the operator registry, never by
    importing the pack that provides it (ADR-0120 D5/D8, #2415): the provider is
    an optional dep of the engine, and when none is installed this raises —
    :func:`compose_knowledge` treats that corpus as contributing nothing."""
    from cairn.core.operator.loader import ensure_loaded
    from cairn.core.operator.protocol import OperationContext
    from cairn.core.operator.registry import default_registry

    ensure_loaded(registry=default_registry)
    if not default_registry.has("rag/search"):
        raise LookupError("knowledge retrieval needs the `rag/search` operator — "
                          "install an operator pack that provides it")
    op_cls = default_registry.get("rag/search")
    ctx = OperationContext(pipeline_id="knowledge-inject", node_name="rag/search")
    inp = op_cls.input_schema(corpus=corpus, query=query, top_k=top_k)
    out = await op_cls().run(inp, ctx)
    if not getattr(out, "ok", False):
        return []
    return [(float(h.score), dict(h.payload or {})) for h in out.hits]


def _citation(target, score: float, payload: dict) -> Citation:
    return Citation(
        source_pack=target.pack,
        corpus=target.name,
        source=str(payload.get("source") or payload.get("source_path") or ""),
        page=str(payload.get("page") or payload.get("page_label") or ""),
        score=float(score),
        preview=str(payload.get("text") or payload.get("content") or "")[:300],
    )


def _render(cits: list[Citation]) -> str:
    lines = ["## Knowledge (retrieved — cite the source pack for every claim you draw from it)"]
    for c in cits:
        locus = f"{c.source_pack}:{c.corpus}"
        if c.source:
            locus += f" · {c.source}"
        if c.page:
            locus += f" p.{c.page}"
        lines.append(f"- [{locus}] {c.preview}")
    return "\n".join(lines) + "\n"


async def compose_knowledge(
    roles: list[str],
    query: str,
    *,
    active_roles: Optional[set[str]] = None,
    top_k: int = 4,
    search: Optional[SearchFn] = None,
    packs=None,
) -> KnowledgeContext:
    """Compose ambient knowledge for ``roles``: union the bound corpora
    (Slice 2), retrieve from each via ``rag/search``, fuse + re-rank across
    corpora by score, cap at ``top_k`` so one large corpus can't crowd out the
    rest, and render a cited context block.

    ``active_roles`` gates by the environment's attach-list (a role not in it
    resolves empty). ``search`` overrides the operator (tests). Best-effort per
    corpus; empty compose ⇒ ``KnowledgeContext("", [])``.
    """
    if not roles or not query:
        return KnowledgeContext()
    search = search or _operator_search

    # union the corpora across roles, de-duped by pack-qualified id
    seen: set[str] = set()
    targets = []
    for role in roles:
        for t in resolve_role(role, active_roles=active_roles, packs=packs):
            if t.corpus_id not in seen:
                seen.add(t.corpus_id)
                targets.append(t)
    if not targets:
        return KnowledgeContext()

    # retrieve per corpus (best-effort), tag each hit with its corpus
    scored: list[tuple[float, Citation]] = []
    for t in targets:
        try:
            hits = await search(t.name, query, top_k)
        except Exception:  # noqa: BLE001 — one bad corpus never breaks the compose
            continue
        for score, payload in hits:
            scored.append((float(score), _citation(t, score, payload)))
    if not scored:
        return KnowledgeContext()

    # re-rank: score fusion across corpora, then cap (the #497 budget decision —
    # a simple deterministic default; a cross-encoder reranker is #506).
    scored.sort(key=lambda sc: sc[0], reverse=True)
    top = [c for _, c in scored[:top_k]]
    return KnowledgeContext(text=_render(top), citations=top)


__all__ = ["Citation", "KnowledgeContext", "compose_knowledge", "SearchFn"]
