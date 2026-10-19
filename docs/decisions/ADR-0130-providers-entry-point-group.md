# ADR-0130 — Providers are discovered through one `cairn.providers` entry-point group keyed by kind and name

- **Status:** ACCEPTED 2026-10-15 by the owner, with every recommendation in "Decisions the owner
  takes on acceptance" (1–7) taken as written. No code lands with this record; slice 1 (bus, LLM
  gateway, vector store) is implemented under its own issue.
- **Date:** 2026-10-15
- **Issue:** none yet (the implementation issue is filed on acceptance). Resolves ADR-0128's
  owner decision 7 (the unproven core services have "no service-provider entry-point group" to
  move through) by adding that group. ADR-0128 is reserved by PR #2593 (#2535).
- **Builds on** ADR-0010 (the bus), ADR-0035/0036 (runtime tiers), ADR-0052 (the backend
  configurator), ADR-0058 (one storage selector), ADR-0081 D4 (core owns engine selection, packs
  own construction), ADR-0120 (what is public; core reaches the platform only through entry
  points), ADR-0121 (repositories and names), ADR-0123, ADR-0125 (in-process leg liveness),
  ADR-0127 D5 and its errata. **Amends** nothing locked. This is the decision root `CLAUDE.md`
  §5 requires before an entry-point group is added.

## Context

A deployment chooses five backends: where the record lives, how events fan out, which runtime
executes an agentic leg, how LLM calls are routed, and which vector store serves retrieval.
For four of the five the choice is a hardcoded branch in core, so a paid or private
implementation (a managed Postgres with IAM auth, a hosted bus, a durable runtime, a
commercial LLM gateway) cannot be added by the platform or a third party without editing core.
Verified on `origin/main` 2f21a54a:

