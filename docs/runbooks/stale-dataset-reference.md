# Runbook — stale or unresolvable dataset reference

**Symptoms.**
1. A leg fails **at launch** with `dataset ref resolution failed: dataset ref 'dataset:x@N' not found in the registry`.
2. A readiness leg fails seconds in with `dataset verification FAILED for … the registry pinned <hash>`.
3. (Fixed in #2171) `FileNotFoundError: … 'file:/…'` — a `file://` URI collapsed to a path.

## Why
`dataset:` refs resolve on the control plane before the engine sees them
(`src/cairn/core/dispatch/leg_executor.py:185-201`) to `<uri>#sha256=<content_hash>`; the
kernel verifies the bytes it reads against the fragment (`operator_dataset_checks/dataflow_stages.py:70-85`).
So (1) is a name/version that does not exist in *this* workspace's registry; (2) means the
object at the URI changed since registration (overwrite, pruned box, re-exported file) — the
registry never re-points a version.

## Diagnose
```
curl -s $CAIRN_ENDPOINT/datasets | jq '.[] | {name, versions}'
curl -s $CAIRN_ENDPOINT/datasets/<name>/versions | jq '.[] | {version, uri, durable_uri, verdict, workspace_id}'
sha256sum <local path from uri>          # compare with the version's artifact id
```

## Fix
- (1) Use the right name/version or workspace; a version registered in another workspace
  is invisible by design.
- (2) Do not edit the registry. Re-run the producing flow (import/readiness) — identical
  bytes register as the same id; changed bytes become a new version — and launch on that
  version. If the bytes only moved (box gone), prefer `durable_uri` (S3 relay) by
  registering from the durable location.
- (3) Upgrade to a build containing #2171.

## Prevent
Register through flows (`register:` on the export), keep `CAIRN_ARTIFACT_S3_BUCKET` set so
every version has a `durable_uri`, and never hand-edit files under a registered URI.
