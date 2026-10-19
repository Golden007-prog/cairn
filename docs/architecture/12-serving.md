# Serving

## Purpose
Stand up a model endpoint, benchmark it, gate acceptance by a human, run batch inference
against a served model under a pre-spend cost gate, and export adapters to deployable
formats. Trace: [../golden-paths/serving.md](../golden-paths/serving.md).

## Owns (OBSERVED)
- Domain pack `cairn/packs/serving/` — pipelines `serve` (`template: serve` agentic leg → `gate: accept-endpoint` showing `serve_status`, `bench_tokens_per_sec`), `batch-inference` (`task: cost/tokens` → `gate: cost` predicate → `flow: batch-infer` with `max_rows` = what the gate priced), `export-model` (`task: model/merge`); flows `serve.md` (agentic), `batch-infer.yaml` (data).
- Operator pack `cairn/operators/official/serving/` — `serve/vllm` (`vllm_serve.py:132 VllmServeOp`, `risk = paid_compute`, `executors = [remote_job, in_process]`, resolves a `compute.ssh` connection via `ctx.compute`, launches vLLM detached on the box, polls `/health`), `serve/start`, `serve/stop` (`external_write`), `infer/bench` (`read_only`), `infer/batch` (`infer_batch.py:197 InferBatchOp`, `external_write`, httpx/asyncio concurrency; ADR-0103 reports); `[ssh]` extra (asyncssh); vLLM is the box's runtime, never a cairn dependency; **no `DESIGN.md`** (in BACKFILL).
- Related: `cairn-operator-compute` (GPU pricing/lifecycle as governed legs, ADR-0097), `cairn-operator-model-registry`, `src/cairn/core/compute_lifecycle.py`, `ssh_exec.py`, `dataflow/engine_door.py`, `connections.py` (compute.ssh).

## Does not own
The LLM gateway used by `via=gateway` batch inference (`services/llm`), model promotion (training/model-registry), the GPU box itself.

## Public interface
Capability `model.serve`; pipelines `serving/serve`, `serving/batch-inference`, `serving/export-model`; `Model` artifact type in pipeline I/O; the OpenAI-compatible `endpoint_url` output consumed by eval legs (`closedbook-eval` post-train measurement in `verifiable-finetune`).

## Dependencies
Operator pack → `cairn.core.operator` (5), `cairn.core.llm` (1, gateway path), `cairn.core.<top>` (1), `cairn.core` (1).

## Side effects
SSH sessions and long-lived processes on GPU boxes (paid), HTTP to endpoints, token spend (metered when prices are supplied), HF downloads for merge/GGUF.

## State
Served-endpoint lifecycle state in `serve_lifecycle.py`/`compute_lifecycle.py`; runs/artifacts as elsewhere.

## Entry points
`POST /start {ref: "serving/serve", inputs: {model_uri}}`; console; `cairn pipeline run serving/batch-inference`.

## Tests
`cairn/operators/official/serving/tests/` (bench, hf shim, infer_batch, serve, serve_lifecycle, serve_reachability, ADR-0103 reports), `tests/core/test_serve_leg_pipeline.py`, `test_assemble_serve_report.py`.

## Invariants / findings
- ADR-0098 "serving as a pipeline leg" — CONFIRMED (`serve.yaml`); the agentic `serve.md` leg wraps the operators; a pure `task: serve/vllm` leg in `verifiable-finetune` is still a HITL pause (`gate: serve-adapter`, the YAML comment cites #1459: "task legs have no compute context").
- Batch inference is gated on a predicted cost and the ceiling equals the priced row count — CONFIRMED in `batch-inference.yaml`.
- `infer/batch` manifest `risk` must match the class attribute — this is the case behind the installed-pack verification ruling in `cairn/operators/CLAUDE.md` §B (install the pack before trusting the drift gates).
