"""obs-operator-rag-local — SQLite-vec backed RAG retrieval.

Reference operator pack #4 of the Phase-2 set. Provides ``rag.search``
and ``rag.fetch`` capabilities backed by a local SQLite database with
the sqlite-vec extension. Mirrors the cairn-operator-memory-local shape;
distinct collection namespace (``rag_<corpus>`` vs ``memory_<store>``)
so a pack can host memory + RAG side by side without collision.

Architecture role: ``kind = "service_provider"`` per
``architecture_obs_agent_locked.md`` §2 — backs the substrate's RAG
service. The substrate's profile binds ``rag.search`` and ``rag.fetch``
to this operator pack via ``[profile.bindings]``.

The operator wraps :mod:`cairn.services.rag.stores.sqlite_vec` and
:func:`cairn.services.rag.embeddings.get_embedder`. No transport /
connection / schema-validation code lives in this pack.
"""
