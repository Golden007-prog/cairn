"""SQLite-backed storage backends (Tier 1: dev default).

The single-file embedded store — restart-safe, queryable, zero ops. Phase 1
ships the run/event store; later phases add memory, RAG, sessions, etc. all
sharing the same SQLite database via :data:`CAIRN_STORAGE` env (default
``sqlite:///./.cairn.db``).

Vector columns will use the ``sqlite-vec`` extension when memory + RAG land
in 4.4 / 4.5.
"""
