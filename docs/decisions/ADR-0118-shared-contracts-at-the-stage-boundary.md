# ADR-0118 — Shared contracts at the dataflow stage boundary

- **Status:** ACCEPTED on merge (maintainer, 2026-10-07: "1a 2a 3a 4a 5a 6a 7a" = M1a, M2a,
  M3a, M4a, M5a, M6a, M7a, the recommended option in each; see Maintainer decisions).
- **Date:** 2026-10-07
- **Issue:** #2298, the prerequisite of #2242 (V1, maintainer "1b": V1's stages come under
  contract). Parent epic #2239.
- **Amends** ADR-0111, extending its contracts from operations to dataflow and ML flow stages,
  which ADR-0111 D2/P5 left to classification. **Amends** the locked `DataflowSpec` contract
  (`docs/contracts/README.md`). Accepting this ADR is the CLAUDE.md §5 approval for: D1 (stage
  `implements`); D2's reserved-key rule; the exact field delta in D4; D5's single validation
  function and the `[kernel]` `jsonschema` floor; and the M1–M7 options the maintainer selects.
  It approves nothing beyond that.
  Keeps ADR-0081 D3 (engine-neutral spec, never rows, no registry on job machines) and ADR-0090
  (interpreter-owned result contract).
- **Design note and verified facts:**
  [`issue-2298-stage-contracts-design.md`](../architecture-fix/issue-2298-stage-contracts-design.md).

