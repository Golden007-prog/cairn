# obs-operator-rag-local

Local SQLite-vec backed RAG retrieval. Reference operator pack #4 of
the Phase-2 set — backs the substrate's RAG service via the
`kind = "service_provider"` pack role.

Ships zero-config. Per-corpus collection isolation
(`rag_<corpus>` prefix) keeps `runbooks`, `service_docs`, etc. from
colliding within the same deployment.

## Operators

| Name | Capability | Risk | Executors |
|---|---|---|---|
| `rag/search` | `rag.search` | `read_only` | `in_process` |
| `rag/fetch` | `rag.fetch` | `read_only` | `in_process` |

## Inputs / outputs

```python
SearchInput:
    corpus  : str            # named corpus from [requires.rag.<corpus>]
    query   : str            # embedded by substrate embedder
    top_k   : int = 5
    filters : dict = {}      # payload-match constraints

SearchOutput:
    ok    : bool
    hits  : list[SearchHit]   # {score: float, payload: dict}
    error : str

FetchInput:
    corpus : str
    source : str             # source identifier as recorded at ingest

FetchOutput:
    ok     : bool
    found  : bool            # True iff any chunks matched
    chunks : list[FetchedChunk]   # [{payload: dict}, ...]
    error  : str
```

## Search vs fetch

- `rag/search` runs similarity ranking — embed the query, rank candidates,
  return top_k.
- `rag/fetch` returns all chunks for a known source, bypassing the ranker.
  Use after a `rag/search` hit when you want full document text rather
  than the single top-scoring chunk.

## Naming

The locked architecture mentioned `rag/sqlite-bm25`. Today,
`services/rag/stores/sqlite_vec.py` is dense-vector only ("Sparse
accepted but ignored (upstream-in-flight)"). The operator name + the
capability shape stay stable; only the retrieval ranking changes when
true hybrid BM25 + dense lands.

## Usage

A template's `operations/recall-runbook.yaml`:

```yaml
capability: rag.search
input:
  corpus: runbooks
  query:  $state.symptom
  top_k:  3
```

The deploying profile binds the capability:

```toml
[profile.bindings]
"rag.search" = "rag/search"
"rag.fetch"  = "rag/fetch"
```

## Mock mode

Both operators support mock mode and return shaped-but-fake payloads
for offline template development.

## Wraps

- `cairn.services.rag.stores.sqlite_vec.SqliteVecStore` (storage + retrieval)
- `cairn.services.rag.embeddings.get_embedder` (embeddings)

No transport / connection / schema-validation code lives in this pack.
