# ADR-0089 — Metaflow as governed compute substrate, not the execution DAG

Status: **ACCEPTED** (2026-08-11 — epic #1343, issues #1344–#1350).
Both rulings are made (§6): **R1** keep D1, **R2** govern engine-run flows at
admission and document the boundary (#1347). Where per-call enforcement would
live once obligations exist (#700) is deliberately left open.

Relates to / builds on: **ADR-0082** (training subflow engines — the `ml`
flow kind and the `resolve_engine(kind="ml")` seam this ADR extends),
**ADR-0081** (dataflow engine protocol — the engine-neutrality rule and the
`DataflowSpec` boundary contract), **ADR-0084** (control plane: ONE flow
concept, runtimes/engines as adapters — the posture that makes "substrate, not
DAG" the correct reading), **ADR-0088** (durable stage handoff — the
checkpoint/resume this ADR must not regress). Depends on #1108
(`kernel.from_operator` auto-lift). Post-substrate parallelism: #1290 (G4
foreach).

Research basis: docs.metaflow.org (Runner, `@resources`, `@retry`,
`@kubernetes`/`@batch`, `@secrets`/`@environment`, S3 datastore, metadata
service) + the shipped adapter (`engines_metaflow.py`, `metaflow_bridge.py`) +
the 2026-08-11 feature-status audit (`scratchpad/round2/`, issue #1342).

---

## 1. Context

The stated goal: make the **`ml` flow kind and the cairn SDK fully usable on
the Metaflow engine**, with cairn **operators, policy, and environment**
supported — Metaflow supported "natively."

What ships today (verified in code, 2026-08-11):

* The Metaflow adapter runs the **entire** dataflow inside **one** Metaflow
  step. `CairnFlow` is a fixed `start → execute → end`; the `execute` step
  calls cairn's own `run_dataflow()` for the whole stage list
  (`metaflow_bridge.py:67-96`). This is deliberate and honest — the docstring
  states it — because Metaflow's graph is **static** (defined by `self.next()`
  at flow-definition time) while cairn's stage list is a **dynamic** runtime
  `DataflowSpec`. Mapping N dynamic stages to N Metaflow steps would require
  code-generating a flow per pipeline, which D1 forbids.
* Consequently **none** of Metaflow's substrate is wired: no `@resources`, no
  `@retry`, no remote scheduling (`@kubernetes`/`@batch`), no `@secrets`. The
  only Metaflow features actually used are the subprocess Runner, pathspec
  re-attach, and (optionally) an S3 datastore.
* The submission boundary (`DataflowSpec`, `contract.py`) deliberately carries
  **no** policy, environment, connections, or secrets (ADR-0081: engine-neutral;
  ADR-0082 D3: engines get no database/registry credentials). The shared runner
  `run_dataflow` has **zero** policy/ctx/audit awareness.
* Environment reaches the flow only by **wholesale process-env inheritance**
  (`env = dict(os.environ)`, `engines_metaflow.py:123`) — which over-shares
  every control-plane secret locally and under-provisions on any remote task.
* The engine-selection model overloads `ml_target` as the **metadata root**, so
  there is no way to say "Metaflow **and** a GPU/k8s compute target."

So the current answer to "does the ml flow run on Metaflow" is *yes, but as a
subprocess wrapper around cairn's own runner, on the control-plane box, with no
governance inside the flow and no remote compute.*

## 2. The reframe

The trap in "support Metaflow natively" is that the **native DAG is the least
valuable Metaflow feature for cairn**: cairn already owns a governance graph
*and* stage-level checkpoint/resume (ADR-0088). What cairn does **not** have,
and what Metaflow supplies, is the **execution substrate**: remote GPU/k8s
scheduling, per-run `@retry`, `@resources`, an S3 datastore, and `@secrets`
injection.

**Decision: treat Metaflow as the execution substrate, not the execution DAG.**
Keep the single-`execute`-step `CairnFlow` (record stays source of truth, D1
intact, `run_dataflow` still shared with builtin/prefect), and decorate that
step with Metaflow's execution machinery driven by the environment backend
config. The flow runs *on Metaflow's infra* — genuinely Metaflow-native
**execution** — it is simply not a Metaflow-native **graph**.

## 3. Rejected alternative — code-generate a Metaflow flow per pipeline

Generate a `FlowSpec` with one `@step` per stage. Gains: native DAG/UI/cards,
per-step retry/resources, `foreach`, Metaflow-level step resume. Rejected
because it:

* **Violates D1** — a generated flow file per pipeline is exactly the "every
  future adapter becomes a rewrite" failure the runner's docstring warns about.
* **Breaks engine-neutrality** — Metaflow would stop sharing `run_dataflow`, so
  the conformance suite (one runner, every engine) no longer holds.
* **Reopens content-hash drift** — the record is no longer the single source of
  truth; a generated artifact must be kept in sync and hashed.
* **Cannot express runtime-dynamic flows** — loops and config-driven stage lists
  can't be frozen into a static graph at build time.

Code-gen is only correct if the product posture flips to *Metaflow-first
authoring* (users write Metaflow; cairn is a thin wrapper). ADR-0084 sets the
opposite posture, so this alternative is out unless that ADR is revisited.

## 4. Design (R1: keep D1)

Engine-neutral knobs live in the environment backend
(`[profile.backends.dataflow.metaflow]`), never on `DataflowSpec` (ADR-0081 D4).

* **D1 — selection carries a compute target (#1345).** Split identity from
  compute: keep `metadata_root`/`datastore`; add a `compute` block
  (`kind: local|kubernetes|batch`, `queue`, resource defaults). `resolve_engine`
  stops overloading `ml_target` as the metadata root.
* **D2 — execution decorators on `execute` (#1346).** Apply `@resources`
  (from the existing engine-neutral `StageResources` hints, `contract.py:40-52`),
  `@retry`, and remote scheduling (`@kubernetes`/`@batch`) to the single step,
  from the `compute` block. Static graph preserved; no per-stage code-gen.
* **D3 — scoped secret injection (#1344).** Resolve the environment's declared
  connections **control-side** into a *scoped* secret set (only what the flow's
  stages declare — ties to `requires_secrets` #112) and inject via `@secrets`/
  `@environment`, replacing wholesale env inheritance. ADR-0082 D3 preserved:
  the engine still gets no registry/DB credentials, only the runtime secrets its
  stages need.
* **D6 — cross-box durability (#1349).** Make `datastore` (S3) + `metadata
  service` first-class so poll/`resume` work across boxes; remote `@batch`/
  `@kubernetes` tasks require the shared datastore anyway.
* **operators-as-stages (#1108).** Flows run *kernels*, not arbitrary cairn
  *operators*; `kernel.from_operator` auto-lift is the bridge. Out of scope for
  this ADR's engine work but required for the full "support cairn operators"
  requirement; tracked as the dependency.
* **SDK parity (#1350).** `flow.ml` gains engine-neutral `resources`/`retry`
  hints that compile onto `StageResources`/budget and flow to the D2 decorators;
  no `flow.native` (keep one authoring surface). `cairn flow export --to
  metaflow` must render the substrate-faithful single-step flow.

Also fixes a real bug found alongside: **cancel is a silent no-op after a
control-plane restart** (#1348) — the detached child survives and keeps
computing; persist pgid + pathspec and kill on re-attach.

## 5. Consequences

* Metaflow's **own UI shows one opaque `execute` node**, not the pipeline's
  stages. Accepted: cairn's console already renders the combined stage graph
  (#1077), which is where a governance product should surface it.
* Native **per-stage** retry/resources are not available; flow-level retry/
  resources are. For ML training (one heavy GPU step) this is the right grain.
* True per-task parallelism arrives separately as **G4 `foreach`** (#1290) — the
  only native-per-task path this ADR endorses, and only for genuinely parallel
  stages.
* `run_dataflow` stays the one interpreter across builtin/prefect/metaflow; the
  conformance suite stays the guardrail.

## 6. Rulings

### R1 — keep D1. **Ruled: yes** (2026-08-11)

Metaflow is the substrate; the bridge flow is never code-generated per pipeline.
§3 records why the alternative is rejected.

### R2 — the in-flow policy boundary. **Ruled: document it** (2026-08-11, #1347)

**cairn governs an engine-run flow at ADMISSION, not per call.** A flow is
gated before it runs — plan-time structural gates on the aggregated stage risk,
plus the door's own `external_write` risk — and the engine then executes the
admitted stage list with no policy engine inside it.

The 2026-08-11 audit framed the missing in-flow enforcement as a governance
hole: "a platform running kernels over sensitive data with an unredacted in-flow
path." **That overstated what ships.** Checking the code rather than the framing:

* policy effects are **admission decisions** — `deny > require_hitl >
  require_dry_run > allow_auto` (`core/policy/engine.py:8`). Enforcing `deny`
  mid-flow is meaningless: the decision is made before the flow starts.
* there is **no obligation mechanism at all** — no redact/mask/per-call-budget
  primitive exists anywhere in `core/policy/`. Content guardrails as a policy
  obligation are **proposed in #700, not built**.
* **no shipped kernel calls an LLM or egresses.** Only three packs register
  stages (`builtins/dataflow`, `official/dataset_checks`, `official/training`);
  `dataset_synth` does call an LLM but exposes **operators only**, so it takes
  the governed operator path and never runs inside an engine-executed flow.

Per-call enforcement would therefore police obligations that do not exist,
against a risk no shipped kernel presents, using a format #700 has not defined.
The boundary is right for today, and is now recorded as *intentional* rather
than left as an unexamined gap.

**The guard that keeps it honest.** The risk vocabulary is exactly
`read_only | external_write`, and `stage_risk_class` coerces anything else to
`external_write` — fail-closed and correct, but *silently*, which is precisely
how this boundary would erode unnoticed. Coercion now **warns and names this
ruling**: a kernel declaring a new class (an LLM call, a network egress) is
asking a question the vocabulary cannot answer, and that is the moment R2 must
be reopened. Admission behavior is unchanged — the guard adds visibility, not
leniency.

**Revisit when** any of: #700 lands an obligation primitive; a kernel gains LLM
or egress capability; or a deployment needs per-call governance of kernel I/O.

### Deliberately left open

Where enforcement should live once obligations exist — a sealed decision bundle
passed to the runner, a kernel-level chokepoint, or reusing the gate-split so
governed work stays control-side — is **undecided** (2026-08-11). Choosing now
would mean designing against an obligation format that does not yet exist. The
same applies to whether remote `@batch`/`@kubernetes` tasks would need an
audit-forwarding channel back to the control plane.