- **Storage.** `default_store()` reads `CAIRN_STORAGE` and branches on the URL prefix:
  `postgres://`/`postgresql://` → `PostgresRunStore`, `sqlite://` → `SqliteRunStore`, any other
  set value raises (`cairn/core/storage/__init__.py:75`, `:98`, `:101`, `:127`, #1855). The
  branch is not one selector but thirty-four: 33 modules repeat
  `startswith(("postgres://", "postgresql://"))` for their own store (`storage/artifacts.py:1275`,
  `storage/eval_ledger.py:513`, `policy/store.py`, `account.py`, `budget_reservations.py`,
  `dispatch/coordination.py`, …; `deploy_check.py` twice). Postgres connections are opened in
  one place, `shared_pool()` (`cairn/core/storage/postgres/pool.py:51`, 34 callers), plus five
  direct `asyncpg.connect` callers (`connection_probe.py`, `policy/store.py`,
  `storage/respell_packs.py`, `storage/retention.py`, `storage/schedule_store.py`) and the psycopg
  checkpointer (`runtime/checkpointer.py`).
- **Bus.** `bus_backend()` is the one reading of `CAIRN_BUS` (`cairn/core/bus/__init__.py:155`):
  `postgres` for a Postgres DSN, `refused` for `redis://`/`rediss://` (ADR-0127 D5, #2584),
  `inprocess` when unset, `unrecognized` otherwise. `get_bus()` (`:174`) builds `PostgresBus` or
  `InProcessBus`, raises for `refused`, and for `unrecognized` logs a warning and runs
  in-process (`:202-204`). The protocol is `Bus` (`:98`).
- **Runtime.** `select_runtime(name)` (`cairn/core/runtime/__init__.py:30`) returns
  `TemporalRuntime`, `DBOSRuntime` or `InProcessRuntime`; an unknown name falls back to
  in-process (`:33-35`, `:47`). The name comes from `settings.runtime()` — profile
  `durable_runtime` first (an unknown profile value raises, `cairn/core/settings.py:266-303`),
  then `CAIRN_RUNTIME` (`:233-251`). The `Runtime` protocol declares only `invoke`
  (`cairn/core/runtime/base.py:123-145`); everything a durable runtime adds is reached by name:
  `resume` (`dispatch/dispatcher.py:1313-1316`), the reconciler's status, result and
  owner-alive functions (`triggers/server.py:560-596`), the DBOS boot (`:706`), the ADR-0125
  owner stamp (`dispatcher.py:284`), shutdown (`:1585`), the bridge (`pipeline_bridge.py:855`,
  `:1076`, `:1148`), the compiler (`engine/compiler.py:854`, `:1097`), the approval reaper
  (`approval/reaper.py:94`), the CLI (`cli/commands/pipeline.py:345`, `doctor.py:142`, `:704`)
  and `pipeline explain` (`engine/pipeline_explain.py:837`) — sixteen call sites of
  `settings.runtime()`.
- **LLM routing.** `build_chat_model` (`cairn/services/llm/factory.py:97`) is invariant 2's
  chokepoint: it applies the gateway (`:124`), then sets `callbacks` to `BudgetCallback` keyed on
  the *bare* model name (`:130`), and returns `RedactingChatOpenAI` (`:156`). The second LLM path,
  `llm_complete`, redacts messages first (`cairn/core/llm/client.py:123-126`) and applies the
  same gateway (`:150`). The gateway is chosen by `get_gateway()`
  (`cairn/services/llm/gateway.py:93`) from `LLM_GATEWAY` (`provider_settings.llm_gateway`,
  `cairn/core/provider_settings.py:140`): `proxy` → `ProxyGateway`, `direct` → `DirectGateway`,
  anything else logs "falling back to direct" (`gateway.py:112`). The extension point a provider
  needs already exists — `LLMGateway.apply(params, job_tag) -> params` (`gateway.py:23`) — only
  its selection is closed.
- **The precedent that works.** `build_vector_store(name)` builds `sqlite_vec` or `pgvector`,
  then falls through to the `cairn.vector_stores` group (`cairn/services/rag/stores/__init__.py:42`,
  `:67`, `:81-91`); the root `pyproject.toml:146-148` registers `qdrant` and `pinecone` from
  `cairn/providers/vectorstores/`, and no core module imports that package. An unknown name logs a
  warning and falls back to `sqlite_vec` (`:72-78`). Its module docstring still tells a
  contributor to "add an `if name == ...` branch" (`:18-21`).
- **The closest model.** `cairn.dataflow_engines`: core owns selection, packs own construction;
  a factory per name takes `(*, kind, target)`; a selected-but-uninstalled engine raises and
  never falls back (`cairn/core/dataflow/engine_registry.py:14-37`, `:83-130`). Its conformance
  suite is a class in core every adapter subclasses (`DataflowEngineConformance`,
  `cairn/core/dataflow/conformance.py:105`; reference run `tests/dataflow_conformance/`).
- **The groups core already reads.** `cairn.operators` (`operator/loader.py:50`), `cairn.packs`
  (`pack/loader.py:70`), `cairn.routers` (`triggers/router_providers.py:33`), `cairn.run_hooks`
  (`pack/run_hooks.py:68`), `cairn.seeds` (`pack/seeds.py:47`), `cairn.model_catalogs`
  (`pack/model_catalogs.py:32`), `cairn.dataflow_engines` (`dataflow/engine_registry.py:46`),
  `cairn.dataflow_stages` and `cairn.flow_stages` (`dataflow/defs.py:43`, `:46`),
  `cairn.dataset_readers` (`dataset_readers.py:22`), `cairn.vector_stores`
  (`services/rag/stores/__init__.py:87`) and, from the CLI, `cairn.cli`
  (`cli/providers.py:36`). The ADR-0120 D4 groups fail open (a broken entry is skipped with a
  warning, listed by `cairn doctor`); the engine group fails closed. Collisions elsewhere resolve
  toward the more trusted side: an installed pack beats a dev-path pack (`pack/loader.py:18-27`),
  a public CLI command beats a provider's (`cli/providers.py:20-23`).
- **Selection is already declared in one schema.** `[profile.backends]` fields
  (`cairn/core/profile_backends.py:79`) carry `storage` (env `CAIRN_STORAGE`, `env_is_url_kind`),
  `durable_runtime` (`CAIRN_RUNTIME`), `event_bus` (`CAIRN_BUS`), `rag_vector_store`
  (`RAG_VECTOR_STORE`) and `llm_gateway` (profile-only text); precedence is profile → env →
  default (`effective_backends`, `:337`), and `_kind_from_url` already maps an unknown URL to its
  scheme (`:323-334`). `backend_selection.selected()` is the accessor for selector *names*; DSNs
  are deliberately not routed through it (`cairn/core/backend_selection.py:16-24`). Of the five,
  only `durable_runtime` is read from the profile at selection time; `get_bus`, `default_store`,
  `get_gateway` and `get_vector_store` read the environment.
- **Rules this must respect.** Root `CLAUDE.md` §3: core never imports a pack or an operator pack;
  engines and stages are reached only through entry-point groups; core ↔ services only through
  function-local imports. §4 invariants 1–3 (gate, LLM factory, launch-door policy). §5: a new
  entry-point group, registry or abstraction needs an approved decision. ADR-0120 D1/D3: what is
  public and what is the platform's. ADR-0121 D3: distribution names. ADR-0123: the extension
  groups and what stays in core. ADR-0127 D5: core ships only the connectors the example proves;
  `redis://` is refused. ADR-0128 D2: the Qdrant/Pinecone drivers and the Presidio cascade are
  "undecided" because no service-provider group exists (its owner decision 7).

## Decision

**D1 — one group, `cairn.providers`; the entry-point name is `<kind>.<name>`.** A provider is
registered as

```toml
[project.entry-points."cairn.providers"]
"bus.acme"          = "cairn_provider_acme.bus:make_bus"
"llm_gateway.acme"  = "cairn_provider_acme.gateway:make_gateway"
"vector_store.qdrant" = "cairn_provider_qdrant:make_store"
```

`<kind>` is one of a closed set core declares; `<name>` matches `[a-z0-9][a-z0-9_-]*` and is the
value a deployment selects (D4). The separator is the first `.`; neither part may contain
another. A dotted single level is chosen over a colon because the packaging specification
recommends entry-point names match `[\w.-]+`, and over a two-level registration (a group per
kind, or one entry per distribution returning a table) because a flat name lets
`importlib.metadata.entry_points(group=…)` answer "who provides `bus.acme`" without loading any
module, which is what `cairn doctor` and the console's option list need. The kinds in scope:

| Kind | Builtins (core, never registered) | Factory returns | Protocol |
|---|---|---|---|
| `storage` | `postgres`, `sqlite` | a `StorageConnection` (D3) | the Postgres or SQLite dialect core already speaks |
| `bus` | `inprocess`, `postgres` | a `Bus` | `cairn.core.bus.Bus` |
| `runtime` | `inprocess`, `dbos`, `temporal` | a `ProvidedRuntime` (D3, D6) | `cairn.core.runtime.base.Runtime` plus the durable surface |
| `llm_gateway` | `direct`, `proxy` | an `LLMGateway` | `cairn.services.llm.gateway.LLMGateway` |
| `vector_store` | `sqlite_vec`, `pgvector` | a `VectorStore` | `cairn.services.rag.stores.base.VectorStore` |

The kind is `llm_gateway`, not `llm`: what a provider supplies is the routing step, never the
chat model (D3), and the name says so. `vector_store` is **folded in**: one mechanism, one
doctor row, one boundary rule. `cairn.vector_stores` keeps working for one minor release —
`build_vector_store` reads `vector_store.<name>` from `cairn.providers` first, then the old
group, and a name found only in the old group logs one deprecation warning naming the new
spelling; the root `pyproject.toml` registers `qdrant` and `pinecone` under both groups for that
release. Removing the old group is a later change that names the release.

**D2 — resolution: builtins first, then the group; ambiguity and absence refuse.** For a
selected `(kind, name)`:

1. A builtin name is built by core's own branch, as today. If an installed distribution also
   registers `<kind>.<name>` for a builtin name, the builtin wins and core logs one warning per
   process naming the shadowing distribution; `cairn doctor` lists it. This is the rule the pack
   loader and the CLI providers already follow — the shipped, tested side wins a collision — and
   it means installing a wheel can never silently replace the Postgres store or the redaction
   path's gateway.
2. Otherwise the one `cairn.providers` entry named `<kind>.<name>` is loaded and called. One
   distribution seen twice on `sys.path` with the same value is one provider (as
   `cli/providers.py:71-74` dedupes); two different values for the same name **refuse** — core
   cannot know which one the deployment meant.
3. No builtin and no provider → **refuse at selection**, naming the builtins and the installed
   providers of that kind, the knob that selected it, and that no fallback happened. A provider
   that is selected and fails to import or construct refuses the same way. This is the #568 /
   #1855 / #2584 discipline (`engine_registry.py:34-37`): a deployment that asked for a backend
   must not silently get another one.

Only the *selected* entry is ever loaded; a broken provider of an unselected name costs nothing
and is reported by `cairn doctor` (which loads each entry to report it) — the fail-open
behaviour of the ADR-0120 D4 groups is kept for listing and dropped for selection. Rule 3
changes four existing fall-backs (bus `unrecognized`, runtime unknown name, `LLM_GATEWAY`
unknown, `RAG_VECTOR_STORE` unknown); that is owner decision 2.

**D3 — the per-kind factory contract.** Every factory is a callable taking keyword arguments
only, so a kind can gain an input without breaking providers. Each kind's signature, output and
obligations are declared in one core module (D5) as a `Protocol` and a conformance class:

- **`storage`** — `factory(*, url: str) -> StorageConnection`, where `StorageConnection` names a
  **dialect core already implements** (`"postgres"` or `"sqlite"`), the DSN or path to use, and
  optional connection options (for Postgres: the keyword arguments `shared_pool()` passes to
  `asyncpg.create_pool`, such as a password callable for short-lived IAM tokens or an SSL
  context). A storage provider is therefore a *managed* Postgres or SQLite (Aurora/Cloud SQL/Neon
  with IAM auth, a hosted libSQL), never a new store family: the 33 modules keep their own
  Postgres and SQLite twins and branch on the dialect instead of the URL prefix. A non-SQL record
  store would need a store family per protocol (`RunStore`, `PipelineStore`, the policy, budget
  and account stores, …) and is out of scope until the single-substrate work (ADR-0083 R4,
  candidate C-7 in `docs/adr/README.md`) gives core one family to implement.
- **`bus`** — `factory(*, url: str) -> Bus`. The returned object implements `publish`,
  `subscribe` and `replay` (`replay` may raise `NotImplementedError` and must declare so, as
  `InProcessBus` does). Construction must not open a connection; the first publish may (the
  `PostgresBus` rule, `bus/__init__.py:177-179`).
- **`runtime`** — `factory() -> ProvidedRuntime`. `ProvidedRuntime` is `Runtime` (`invoke`) plus
  the surface core reaches by name today, declared once: `durable: bool`, `resume(run_id,
  decision)`, `workflow_terminal_status(run_id)`, optional `workflow_result(run_id)` and
  `workflow_is_running(pipeline_id)`, and a `boot(store)` hook called where the DBOS boot runs
  now. D6 says what `durable` obliges.
- **`llm_gateway`** — `factory(*, settings) -> LLMGateway`, where `settings` is
  `provider_settings.get_settings()`. The provider supplies routing (`base_url`, keys, headers,
  a model prefix) through `apply(params, job_tag) -> params`. It never returns a chat model and
  never wraps one: `build_chat_model` still builds `RedactingChatOpenAI`, sets `callbacks` after
  `apply` (so a provider cannot drop `BudgetCallback`), and prices on the bare model name;
  `llm_complete` still redacts before `apply`. Core refuses a gateway whose `apply` returns
  `callbacks` (the conformance suite checks it).
- **`vector_store`** — `factory() -> VectorStore` (the zero-argument class the old group already
  registers, so the migration is a re-registration, not a rewrite).

**Conformance.** Each kind ships a conformance class in core that a provider's own tests
subclass, as engine adapters subclass `DataflowEngineConformance`: `BusConformance` from the
cases in `tests/core/test_bus.py` and `test_bus_postgres.py` (topic and wildcard dispatch,
unsubscribe, at-most-once on a raising handler, replay or its declared absence, DSN case
preserved); `StorageConnectionConformance` running the run-store and pipeline-store twin cases
(`tests/core/test_run_store.py`, `test_postgres_run_store.py`, `test_pipeline_store.py`) and the
ADR-0125 owner-lock probe against the provider's connection; `RuntimeConformance` from the DBOS
scenarios (`tests/core/_dbos_scenarios.py`: run, approve-and-resume, crash and recover) plus the
ADR-0125 checks in D6; `LLMGatewayConformance` (params in, params out, no `callbacks`, no message
content read or written, idempotent on a second apply); `VectorStoreConformance` from
`tests/services/test_pgvector_store.py`. Each builtin passes its own kind's suite in core's CI —
that is what makes the suite the definition and not a description.

**D4 — how a provider is selected: by name, in the place that selects that kind today.** No new
environment variable and no new profile key; every row of the settings inventory stays.

| Kind | Selected by | Rule |
|---|---|---|
| `storage` | `CAIRN_STORAGE=<scheme>://…` | `postgres`/`postgresql`/`sqlite` are builtins; any other scheme selects `storage.<scheme>` and the whole URL is passed to it. `[profile.backends] storage` keeps showing the derived kind (`_kind_from_url` already yields the scheme). |
| `bus` | `CAIRN_BUS=<scheme>://…` or `CAIRN_BUS=<name>` | `postgres://` and `inprocess`/unset are builtins; another scheme or bare name selects `bus.<scheme or name>`. `redis`/`rediss` refuse unless a `bus.redis` provider is installed (owner decision 3). `event_bus` in the profile stays descriptive, as its field text says. |
| `runtime` | `[profile.backends] durable_runtime`, then `CAIRN_RUNTIME` | the vocabulary `settings.profile_runtime` accepts gains the installed `runtime.*` names; the precedence of #1852 is unchanged. |
| `llm_gateway` | `LLM_GATEWAY=<name>` | `direct` and `proxy` are builtins; another name selects `llm_gateway.<name>`. The profile's `llm_gateway` text field (an endpoint or connection ref) is not a selector and is not wired by this record. |
| `vector_store` | `[profile.backends] rag_vector_store`, then `RAG_VECTOR_STORE` | as today; the name selects `vector_store.<name>`. |

The console's backend form (`profile_backends.BACKENDS`) lists, for each `select` field, the
builtin options plus the installed providers of that kind, read from entry-point names without
loading them. `cairn doctor` and `cairn pipeline explain` report, per kind, the selected name,
whether it is builtin or provided, and the providing distribution and version. Which provider
served a run is deployment state, not a run field: no `Run`, `PipelineRecord` or OpenAPI change.

**D5 — boundaries.** One new core module, `cairn/core/providers.py`, owns the group: the kind
table, `provider_factory(kind, name)`, `installed_providers(kind)`, the refusal error, and the
per-kind `Protocol`s and conformance classes of D3 (the LLM gateway protocol stays in
`cairn.services.llm.gateway` and is reached function-locally, per §3). It is the only reader of
`cairn.providers`. Core reads the group and never imports a provider package. A provider depends
on `cairndev` and the public contract modules only: `cairn.core.providers`, its kind's protocol
module (`cairn.core.bus`, `cairn.core.runtime.base`, `cairn.services.llm.gateway`,
`cairn.services.rag.stores.base`) and `cairn.core.settings` — never dispatch, engine, triggers,
another provider or an operator pack. Provider distributions are named `cairn-provider-<name>`
with import name `cairn_provider_<name>` (owner decision 4). The platform ships its paid
providers as such wheels; a public provider not proven by the hello-cairn example goes to
`cairn-extensions` under ADR-0128 D1's placement rule; the public core gains no private code and
no dependency on a provider wheel. Tests that hold it:

- `tests/core/test_architecture_boundaries.py` gains `R_PROVIDER`: no module under
  `cairn/core/`, `cairn/cli/`, `cairn/services/` or `cairn/operators/` imports `cairn_provider_*`
  or the in-tree `cairn.providers` package (empty allowlist — true today); and
  `R_PROVIDER_CORE`: a module under `cairn/providers/` imports from `cairn.*` only the surface
  above, with today's one excess edge recorded as debt
  (`cairn/providers/vectorstores/qdrant.py` → `cairn.core.provider_settings`). Each gets a
  rejecting case in `test_boundary_rule_can_fail`.
- `tests/core/test_public_core_boundary.py` gains: no base dependency or public extra of the root
  `pyproject.toml` names a `cairn-provider-*` distribution, with a rejecting case.
- A new `tests/core/test_providers.py` proves the resolver: builtin wins and warns, two values
  refuse, absent refuses, only the selected entry is loaded, the old vector-store group still
  resolves with its warning.

**D6 — what does not change.** The chokepoints stay in core and wrap whatever a provider builds:
operator invocations still pass `enforce_gate` and flow legs `enforce_flow_gate` (invariant 1);
every LangChain model is still built by `build_chat_model` (invariant 2, D3); policy gates still
fire at the launch door before any record exists (invariant 3); budget reservation, redaction,
audit and the run record are untouched; artifacts are still registered control-plane-side
(invariant 5). A provider can change *where* bytes go, never *whether* a gate, a policy, a budget
or an audit event applies.

A provided runtime gets ADR-0125's durability guarantees only by passing `RuntimeConformance`,
and what it must implement depends on `durable`:

- `durable = False` — the leg runs in this process: core stamps `Run.owner` with this process's
  owner id and the in-process liveness function governs restart, cancel and the reconciler
  (ADR-0125 D1–D3), exactly as for `inprocess`. The provider implements `invoke` and
  `resume` from the LangGraph checkpointer.
- `durable = True` — core stamps no owner (`dispatcher.py:284` today, for Temporal and DBOS) and
  the provider's own liveness takes precedence, so the provider must implement
  `workflow_terminal_status` (the reconciler's `status_fn`), `resume`, and — if a workflow can
  finish with nobody awaiting it, as under DBOS (#1847, #1869) — `workflow_result`; and it must
  pass the crash-and-recover scenario (the leg reaches a terminal record after the owning process
  is killed) and the approve-after-restart scenario. A runtime that cannot pass them registers
  with `durable = False` or not at all.

The sixteen `settings.runtime()` call sites become questions to the selected runtime
(`rt.durable`, `rt.resume`, …) before `runtime.*` providers are honoured; until that slice lands,
selecting a provided runtime refuses (D2 rule 3), so no provider ever runs under name checks that
do not know it.

**D7 — relation to ADR-0128.** Providers are the mechanism by which the extensions repository and
the platform register *non-operator* backends; operator packs keep `cairn.operators`, engines
`cairn.dataflow_engines`, stages the stage groups. ADR-0128 D2's "undecided — no service-provider
entry-point group" rows resolve to **provider wheels**: the Qdrant driver becomes
`cairn-provider-qdrant` (kind `vector_store`), a public extension under ADR-0128 D1 because the
example does not prove it; the Pinecone stub, which raises `NotImplementedError` on every method
(`cairn/providers/vectorstores/pinecone.py`), is deleted rather than moved (owner decision 6).
The Presidio cascade is not a backend of any kind above — it is a redaction implementation
behind `redact.text`, which ADR-0028 already makes a profile-bound service-provider *operator* —
so it is outside this record and its row stays ADR-0128's to decide. The `rag` extra's readers are
likewise not a provider kind.

## Consequences

- A paid or private backend of the five kinds is a wheel the deployment installs and selects by
  name; core is not edited, and the public core never sees the code.
- One more entry-point group (thirteen with `cairn.cli`), one more core module and five
  conformance classes. `cairn.vector_stores` is deprecated, not removed.
- Four fail-open selections become refusals (owner decision 2). A deployment with a typo in
  `CAIRN_BUS`, `CAIRN_RUNTIME`, `LLM_GATEWAY` or `RAG_VECTOR_STORE` stops starting instead of
  running on a default it did not ask for; `tests/services/test_vector_store_default.py`'s
  fallback assertion is rewritten to the refusal.
- The storage dialect seam replaces 34 URL-prefix tests with one resolver, which also removes a
  class of drift between stores (one store recognising a spelling another does not).
- Runtime providers wait for the runtime-traits slice; until then the kind is declared and
  refuses.
- `cairn lock` (`cli/commands/lock.py:132`) pins `cairn.packs` and `cairn.operators`
  distributions; provider distributions join it so a deployment's backend wheels are pinned like
  its packs (owner decision 5).
- What does not change: `Operation`, `pack.toml`, `PipelineDef`, `DataflowSpec`, `Run`,
  `console/openapi.json`; the env and profile names; the chokepoints (D6); ADR-0127's gate for the
  flip (the public core is the same with or without a provider installed).

## Alternatives considered

- **Keep the hardcoded selectors.** Every new backend is a core change and a core release; a paid
  backend cannot exist without putting private code in the public core or forking it. Rejected:
  it is the problem.
- **One group per kind** (`cairn.buses`, `cairn.runtimes`, `cairn.storage_backends`,
  `cairn.llm_gateways`, with `cairn.vector_stores` as the template). Works, and is what the
  vector stores did. Rejected: four §5 decisions instead of one, four loaders with four copies of
  the builtin-wins and refusal rules, four doctor rows, four boundary predicates — and the rules
  would drift the way the four fall-backs above already differ from the engine group.
- **A plugin registry module** (providers call `register_provider(kind, name, factory)` at import,
  core imports a known list or scans `sys.modules`). Rejected: registration then depends on import
  order and on something importing the provider, which is either core naming it (forbidden by
  §3) or an entry point anyway; and the registry is process-global mutable state §5 asks to
  avoid.
- **Providers as service-provider operators** bound in `[profile.bindings]` (the ADR-0028 shape
  for redaction and audit). Rejected for these kinds: a store, a bus, a runtime or a gateway is
  process infrastructure built before any operator runs, and routing every publish or query
  through a governed operator invocation would put `enforce_gate` on the hot path of the record
  itself. Redaction and audit stay operators.
- **Two-level registration** (one `cairn.providers` entry per distribution returning a table of
  kinds and names). Rejected: core must load every provider module to learn what exists, so a
  broken unselected provider breaks discovery.

## Verification

Once built:

- `tests/core/test_providers.py`: a fake distribution (a monkeypatched entry-point list, as
  `tests/core/test_engine_registry.py:75` patches the engine registry) registering `bus.acme` is selected by
  `CAIRN_BUS=acme://x` and receives the URL; `bus.postgres` from a fake distribution is ignored
  with one warning; two values for `bus.acme` refuse; `CAIRN_BUS=nope://` refuses naming the
  builtins and installed providers; only the selected entry's module is imported.
- Each conformance class passes for every builtin of its kind in core's CI, and for the reference
  fake provider in `tests/core/test_providers.py`.
- `tests/core/test_architecture_boundaries.py` holds `R_PROVIDER` and `R_PROVIDER_CORE` on the
  tree and rejects their planted edges; `test_public_core_boundary.py` rejects a root extra that
  names a `cairn-provider-*` wheel.
- `build_chat_model` with a fake `llm_gateway.acme` returns `RedactingChatOpenAI` whose callbacks
  start with `BudgetCallback`, and a gateway that returns `callbacks` is refused.
- `cairn doctor` lists every kind's selection with builtin/provided and distribution; `cairn
  pipeline explain` reports the same for the leg's runtime, storage and bus.
- `cairn-provider-qdrant` installed into a clean `cairndev` venv: `RAG_VECTOR_STORE=qdrant`
  builds `QdrantStore` with no `cairn.vector_stores` entry present, and
  `scripts/check_public_set.py` refuses that venv for the public job.

## Implementation sketch

Three slices, each its own issue and PR; the first is the reviewable unit this record asks to be
accepted for.

1. **Group, resolver, and the three kinds whose seam exists** (about one day plus review).
   `cairn/core/providers.py` (kind table, resolver, refusal error, protocols, conformance
   classes); `cairn/core/bus/__init__.py` (`bus_backend` gains `provided`, `get_bus` consults the
   resolver, `unrecognized` refuses); `cairn/services/llm/gateway.py` (`get_gateway` consults the
   resolver, unknown refuses); `cairn/services/rag/stores/__init__.py` (both groups, deprecation
   warning, unknown refuses, docstring fixed); `pyproject.toml` (qdrant/pinecone under both
   groups); `cairn/core/profile_backends.py` (options include installed providers);
   `cairn/cli/commands/doctor.py`, `cairn/core/engine/pipeline_explain.py` (report rows); tests:
   `test_providers.py`, the two boundary rules, the vector-store default test, the bus and gateway
   tests for the new refusals. Docs: `docs/architecture/02-dependency-model.md`,
   `docs/contracts/operators-and-packs.md` (the group list), `docs/contracts/README.md`.
2. **Storage dialect seam** (one and a half to two days). One resolver
   `storage_connection(url) -> StorageConnection` in `cairn/core/storage/`, `shared_pool()` taking
   its connection options, the 33 prefix tests, the five direct connects and the psycopg
   checkpointer switched to the dialect; `StorageConnectionConformance` run for sqlite and postgres.
3. **Runtime traits** (about two days). `ProvidedRuntime` on the three builtin runtimes; the
   sixteen `settings.runtime()` sites rewritten to ask the selected runtime; then `runtime.*`
   providers are honoured; `RuntimeConformance` from `_dbos_scenarios.py` run for in-process and
   DBOS.

The Qdrant move to `cairn-extensions` follows slice 1 and ADR-0128's acceptance.

## Documentation / implementation conflicts found while writing

Reported, not resolved here:

- `cairn pipeline explain` says an unrecognised `CAIRN_STORAGE` scheme "falls back to
  CAIRN_RUN_DB / in-memory" (`cairn/core/engine/pipeline_explain.py:829`); `default_store()`
  raises for it since #1855 (`cairn/core/storage/__init__.py:127`). The explain text is wrong.
- ADR-0128 D2 places the Qdrant driver at `cairn/core/storage/rag.py` and says there is no group to
  move it through; the driver is `cairn/providers/vectorstores/qdrant.py`, already reached only
  through `cairn.vector_stores` (`pyproject.toml:146-148`). What is missing is a group for the
  other kinds, which D1 adds.
- `cairn/services/rag/stores/__init__.py:18-21` tells a contributor to add an `if name == ...`
  branch; the code resolves external stores from the entry-point group (`:62-70`).
- `CAIRN_BACKEND` is the observability telemetry backend (`mock`/`otel`/`clickstack`,
  `cairn/core/settings.py:667`, `cairn/core/provider_settings.py:87`), not an LLM selector; LLM
  routing is `LLM_GATEWAY` alone.

## Decisions the owner takes on acceptance

**Accepted 2026-10-15: all seven recommendations below, as written** — `<kind>.<name>` in one
group; every unknown selection refuses (slice 1, with a release note); `redis://` refused only
while no `bus.redis` provider is installed; `cairn-provider-<name>` distributions; provider wheels
pinned in `cairn.lock`; the Pinecone stub deleted; slice 1 = bus, LLM gateway, vector store.


1. **Entry-point name shape.** Recommendation: `<kind>.<name>` in one `cairn.providers` group
   (D1). Alternative: `<kind>:<name>` (allowed by the specification but outside its recommended
   character set) or a group per kind.
2. **Fail closed on every unknown selection** (bus `unrecognized`, runtime unknown, `LLM_GATEWAY`
   unknown, `RAG_VECTOR_STORE` unknown — all four fall back today). Recommendation: refuse, as
   storage (#1855), the engine group and `redis://` (#2584) already do; ship it in slice 1 with a
   release note. Alternative: refuse only names that look like a provider (a scheme) and keep the
   warnings for the rest.
3. **`redis://` with a provider installed.** Recommendation: refused only while no `bus.redis`
   provider is installed — ADR-0127 D5 decided what the public core ships, not what a deployment
   may install. Alternative: keep `redis` refused unconditionally and have a Redis provider pick
   another scheme.
4. **Provider distribution names.** Recommendation: `cairn-provider-<name>`, import
   `cairn_provider_<name>`, `publisher = "cairn"` for first-party ones, extending ADR-0121 D3.
   Alternative: reuse `cairn-operator-*` (wrong: a provider is not an operator and has no
   `pack.toml`).
5. **Pin provider wheels in `cairn.lock`.** Recommendation: yes, in slice 1, so a deployment's
   backends are pinned like its packs (ADR-0017). Alternative: leave locking to the deployment's
   own requirements file.
6. **The Pinecone stub.** Recommendation: delete it and its `rag-pinecone` extra and console
   option; it implements nothing. Alternative: move it with Qdrant as a stub.
7. **Scope of slice 1.** Recommendation: bus, LLM gateway and vector store only, with `storage`
   and `runtime` declared and refusing until slices 2 and 3. Alternative: hold the whole change
   until all five kinds are honoured.
