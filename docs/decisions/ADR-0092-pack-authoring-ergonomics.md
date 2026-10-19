# ADR-0092 — Pack authoring & wiring ergonomics: one source of truth, author-time contracts

Status: **PROPOSED** (2026-08-21 — epic #1537, slices #1538–#1545).

Relates to / builds on: **ADR-0084** (ONE flow concept × 3 leg kinds — the
composition surface this ADR makes author-safe; epic #1122), **ADR-0068/0070**
(operator config_schema + layering — the scope declaration this ADR makes
fail-closed), **ADR-0045/#209** (registry / distribution — the install story
this ADR forces a decision on), **#1106** (manifest slimming, convention over
configuration — the same instinct, applied here to metadata generation),
**#1511** (CLOSED — the scaffolder's retired code/ shape; this ADR addresses the
*next* scaffolder defect found after that fix).

Research basis: a four-track DX audit of the authoring journey at `4b2cd8e3`
(scaffold → operator contract → flow wiring → install/deploy), every claim
file:line-verified. Key inputs quoted below.

---

## 1. Problem

Creating a domain pack + operators and wiring them into a deployable flow is
hard today, for one structural reason: **cairn's strong guarantees (typing,
validation, governance) live at the tiers an author touches last, while the
tiers an author touches first — scaffold, `cairn validate`, the pipeline — are
weakly or falsely validated.** Feedback that should arrive in the editor
arrives at runtime spend.

Verified defects, by stage:

**Scaffold / create.**
- The advertised first-run loop (`init pack` → `validate` → `run`,
  `init.py:20-25`) is broken twice: `cairn validate .` misdetects the
  scaffold's own `pipelines/hello.yaml` as a *policy* (suffix-based
  `detect_artifact_type`, `core/validate.py:95-110`) and errors; and
  `pack.toml` — the one strictly-schema'd load-bearing file — is **never
  validated** by `validate` at all (`ARTIFACT_GLOBS`, `validate.py:236`); a
  malformed manifest is silently skipped by the loader (`loader.py:106-114`).
- The scaffolded pipeline **cannot run**: `_render_flow` emits an agentic `.md`
  prompt flow, but `_render_pipeline` wires it as a `flow:` leg
  (`init.py:398-469`), which routes to the data/ML engine that only globs
  `flows/*.yaml` (`dataflow/defs.py:231`) — the leg fails at launch. No test
  plans the generated pipeline, so this ships broken.
- Discovery is entry-point-only (`loader.py:103`): nothing is visible until
  `pip install -e .`, and first-party packs additionally require a manual
  root-`pyproject.toml` workspace-member edit.
- The domain-pack `__init__.py` is 60–100 lines of near-identical boilerplate
  derivable entirely from `pack.toml` (compare `packs/incident/__init__.py` vs
  `packs/compress/__init__.py`).

**Operator / data contract.**
- One operator's identity is restated **5×** (decorator, `pack.toml` table key
  + strings, `pyproject` entry point, class attrs); schemas 3×; lifecycle flags
  2× — no codegen keeps them in sync.
- **The composition boundary is untyped**: legs communicate through `$state`
  slots on a `total=False` TypedDict (`compiler.py:417`). Nothing anywhere
  checks that leg N's `output_schema` is assignable to leg N+1's
  `input_schema`; a mis-wire fails as a Pydantic error mid-run, after earlier
  legs' side effects. At the pipeline tier, `assert_runnable` is a **no-op**
  (`pipeline_resolve.py:81-90`) — a leg ref to a missing flow/operator admits,
  spends, then dies. Typed wires (`consumes:`/`produces:`) are opt-in and
  one-sided (`pipeline.py:293-339`).
- The `task:`-leg invoker (`pipeline_bridge.py:952-1021`) re-implements a
  reduced operator invocation: **no config layering, no declared-secret
  injection, no governance-strip, no sharding** — the same operator has a
  different effective contract depending on how it is invoked.
- `config_schema` scope is stringly-typed via `json_schema_extra={"scope":…}`
  and **fails open**: a typo silently demotes a governance-locked knob to
  operational (`config.py:236`).
