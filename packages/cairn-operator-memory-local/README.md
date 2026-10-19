# obs-operator-memory-local

Local SQLite-vec backed memory store. Reference operator pack #2 of the
Phase-2 set — backs the substrate's memory service via the
`kind = "service_provider"` pack role.

Ships zero-config: no Qdrant, no Postgres, no external services. Stores
records in a local SQLite file using the `sqlite-vec` extension.

## Operators

| Name | Capability | Risk | Executors |
|---|---|---|---|
| `memory/search` | `memory.search` | `read_only` | `in_process` |
| `memory/write` | `memory.write` | `production_mutation` | `in_process` |

Risk note: `memory.write` is classified as `production_mutation` (the
nearest match in the locked enum for "DB write"). A future risk-enum
extension may add `local_write` for tighter classification. Phase 3+
policy can override per-deployment.

## Inputs / outputs

```python
SearchInput:
    store   : str            # named store from [requires.memory.<store>]
    query   : str            # embedded by substrate embedder
    top_k   : int = 5
    filters : dict = {}

SearchOutput:
    ok    : bool
    hits  : list[SearchHit]   # {score: float, payload: dict}
    error : str

WriteInput:
    store      : str
    record     : dict        # payload conforming to the pack's declared schema
    embed_text : str
    id         : str = ""    # derived from embed_text if omitted

WriteOutput:
    ok    : bool
    id    : str
    error : str
```

## Per-store isolation

Multiple `[requires.memory.<store>]` declarations in a single
deployment get their own underlying SQLite collection — the operator
prefixes each one as `memory_<store>` so the incident pack's
`similar_incidents` and `service_runbooks` don't collide.

## Usage

A template's `operations/recall-incident.yaml`:

```yaml
capability: memory.search
input:
  store: similar_incidents
  query: $input.symptom
  top_k: 3
```

The deploying profile (`cairn/profiles/local-dev.toml`)
binds the capability to this operator pack:

```toml
[profile.bindings]
"memory.search" = "memory/sqlite-vector"
"memory.write"  = "memory/sqlite-vector"
```

## Mock mode

Both operators support mock mode. `memory/search` returns one fixed
mock hit; `memory/write` returns a derived id without writing. Useful
for offline template development.

## Wraps

- `cairn.services.rag.stores.sqlite_vec.SqliteVecStore` (storage)
- `cairn.services.rag.embeddings.get_embedder` (embeddings)

No transport / connection / schema-validation code lives in this pack.
