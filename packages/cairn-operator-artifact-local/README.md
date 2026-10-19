# obs-operator-artifact-local

Local filesystem-backed artifact store. Reference operator pack #3 of the
Phase-2 set — backs the substrate's artifact service via the
`kind = "service_provider"` pack role.

Pure stdlib — no external dependencies, no external services. Per the
locked architecture, "a tiny local filesystem artifact store can be
built-in for dev." Production deployments swap to `cairn-operator-s3`,
`cairn-operator-gcs`, or `cairn-operator-minio` without any pack changes.

## Operators

| Name | Capability | Risk | Executors |
|---|---|---|---|
| `storage/put` | `storage.put` | `external_write` | `in_process` |
| `storage/get` | `storage.get` | `read_only` | `in_process` |

## Storage layout

Root path resolves via `CAIRN_ARTIFACTS_DIR` (default
`~/.cairn/artifacts`). Per-artifact-name subdir keeps different
`[requires.artifacts.<name>]` decls from colliding:

```
~/.cairn/artifacts/
  rca_report/
    <key1>
    <key2>
  postmortem/
    <key3>
```

URIs are plain `file://<absolute-path>`. A more abstract
`artifact://<store>/<key>` scheme lands in Phase 3+ along with the full
artifact service (lineage, sensitivity labels, retention).

## Inputs / outputs

```python
PutInput:
    store        : str           # named store from [requires.artifacts.<name>]
    content      : str | bytes   # str → UTF-8; bytes verbatim
    key          : str = ""      # derived from sha256(content) if omitted
    content_type : str = ""      # MIME, recorded for downstream consumers

PutOutput:
    ok         : bool
    uri        : str             # file://<absolute path>
    key        : str             # the resolved key (supplied or derived)
    sha256     : str             # hex digest of content
    size_bytes : int

GetInput:
    uri     : str = ""           # file://... — takes precedence
    store   : str = ""           # required if uri is empty
    key     : str = ""           # required if uri is empty
    decode  : bool = True        # True → str (UTF-8); False → bytes

GetOutput:
    ok         : bool
    content    : str | bytes
    sha256     : str
    size_bytes : int
    error      : str
```

## Content addressing

When `storage/put` is called without an explicit `key`, the SHA-256 of
the content becomes the key. Repeated writes of the same content land
at the same URI — idempotent by default. Callers that want stable
named slots supply their own `key`.

## Usage

A template's `operations/save-rca.yaml`:

```yaml
capability: storage.put
input:
  store:        rca_report
  content:      $state.rca.markdown
  content_type: text/markdown
```

The deploying profile (`cairn/profiles/local-dev.toml`)
binds the capability:

```toml
[profile.bindings]
"storage.put" = "storage/fs"
"storage.get" = "storage/fs"
```

## Mock mode

Both operators support mock mode. `storage/put` returns a derived URI
without writing; `storage/get` returns a fixed mock markdown body.

## Wraps

Nothing. The filesystem implementation lives in
`operator_artifact_local._fs` — pure pathlib + hashlib.
