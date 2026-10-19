# Changelog

## 0.1.0

- Initial release — reference operator pack #2 of Phase 2.
- Two operators: `memory/search` (read_only) and `memory/write` (production_mutation).
- Wraps `cairn.services.rag.stores.sqlite_vec.SqliteVecStore` and `cairn.services.rag.embeddings.get_embedder`.
- Per-store collection isolation via `memory_<store>` prefix.
- Mock mode for both operators.