## Context
- **The seam covers operators only.** Shared contracts (ADR-0111, #2240/#2241) are checked at
  one seam, `invoke_operator_core`. That covers task legs and template operation nodes.
- **Stages bypass it.** A `flow:` leg never reaches that seam. Neither does a `dataflow/run` or
  `ml/run` operator: it is reached, but its stages run on the engine.
- **Stages carry almost no declared interface.** A stage is a plain function
  `(workdir, config, inputs) -> scalar stats`. The in-tree interpreter, `run_dataflow`, runs it
  on an engine's machine, which has no registry or database. A stage declares only a risk class.
  Nothing checks its configuration or its outputs.
- **The control plane already depends on stage outputs.** `cairn/core/intake.py` reads the
  intake stages' stats by name.
- **V1 needs nine stages under contract.** These are the eight stages of the `intake` flow, plus
  `scan.all`, which V1's `check` template submits through `dataflow/run`.

## Decision (proposed; M1–M7 choose the open points)
1. **Identity.** A stage kernel may declare `implements = ("contract://<id>/<rev>",)` beside
   `risk_class`. The contract it names is a **stage contract**, whose format M1 decides. A stage
   contract can never be implemented by an operation, and an operation contract never by a
   stage. A stage without `implements` is legacy and runs exactly as today.
2. **What a stage contract covers.**
   - `configuration`: the config the kernel receives. For a mapped stage this is the per-item
     config (`ref.config` plus the `as:` item).
   - `output`: the kernel's **raw** returned stats, before the runner adds sysmetrics, capture
     keys or `kernel_version`. For a mapped stage, the check runs on the merged result.
   - **Not covered:** rows, or row content. Content checks remain explicit governed check stages
     and operators (ADR-0111 P4). The kernel's `inputs` artifact refs are not covered either
     (M1 option c would add them).
   - **Output schemas are open.** They constrain the keys they declare and never forbid
     others (no `additionalProperties: false`). The keys the runner adds (sysmetrics, capture
     keys, `kernel_version`) are a reserved set that a stage contract may not declare. So the
     same schema gives the same answer on the kernel's raw stats and on the merged stats the
     resume and descriptor re-checks see.
   - Within the v1 schema vocabulary, stats schemas stay open-ended where keys are dynamic
     (`bucket_*`) or carry JSON strings (`failures_json`).
   - The contract's value is identity pinning plus the stat keys the control plane depends on.
     That is worth having, and is the stated reason for doing this.
3. **Admission.**
   - The launch door records one entry per opted-in stage of each `flow:` leg. It also records
     entries for each opted-in stage a `dataflow/run`/`ml/run` operation node reaches inside an
     admitted template leg.
   - Entry keys are structured: leg, a node path for the template door, the stage index and the
     stage name. They are structured fields beside the existing node path (D4).
   - Repeats of the same declared stage share one entry: loop iterations (`readiness` runs
     `intake` up to 3 times), retries and resumes. The occurrence goes on the run record, not
     on the admission entry.
   - A flow with `gate:` segments is refused at the door when any of its stages is opted in. The
     pipeline path refuses such flows at execute time already.
   - The leg is never re-resolved after admission (invariant 4).
4. **Exact field delta.** All fields are optional; absent means legacy.
   - `InvocationEntry` gains `surface: "operation" | "stage"`, default `"operation"`, so existing
     rows hydrate unchanged. For a stage, `operator_ref` carries the stage name and
     `operator_version` the kernel distribution version.
   - `DataflowSpec` gains `contracts: {format: 1, leg: <admitted leg id>, schemas: {<uri>:
     <self-contained schema>}}`. The schemas table is deduplicated by URI.
   - `StageRef` gains `contract: {capability_ref, version, configuration: <uri>, output: <uri>}`.
   - The door bundles each part into one self-contained schema, with every `$ref` inlined. The
     job machine therefore needs only `jsonschema`, already in the `[kernel]` extra, and not
     `referencing` or the registry.
   - **Size bound.** The spec travels in an environment variable, and Linux caps one
     environment string at 128 KiB. So the **submission sites** (`execute_leg` and
     `DataflowRunOp.submit`, where config is
     final) measure the whole serialized opted-in spec (contracts, config and paths together)
     and refuse beyond 96 KiB. Legacy specs are
     unchanged.
   - **Deterministic serialization.** The bundle has sorted keys and URIs, because
     `checkpoint_key` is recomputed on every `execute_leg` call (restart, adoption, Temporal
     replay), and a non-deterministic bundle would silently miss resumes.
   - **Key representation.** `InvocationEntry` gains optional `stage_index: int` and
     `stage: str`. For a stage, `path` is the leg id, or `<leg>/<node path>` for the template
     door. Repeats share the entry.
   - **Stage entries stay out of the operation lookup.** The ADR-0117 channel builder
     (`dispatcher.py:1126-1130`) and the node lookup (`_node_contract`, `compiler.py:446-452`)
     take only `surface == "operation"` entries. Stage entries reach a node's
     `dataflow_contracts` keyed by (node path, `stage_index`). Without this, a stage entry at a
     `dataflow/run` node's path would be read as that node's own operator contract and refused
     by the identity check.
   - **Template-door carrier.** `OperationContext` gains an optional `dataflow_contracts` field
     (the stamp plus that node's stage contracts). The compiler fills it for `dataflow/run` and
     `ml/run` nodes from the ADR-0117 `contract_invocations` channel of an admitted leg.
     `DataflowRunOp` / `MlRunOp` copy it into the spec they submit. Without it an admitted
     template leg's spec would go out unstamped, which is a silent bypass.
   - **Engine declaration.** The `DataflowEngine` protocol gains an optional attribute
     `carries_stage_contracts: bool`, read with a default of `False`. The control plane refuses
     an opted-in spec on an engine that does not declare it, in `execute_leg` and in
     `DataflowRunOp.submit`, before submission or provisioning. This is a positive declaration:
     a third-party `validate()` cannot be relied on to refuse.
   - `spec_key` changes for opted-in specs. That is correct: the contract is part of what runs.
5. **Where each check runs.** Every check uses **one** validation function in
   `cairn.core.dataflow` over `jsonschema`, on self-contained schemas. The box imports that
   module already. `ContractEnvelope.problems()` on the operator path delegates to it, so there
   is one implementation, not a mirror. The `[kernel]` extra's `jsonschema` floor is raised to
   the core floor (`>=4.26`).
   - **Control plane, before the attempt and reservation loop:** configuration, as resolved for
     the leg. Kernel defaults are not visible here, so schemas mark `required` only the keys the
     kernel truly requires. A refusal is non-retriable.
   - **Preview:** refuses only what is statically known. Dataset and judge refs, injected
     `eval_paths` and upstream wires resolve at leg time.
   - **Interpreter, before each opted-in stage:**
     - identity, meaning the loaded kernel implements the recorded revision at the recorded
       distribution version (the two registries can disagree today);
     - configuration.
   - **Interpreter, after the stage, before its done marker and checkpoint:** the raw output.
     A stage that fails here leaves no marker, so a resume cannot skip it.
   - **Resume:** frozen stats are re-checked. Their `kernel_version` must equal the recorded
     version, or the resume refuses. For an opted-in stage, a missing `kernel_version` refuses;
     today it is best-effort (`stages.py:53-54`).
   - **Control plane, on the result:** each stage descriptor's stats are re-checked, not the
     flattened `<stage>.<key>` view. This happens before artifact registration and before the
     intake verdict, at **both** registration sites: `execute_leg`'s
     `register_leg_descriptors` and `DataflowRunOp.poll` (`operators/run.py:104-111`).
   - **Any refusal** fails that stage, and so its leg, as a `ContractConformanceError` does for
     task and template legs (ADR-0117).
6. **No-envelope behaviour (M2).** Inside an admitted spec (`contracts.leg` stamped), an
   opted-in stage with no `contract` refuses: lost provenance, as in ADR-0117 D2. An unstamped
   spec runs opted-in stages as legacy. That covers the dev loop (ADR-0106 D6), `cairn flow
   export` and an unadmitted template.
7. **Engine obligation (M5).**
   - All four in-tree engines (builtin and SSH, prefect, metaflow, skypilot) run kernels through
     `run_dataflow`. Builtin, SSH and SkyPilot run it through the `cairn-dataflow-run` binary;
     Prefect and Metaflow call it in-process in their worker. No protocol guarantees this: the
     engine group is open, `StubEngine` runs no kernel, and exported flows call kernels directly.
   - So an engine **declares** `carries_stage_contracts` (D4), and the control plane refuses an
     opted-in spec on an engine that does not, before submission or provisioning.
   - The conformance kit gains interpreter-backed cases (conforming, non-conforming, no
     envelope inside an admitted spec, unknown format, oversized). They run for builtin,
     prefect and metaflow.
   - SkyPilot's image-less path installs no cairn at all (a pre-existing gap). That engine
     refuses opted-in specs until it can carry them.
8. **Temporal.** Stage entries reach `execute_leg` through the Temporal flow activity exactly as
   through the bridge. #2299, the missing flow gate there, is fixed separately.

## Verification (verify-upstream, 2026-10-07)
- **Check A, LangGraph:** N/A. Stages are not graph nodes.
- **Check B, workflow engines:** the engines are transports for `run_dataflow`: Prefect and
  Metaflow call it in their worker, and SkyPilot runs the binary. Cairn passes them only an
  opaque JSON spec, so none of them validates a stage's arguments. Their native typing
  (Prefect task parameters, Metaflow parameters) would bind the contract to one engine,
  contrary to ADR-0081 D3. Nothing to reuse.
- **Check C, ecosystem:** `jsonschema` (already a core and `[kernel]` dependency) does the
  validation. Pydantic stays the spec's own model.
- **Check D, this codebase:**
  - `ContractEnvelope.problems()` (`cairn/core/operator/effective.py`) is the existing validator.
    D5 moves that validation into one function in `cairn.core.dataflow`, over self-contained
    schemas, and `problems()` delegates to it. So the box does not import `cairn.core.operator`
    (runtime, loader, registry).
  - The launch-door walk extends `_reachable_operators` / `_invocation_entry`; it is not a new
    resolver.
  - The engine conformance kit (`cairn/core/dataflow/conformance.py`) is extended; no new
    harness is built.

## Consequences
- **Not additive everywhere.**
  - An older core refuses a contract distribution carrying the new stage format: discovery
    rejects unknown kinds and fields (`cairn/core/pack/contracts.py:135,290-292`), which fails
    every launch. Stage contracts therefore ship in their **own** distribution, and core
    understands the format before any such wheel is installed.
  - An older runner refuses a spec with the new fields through Pydantic (`extra="forbid"`)
    before any kernel runs. In-tree engines ship the runner with core.
  - An older core reading a newer record hydrates stage entries as operator entries
    (`InvocationEntry` ignores unknown fields). For a template leg, it would key one under the
    `dataflow/run` node's path and refuse that node on the identity check. That fails closed, not
    silently, so it is recorded rather than guarded.
- **Contracts touched:**
  - Execution/dataflow: `DataflowSpec`, `StageRef`, `spec_key`, and the `DataflowEngine`
    protocol (`carries_stage_contracts`).
  - Operator: `OperationContext.dataflow_contracts`.
  - Capability: the stage format.
  - Pipeline/Run: `InvocationEntry`.
  - Operator/Pack: stage `implements`, plus `pack.toml` if M3 says so.
  - Governance, if M4 says so.
- **V1 scope consequences:**
  - Nine stages: the 8 intake stages plus `scan.all`.
  - The `dataflow/run` door is included.
  - Data stages only. ML stages (`train.qlora`, TrainSpec drift) come with V2.
- **Corrected now (M6):** the `StageRef` docstring and ADR-0081 D2 promise per-stage config
  validation that does not exist.

## Alternatives rejected
- **Control-plane checks only:** they miss a kernel on the job machine that differs from the
  admitted one, and cannot fail the stage where it ran.
- **Stages only behind operators:** the `flow:` pipeline leg, which ADR-0084 made a first-class
  leg kind beside ADR-0081 D1's `dataflow/run` door, never passes an operator.
- **Row validation in the engines:** content checks stay with explicit governed check stages
  and operators (ADR-0111 P4). Each data format would also add cost.

## Maintainer decisions
Selected 2026-10-07: **(a) for M1–M7.** Consequences of the selection:
- **M1a:** a new definition kind `stage`; the operation format is unchanged.
- **M3a:** no `pack.toml` change.
- **M4a:** a separate issue for keying flow-gate policies by contract.
- **M6a:** the `StageRef` docstring is corrected in this change.
- **M7a:** V1 extracts the shared helpers of the four kernel-reuse intake stages.

- **M1, stage contract format:**
  - (a) a new definition kind `stage`, with `configuration` and `output` and no `input`
    (recommended);
  - (b) `cairn.capability/2` with `surface` and an optional `input`;
  - (c) as (a), plus an `inputs` schema over the kernel's artifact refs.
- **M2, no envelope inside an admitted spec:**
  - (a) refuse, as D6 says (recommended);
  - (b) run legacy and record a warning.
- **M3, identity declaration:**
  - (a) the function attribute only, with a drift test against the contract (recommended);
  - (b) also a `[stage."<name>"]` section in `pack.toml`, a Pack contract change.
- **M4, which identity flow-gate policies key on:**
  - (a) no change in V1: the aggregate risk only, with a separate issue on keying policies by
    contract ref (recommended);
  - (b) V1 feeds the stage contract refs to `enforce_flow_gate`.
- **M5, engine obligation:**
  - (a) an engine declares `carries_stage_contracts`; the control plane refuses otherwise
    (recommended);
  - (b) assume every engine uses `run_dataflow`.
- **M6, the `StageRef` docstring:**
  - (a) correct it now, in this PR (recommended);
  - (b) leave it to V1.
- **M7, the four intake kernels that call `Operation().run`** (`dedup.exact`, `dedup.near`,
  `pii.redact`, `decontam.apply`):
  - (a) V1 extracts their shared helpers, per the 2026-10-06 disposition ("target shape =
    shared helper, migrated per pack in #2242/#2243") (recommended);
  - (b) they stay in-engine helpers, recorded as a known gap.
