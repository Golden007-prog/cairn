"""Domain-neutral retrieval (RAG) service.

The engine owns the retrieval machinery — embeddings, pluggable vector stores,
and the ingest / query workflows (embed → hybrid-retrieve → rerank →
synthesize). A pack supplies only *what* to retrieve over: its collection name,
and optionally a domain-flavored synthesis prompt.

Public API:
  - ``search(query, *, collection, ...)``   — retrieve + synthesize an answer
  - ``ingest_document(file_path, collection)`` — chunk + embed + store a doc
  - ``get_vector_store()`` / ``get_embedder()`` — the configured infra singletons

Lazy-export note (Phase 2.4): ``ingest_document`` and ``search`` pull in
heavyweight optional deps (``llama_index``, ``trafilatura``) — fine when
those code paths are actually used, but they shouldn't be required just
to touch the vector-store or embedder layer (operator packs like
``cairn-operator-memory-local`` and ``cairn-operator-rag-local`` import
``services.rag.stores`` directly but never call ingest/query). The
module-level ``__getattr__`` below defers those imports until first
attribute access; ``get_embedder`` / ``get_vector_store`` stay eager
since they're small.
"""

from cairn.services.rag.embeddings import get_embedder
from cairn.services.rag.stores import get_vector_store

__all__ = [
    "search",
    "ingest_document",
    "get_vector_store",
    "get_embedder",
]


def __getattr__(name: str):
    # PEP 562 — module-level __getattr__ runs only when an attribute is
    # missing, so the heavy submodules stay unimported until something
    # actually asks for ``search`` / ``ingest_document``.
    if name == "ingest_document":
        from cairn.services.rag.ingest import ingest_document as _id
        return _id
    if name == "search":
        from cairn.services.rag.query import search as _s
        return _s
    raise AttributeError(f"module 'cairn.services.rag' has no attribute {name!r}")
