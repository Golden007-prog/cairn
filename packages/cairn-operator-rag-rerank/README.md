# cairn-operator-rag-rerank

Cross-corpus re-ranking for RAG hits (`rag/rerank`, #513).

Re-scores `(query, passage)` so relevance — not corpus size — decides the top-k
when a knowledge role composes hits from many corpora.

- **identity** (default, no deps): sort by the incoming score.
- **cross_encoder** (`[cross_encoder]` extra): a local cross-encoder model.
- **cohere** (`[cohere]` extra, `COHERE_API_KEY`): Cohere Rerank API.

A missing extra falls back to identity — never a hard failure.