- Cross-pack copy-paste is rule-induced (no shared helpers): SSH conn-params
  incl. the host-key-safety line duplicated across `remote`/`serving`; the
  whole GPU-provider skeleton duplicated across `runpod`/`vast`/`modal`/
  `kaggle`; 49 pack test files hand-roll an `OperationContext` and bypass the
  real invoke path (no config-merge or governance-strip coverage).

**Wiring / mental model.**
- Three string namespaces joined by convention: operator `group/verb`, kernel
  `dotted.name`, and the leg keyword — where `flow:` vs `template:` silently
  selects engine-vs-LangGraph **by file extension** (`pipeline.py:37-48`), with
  no plan-time validation of the match.
- Two trigger mechanisms (`triggers/*.yaml` — authored, never dispatched — and
  pipeline `on:`, where only `cron` arms, #1159); four incompatible authoring
  conventions across scaffolder, SDK, starter pack, and shipped packs.

**Install / deploy.**
- **There is no working way to add a pack to the running control plane.** The
  dynamic-install surface is triply inert: `_uv_install` shells out to `uv`,
  absent from the runtime image (`registry_install.py:274`,
  `Dockerfile.engine:95-97`); the registry publishes no wheels (release job
  dormant, catalog PyPI names unresolvable); and restart-to-discover destroys
  the install because the engine runs an immutable `image:` with no venv
  volume. The only real path is a full GHCR image rebuild + redeploy.
- No `cairn install/catalog/uninstall` CLI; two confusingly-named SDK packages
  (`cairn_sdk` vs `cairn.sdk`); `[compat] cairn_engine` declared but never
  enforced (`manifest.py:551-552`).

Already fixed, not re-litigated: #1511 (retired `code/` scaffold),
`_check_templates` templates-dir bug (#1391), `PRODUCTION_TIER` build-arg
(#1516).

## 2. Decision

Adopt four principles for the authoring surface:

1. **One source of truth, generated everywhere else.** The operator class
   (decorator + Pydantic schemas) is the single declaration; `pack.toml`
   operator tables, `pyproject` entry points, and the domain-pack `__init__`
   shim are generated (`cairn pack build` grows this; `Pack.from_manifest()`
   collapses the shim). Convention over configuration, per #1106.
2. **Contracts checked at author time, fail closed.** `cairn validate` becomes
   the real gate: content-sniffed artifact detection, `pack.toml` validated via
   `load_manifest`, every pipeline leg ref resolved at plan time
   (`assert_runnable` made real), and inter-operator schema assignability
   checked across wired legs. `config_scope` becomes typed and fails closed.
3. **One invoker, one namespace surface.** A single `invoke_operator_core`
   both template and task-leg paths call (config layering + secrets +
   governance-strip in one place); a uniform leg keyword with internal
   `.md`/`.yaml` resolution (or a plan-time reject on mismatch); the
   kernel↔operator map surfaced in `cairn ls`; `triggers/*.yaml` folded into
   pipeline `on:`.
4. **An honest install story.** Local dev gets instant visibility (dev-mode
   directory scan + `cairn pack add` automating the members/entry-point edit).
   For the running control plane, the owner decides: make dynamic install real
   (bake installer + persist venv + publish wheels) **or** delete the inert
   surface and bless baked-image + redeploy as the one path. Note: hot-loading
   arbitrary pack code into a *governed* control plane is itself a
   supply-chain/governance risk — baked-image-only is a defensible posture if
   stated honestly.

## 3. Consequences

- Authors get editor-time failure instead of runtime-spend failure; one
  declaration instead of five; one convention instead of four.
- Generation adds a build step to operator packs (already exists as
  `cairn pack build`; it grows, it isn't new).
- The inter-operator assignability check constrains schema evolution (an
  output-field rename becomes a compile break downstream — which is the
  point).
- The install decision (principle 4) may retire ADR-0045's v1 dynamic-install
  routes; #209 (distribution bundle) is the natural successor frame.

## 4. Rollout

Epic slices, ordered by leverage: (1) validate/doctor made real; (2) metadata
generation; (3) typed composition; (4) scaffolder fixed + one canonical
convention with a plan/launch CI test; (5) single invoker + fail-closed scope;
(6) namespace/trigger unification; (7) shared core helpers + operator test
harness; (8) local-dev loop + the install-story decision.

---

## Amendment 1 (2026-08-22) — the prod pack-install posture (#1545 D2)

**Status: PROVISIONAL. Deliberately revisitable — see "When to revisit".**

Principle 4 left one question open: how does a pack reach a *running*
production control plane? It is answered here so the gap stops reading as an
oversight, and recorded with both sides written out so revisiting it is a
decision rather than an archaeology exercise.

### Decision

**Option B — the image is the install boundary.** Packs enter production
through `cairn pack add <dir>` → CI → image rebuild → redeploy. There is no
supported path that installs a pack into a live control plane.

The `POST /registry/install` and `POST /registry/uninstall` doors are **kept
and made to refuse honestly** (501, naming the supported path and this ADR)
rather than deleted. A 501 that states the posture tells an operator what to
do instead; a 404 tells them nothing. The lifecycle orchestration behind them
(allowlist → record → validate → rollback) is kept and still tested, because
it is the reusable half if this is revisited.

### Why the previous state was not a third option

The dynamic-install path did not work and could not have:

- `Dockerfile.engine` copies only `/app/.venv` out of the builder stage, so
  `uv` is absent from the runtime image and `uv pip install` could never run.
- The registry publishes no wheels (its build job is dormant; catalog PyPI
  names do not resolve).
- Even on success it wrote into a container filesystem that the
  restart-to-discover step then destroyed — the engine runs an immutable
  image with no venv volume.

It stayed invisible because all 17 install tests inject their own
`installer=`: they proved the bookkeeping and never touched the installer.
**Lesson worth keeping: a seam that is always stubbed in tests is a seam
nobody has ever run.**

### Option A — make dynamic install real

**Pros**
- A pack can be added to a running system with no deploy; useful for a hosted
  multi-tenant tier where customers bring their own packs and cannot wait on
  a release train.
- Shortens the demo/iteration loop for anyone without CI access.
- The console's "Browse registry → Install" story becomes truthful and
  self-service.

**Cons**
- Three pieces of infrastructure, not a feature: an installer baked into the
  runtime image, a persistent site-packages volume, and un-blocked registry
  wheel publishing. Each with its own failure modes.
- **It inverts the product's own principle.** cairn already pins the digest of
  the *job* image an operator ran (`core/operator/pins.py`) so an auditor can
  reconstruct what executed. Option A makes the code surface of the
  *governing* process mutable at runtime with no equivalent pin — "what code
  was serving on August 22nd" stops having a single answer.
- Supply chain: hot-loading third-party Python into the process that enforces
  approval gates means a compromised or typo-squatted registry entry executes
  with the control plane's own privileges.
- Restoring the deleted two-line `uv pip install` would **not** be the fix; a
  real option A needs signed wheels and a recorded install digest.

### Option B — baked image only (chosen)

**Pros**
- Extends to the control plane the principle cairn already enforces on job
  containers: one image, one digest, one answer to "what was running".
- No new infrastructure; the path already exists and is exercised every
  deploy.
- Removes the supply-chain surface entirely rather than mitigating it.
- Stated as a posture it is defensible and even differentiating; left
  unstated it reads as a missing feature. #209 (locked deployable bundle) is
  the natural successor frame.
- The daily pain was never production — it was the local loop, and that is
  now solved independently by `CAIRN_PACK_PATH` (zero-install dev) and
  `cairn pack add` (workspace wiring).

**Cons**
- A pack cannot be added to a running prod system. For a hosted instance, a
  customer wanting their own pack waits for a release.
- Demo friction: "we'll ship it next release", not "give me two minutes".
- Blocks a future self-service multi-tenant tier without revisiting this.
- Operators lose a console affordance that *appeared* to exist (it did not
  work, but its absence is now explicit).

### When to revisit

Revisit if any of these become true: cairn offers a hosted multi-tenant tier
where customers supply packs; a customer's release cadence makes deploy-to-
install unacceptable; or #209 lands a signed, digest-recorded bundle format
that makes option A safe by construction. Doing it later costs the refusal
stubs and the route bodies — small. Doing option A *now* and reversing later
means unwinding a volume-backed mutable install path that things have begun
to depend on, which is not small. That asymmetry is why B is the provisional
default rather than A.
