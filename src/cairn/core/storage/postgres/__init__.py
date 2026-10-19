"""Postgres-backed storage backends (Tier 2: prod default).

The §24.4 P5 tier. Today: :class:`PostgresRunStore` for runs + events. Future:

  - ``PostgresBus`` (LISTEN/NOTIFY) for §24.10 Tier-2 messaging
  - ``PostgresSessionStore`` / ``ApprovalStore`` / ``BudgetStore`` for P6
  - Postgres-backed adapters around the existing pgvector-driven
    ``HybridRagStore`` / ``EpisodicMemoryStore`` for full prod-tier
    consolidation

Set ``CAIRN_STORAGE=postgres://user:pass@host:5432/db`` to route the run
store here. Pair with ``RAG_VECTOR_STORE=pgvector`` + ``PGVECTOR_DSN``
for the same Postgres to host RAG vectors too — that's the "one Postgres
for everything" prod story §24.4 promises.

Requires the ``storage-postgres`` optional dep group
(``pip install 'cairndev[storage-postgres]'``).
"""
