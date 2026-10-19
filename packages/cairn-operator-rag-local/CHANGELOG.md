# Changelog

## 0.1.0

- Initial release — reference operator pack #4 of Phase 2.
- Two operators: `rag/search` (read_only) + `rag/fetch` (read_only).
- Wraps `cairn.services.rag.stores.sqlite_vec.SqliteVecStore` + `cairn.services.rag.embeddings.get_embedder`.
- Per-corpus collection isolation via `rag_<corpus>` prefix.
- `rag/fetch` implemented via filtered search until `VectorStore.fetch_by_source` lands.
- Mock mode for both operators.
