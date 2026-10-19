# ADR-0088: Durable stage handoff — cross-box resume for flow legs (H6)

- **Status:** Accepted (2026-08-10 — all slices merged: H6a #1309, H6b/c #1310,
  H6d #1311/#1312; live kill-the-box acceptance PASSED on the training box:
  checkpoint relayed in 4.2s, remote process group SIGKILLed, H2 retry resumed
  from the registry, pipeline done with stage 0 never re-run. Transport ruling
  recorded in Consequences: SSH is the default engine channel, NATS serves
  persistent workers, gRPC is not adopted.)
- **Date:** 2026-08-08
- **Related:** ADR-0086 (run-lifecycle hardening — this is slice H6, #1248),
  ADR-0081 D3 (job machines get no registry/database credentials), #1243
  (both engine bridges call `run_dataflow`, so this lands once and serves
  builtin/SSH/Prefect/Metaflow), #1078 (Metaflow datastore overlap).

## Context

Stage-granular checkpointing exists and is cairn's own: the builtin runner
writes a `.done` marker per stage and skips completed stages on re-run. But
markers and intermediate outputs live in a box-local workdir. A control-plane
restart resumes cleanly (H1 re-attach), and a same-box re-run skips completed
stages — **but if the executing box dies, mid-flow progress is gone**: H2 can
re-run the leg from scratch on another target, nothing more. No engine choice
fixes this (both bridges wrap the whole stage loop in one flow-run/step), and
the pieces already exist: a registered `ArtifactDescriptor` per stage, an
artifact store with content addressing + parent lineage, and a file-based
engine contract (`CAIRN_DATAFLOW_SPEC` in, results out) every engine speaks.

## Decision

Checkpoint declared stages through the artifact store; resume seeds a fresh
workdir on any target from the newest valid checkpoint. Six decisions:

### D1 — Checkpointing is DECLARED, per stage, off by default

A stage gains `checkpoint: true`. Only checkpointed stages upload their
working set (the stage's output files); descriptors keep flowing for every
stage as today. Rationale: uploading every intermediate of a large flow
doubles IO for stages that cost seconds to recompute — the author knows
which stages are expensive (train, heavy transforms), and the declaration is
itself a record the audit trail can show. No inference, no heuristics.

### D2 — Upload is SYNCHRONOUS at the stage boundary, and its failure degrades

A checkpoint that claims durability must be durable when claimed — an async
trailing upload is a marker that lies during the loss window, and a fake
checkpoint is worse than none. Cost is bounded by D1 (opt-in on stages where
upload time ≪ recompute time). A FAILED upload never fails the flow: the
stage completes, `checkpoint_failed` lands in the stage stats and the audit
trail, and the leg simply degrades to today's restart-from-zero behavior.
Durability insurance failing must not destroy the work it was insuring.

### D3 — Resume rides the EXISTING engine contract: `resume_from` in the spec

`DataflowSpec` gains an optional `resume_from` block:

```yaml
resume_from:
  spec_key: "9f2c…"            # the runner's existing spec hash — MUST match
  stage_index: 2                # last durably completed stage
  artifacts:                    # bytes to seed the fresh workdir with
    - {uri: "s3://…/current.jsonl", content_hash: "sha256:…", dest: "current.jsonl"}
```

The runner seeds the workdir, writes synthetic `.done` markers for stages
`<= stage_index` (marker content from the checkpoint's frozen stats), and
continues at `stage_index + 1`. Because #1243 made both bridges call
`run_dataflow`, this lands once and every engine path inherits it.
`spec_key` mismatch (the spec changed since the checkpoint) invalidates the
resume entirely — a changed spec restarts from stage 0, the same validity
rule `.done` markers already follow.

### D4 — Bytes move THROUGH the engine channel; no new credentials on boxes

ADR-0081 D3 stands: job machines get no registry or store credentials. The
CONTROL PLANE moves checkpoint bytes — for the SSH engine it pulls the
stage's files over the existing SSH channel and writes them to the artifact
backend; for the local engine it copies directly. Registration (descriptor →
store) stays control-plane-only, unchanged. Direct box→object-store upload
via control-plane-minted presigned URLs is recorded as the scale
optimization (phase 2) for when relay IO becomes the bottleneck — it keeps
D3 intact (a presigned PUT is a scoped, expiring capability, not a standing
credential) but is not needed to make cross-box resume TRUE.

### D5 — The control plane owns resume selection; same-box resume is untouched

`.done`-marker resume on the same box keeps working with zero changes — H6
only adds the cross-box path. The resubmit sites that already exist (H2
retry, the resume/adoption path, manual re-run) ask the artifact store for
the leg's newest checkpoint keyed by `(spec_key, run lineage)`; if one
exists and validates, they attach `resume_from` to the fresh submit. Each
attempt gets a fresh `budget.max_seconds` (wall-clock budgets are
per-invocation; the SERIES budget tier is what caps total spend).

### D6 — A resumed run's lineage POINTS AT the original run's artifacts

Resumed stage artifacts carry `parents` → the checkpoint content hashes of
the original run, and the leg's run record carries an audit event
`leg_resumed_from_checkpoint {stage_index, content_hash, source_run_id}`.
Reproducibility survives the handoff: the chain shows one logical flow
across two physical runs.

## Consequences

- The builtin engine gains the last durability property it lacked; the
  standing answer "cairn needs no external orchestrator for durability"
  becomes true without an asterisk.
- Metaflow's per-step S3 datastore stops being a durability argument for the
  ENGINE tier — its remaining value is for Metaflow-native users via
  `cairn flow export` (record on #1078).
- Checkpointed stages pay an upload at each boundary — the author's declared
  trade, visible in stage stats.
- **Transport ruling (2026-08-08, reaffirmed by the H6d live proof):** the
  engine channel stays SSH — control-plane-initiated, credential-asymmetric
  (ADR-0081 D3: the box never holds control-plane secrets), and the same
  proven exec/sftp machinery carries submit, poll, checkpoint pull (H6d) and
  resume-byte upload. NATS `RemoteJobChannel` remains the channel for
  PERSISTENT workers (a box that outlives jobs and dials out). gRPC is not
  adopted: it would require a listening service + credentials ON the box,
  inverting D3 for no capability the two existing channels lack. At scale,
  bulk checkpoint bytes move to presigned object-store URLs (the D4 phase-2
  `http(s)` path already consumed by `_seed_resume`), not to a new transport.

## Alternatives rejected

- **Replicate descriptors only** — resume would re-derive bytes by re-running
  earlier stages; that is H2's restart-from-zero wearing a costume.
- **Async trailing upload** — a durability marker that lies during the loss
  window; the failure mode (box dies inside the window) is exactly the event
  the feature exists for.
- **Rows-in-database checkpointing** — re-litigates ADR-0080's settled
  control/data split; bytes stay in object/file storage, the database keeps
  references.
- **Lean on Prefect/Metaflow persistence** — both bridges wrap the stage
  loop in one unit (#1243); their per-task/per-step persistence cannot see
  cairn's stages, and adopting their granularity re-opens the bridge
  re-implementation #1243 just deleted.

## Execution

| Slice | Scope |
|---|---|
| H6a | Runner: `checkpoint:` stage field, synchronous local checkpoint emit, `resume_from` seeding + synthetic markers (builtin local; hermetic tests) |
| H6b | SSH relay: control plane pulls checkpoint files over the existing channel, registers + stores them |
| H6c | Resume selection: artifact-store checkpoint query keyed by spec_key; H2/resubmit/adoption attach `resume_from`; `leg_resumed_from_checkpoint` audit |
| H6d | Acceptance (box-gated): kill the box after stage 2 of 4; resubmit on a different target runs only 3–4; lineage crosses the runs |
