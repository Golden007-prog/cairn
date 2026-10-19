# Golden path — serving

## What this workflow does
Stands up an OpenAI-compatible endpoint for a model/adapter on a GPU box you own,
benchmarks it, and stops at a human before anyone is pointed at it (`serving/serve`);
runs batch inference against a served model under a pre-spend cost gate
(`serving/batch-inference`); exports an adapter to merged/GGUF/Modelfile/vLLM formats
(`serving/export-model`).

## Trigger
- `POST /start {"ref": "serving/serve", "inputs": {"model_uri": "hf://… | file://…"}}` (console, after a training promote).
- `cairn pipeline run serving/batch-inference --set path=dataset:x@3 --set rows=1000 --set prompt_template=…`.

## Execution diagram
```mermaid
flowchart TD
  subgraph serve pipeline
    A[launch serving/serve] --> B[template: serve  agentic leg<br/>flows/serve.md]
    B --> B1[operation: serve/vllm  paid_compute<br/>ctx.compute = compute.ssh connection]
    B1 --> B2[ssh: vllm serve --enable-lora … detached · poll /health]
    B2 --> B3[operation: infer/bench  read_only]
    B3 --> B4[report: serve_status · bench_tokens_per_sec]
    B4 --> G{gate accept-endpoint · HITL}
    G --> O[outputs.endpoint = deploy.serve_endpoint_url]
  end
  subgraph batch-inference pipeline
    C[task: cost/tokens] --> D{gate cost<br/>steps.estimate.usd <= input.max_cost_usd}
    D --> E[flow: batch-infer  kind: data<br/>max_rows = what the gate priced]
    E --> F[infer/batch stage: httpx concurrency vs endpoint | gateway]
    F --> H[register annotated export · real token spend on series]
  end
```

## Exact source trace
```
serve
  launch                              src/cairn/core/dispatch/pipeline_launch.py:161 ← cairn/packs/serving/pipelines/serve.yaml
  template leg                        src/cairn/core/dispatch/pipeline_bridge.py:859 _submit_step → dispatcher.run_template (dispatcher.py:565)
    ├ compile                         src/cairn/core/engine/compiler.py:92 ← cairn/packs/serving/flows/serve.md
    ├ operation serve/vllm            operation_node_fn compiler.py:1574 → resolve_runtime_gate (paid_compute → gate per profile/policy)
    │   ├ invoke_operator             src/cairn/core/operator/runtime.py:888 → enforce_gate :950 → pick_executor (remote_job | in_process)
    │   ├ compute connection          src/cairn/core/operator/runtime.py:255 resolve_declared_compute → src/cairn/core/connections.py (compute.ssh)
    │   └ VllmServeOp.run             cairn/operators/official/serving/src/operator_serving/operators/vllm_serve.py:161
    │       └ _deploy (asyncssh)      same file; vLLM is the box's runtime ([vllm] on the box, never a cairn dep)
    ├ operation infer/bench           operators/infer_bench.py
    ├ lifecycle                       operators/serve_lifecycle.py · src/cairn/core/compute_lifecycle.py (serve/start, serve/stop)
    └ reports (ADR-0103)              ctx.report.scalars(...) → RunEvents → gate `show:` fields
  gate accept-endpoint                pipeline_bridge.py gate leg → _pause_leg_for_approval :1035 → review
batch-inference
  task cost/tokens                    pipeline_bridge.py:1187 _submit_task_leg → :1294 _invoke_task_op → enforce_gate :1332
  gate cost (predicate)               engine/pipeline_gate.py · pipeline_bridge.py:_decide_loop (data gate, no human)
  flow batch-infer                    cairn/packs/serving/flows/batch-infer.yaml → leg_executor.py:92 execute_leg
    └ infer/batch                     operators/infer_batch.py:197 InferBatchOp (via=endpoint | via=gateway → src/cairn/core/llm)
export-model
  task model/merge                    operator (model_registry / serving) → merged, GGUF, Modelfile, vLLM stanza URIs
```

## Important state transitions
Endpoint process on the box (detached; `serve/stop` tears down); `Run` per leg; serve
findings (`serve_status`, `bench_tokens_per_sec`, `endpoint_url`) as report scalars;
batch export registered as a dataset version when `register` is set; series spend settled
with real token cost when prices are supplied.

## Governance
`serve/vllm` is `paid_compute` → per-operation gate resolves from profile/policy;
`accept-endpoint` HITL gate; batch inference cannot start above `max_cost_usd`
(predicate gate) and cannot exceed the priced row count; `infer/batch` is
`external_write`; `serve/stop` is `external_write`. Budget `{usd: 25}` on batch inference.

## Execution
SSH (`asyncssh`, `[ssh]` extra) to a `compute.ssh` connection; vLLM on the box;
httpx for inference; optionally the governed gateway (`via=gateway`).

## Output
`outputs.endpoint`; bench metrics; annotated dataset export (`batch-inference`); export
URIs (`export-model`).

## Tests
`cairn/operators/official/serving/tests/test_serve.py`, `test_serve_lifecycle.py`,
`test_serve_reachability.py`, `test_bench.py`, `test_infer_batch.py`, `test_hf_shim.py`,
`test_serving_adr0103_reports.py`; `tests/core/test_serve_leg_pipeline.py`,
`test_assemble_serve_report.py`, `test_batch_cpu_budget.py`.

## Where to modify it
| Change | File |
|---|---|
| a new serving backend (TGI…) | `vllm_serve.py:_build_command` (docstring: "a swap, not a rewrite") + `pack.toml` + `DESIGN.md` (missing today) |
| the acceptance criteria | `cairn/packs/serving/pipelines/serve.yaml` `gate: accept-endpoint show:` |
| batch pricing | `cost/tokens` operator (`cairn-operator-compute`/`eval`) and `batch-inference.yaml` inputs |
| a serve leg as a plain task (no agentic wrapper) | blocked on task legs having compute context (#1459 per the training pipeline comment) — needs an architecture review first |
