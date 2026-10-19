"""obs-operator-memory-local — SQLite-vec backed memory store.

Reference operator pack #2 of the Phase-2 set. Provides ``memory.search``
and ``memory.write`` capabilities backed by a local SQLite database with
the sqlite-vec extension. Zero external infrastructure required — ships
the §24.4 Tier-1 "one SQLite file for everything in dev" promise.

Architecture role: ``kind = "service_provider"`` per
``architecture_obs_agent_locked.md`` §2 — backs the substrate's memory
service rather than executing a domain-specific operation. The substrate's
profile binds ``memory.search`` and ``memory.write`` to this operator
pack via ``[profile.bindings]``.

The operator wraps :mod:`cairn.services.rag.stores.sqlite_vec` (the
:class:`VectorStore` implementation) plus
:func:`cairn.services.rag.embeddings.get_embedder`. No transport /
connection / schema-validation code lives in this pack.
"""
