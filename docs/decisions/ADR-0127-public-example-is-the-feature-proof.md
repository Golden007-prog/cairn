# ADR-0127 — The public example is the feature proof: four pipelines, a trimmed connector surface, DBOS as the public durable runtime

- **Status:** ACCEPTED (owner decisions of 2026-10-14, taken in four rounds: the proof matrix and
  training model; Infisical, Temporal and LLM scope; NATS, contracts, the artifact store and
  lineage; the cuts and the connector policy)
- **Date:** 2026-10-14
- **Issue:** #2572 (the hello-cairn feature-proof epic); gates the public flip
  #2430. **Amends** ADR-0120 D3/D5/D8 (what a domain pack may carry; the boundary test),
  ADR-0123 D6 (the builtin set). Keeps ADR-0121 (repositories, names), ADR-0124 and ADR-0125
  unchanged.

## Context

After the 2026-10-13/14 verification passes the public core (`cairndev`, twelve builtin operator
packs, five contracts) is proven for data pipelines with gates on the in-process runtime, on SQLite
and Postgres, with Docker and the light stack. Nothing else the public tree claims has been run
from it: no agentic template ships, no ML flow, no MCP call, no SSH job, no hook, no watch, and
the Temporal runtime cannot run task legs (#1378). The owner's bar for the flip is that every
feature the core advertises is exercised end to end by an example a new user can run from the
CLI, and that every builtin operator implements a contract.

Verified on `origin/main` 7a3f3ba0: trigger kinds are `manual · schedule · webhook · watch`
(`cairn/core/engine/triggers_grammar.py:97`); run hooks are the `cairn.run_hooks` entry-point group
(`cairn/core/pack/run_hooks.py:68-77`); the event bus has in-process and Postgres backends only
(`cairn/core/bus/__init__.py:153-190`); the builtin dataflow pack ships the `builtin`, `prefect`,
`metaflow` and `skypilot` engine adapters and two stages; eight of the twelve builtins have no
contract; `examples/hello-cairn` has one data pipeline and seven unit tests.

## Decision

**D1 — the public example is the feature proof, and the proof is the gate.** `examples/hello-cairn`
grows to four pipelines, each with one reason to exist for a new user: `profile-and-publish`
(data: profile, data gate, redaction, builtin-engine flow, artifact store, person gate, promote,
audit; `watch`, `schedule` and `webhook` triggers; emits an event), `ask-the-docs` (agentic: RAG
index and query on sqlite-vec or pgvector, a template whose nodes call a model through core and a
tool through `mcp/call` against a stdio server the pack ships, memory, a threshold gate; starts on
the first pipeline's event), `tiny-finetune` (ml: a real LoRA fine-tune on the builtin ml engine,
loss gate, promote, person gate; optional SSH execution) and `nightly-ops` (flow: secrets, an SSH
job to a local sshd, a summarising flow, propose, audit; a cron schedule, a `table` watch, a
run-hook listener). An end-to-end matrix over storage {sqlite, postgres} × runtime {inprocess,
dbos} × bus {inprocess, postgres} × the four pipelines, plus crash cells asserting the ADR-0125
states, runs as a `public-e2e` job on the public repository. This extends ADR-0120 D8: the boundary
test proves installability; the feature proof proves behaviour.

**D2 — a domain pack carries data and glue, never operators.** ADR-0120 D3/D5 described domain
packs as data. hello-cairn additionally ships two pieces of glue that only make sense next to its
pipelines: a `cairn.run_hooks` listener (`hooks.py`) and a stdio MCP tool-server script
(`tools/calc_server.py`). Both are declared through entry points or named by the pipeline; neither
registers an operation. Operators stay in operator packs.

**D3 — `secrets-infisical` is a builtin.** The pack moves from the official tier to
`cairn/operators/builtins/secrets_infisical` as `cairn-operator-secrets-infisical`, its SDK behind
the optional `[infisical]` extra, bound through the profile capability `secrets.store` beside
`secrets-env` (errata 2026-10-14: the capability is `secrets.store`; `secrets.get` is the contract
name proposed in D8). The engine and platform images install the extra. The light stack gains an
off-by-default `infisical` compose profile. Thirteen builtins (amends ADR-0123 D6).

**D4 — a `train-local` builtin with the one contract this record adds.** A new public pack
`cairn-operator-train-local` provides an ml flow stage `train/lora` and an operation
`train/eval-loss`, implementing a new public contract `train.finetune/1.0.0` (inputs: dataset ref,
base model, LoRA rank, steps, max samples; outputs: adapter path, train and eval loss, token
counts). Its dependencies (`torch`, `transformers`, `peft`, `datasets`) sit behind a `[train]`
extra; CPU only. The default model is `Qwen/Qwen2.5-0.5B` with a handful of steps; CI runs the
`sshleifer/tiny-gpt2` stand-in in seconds. Fourteen builtins. The private training pack may later
implement the same contract.

**D5 — the public core ships only the connectors the example proves.** Core keeps MCP over stdio
and HTTP, env and Infisical secrets, SSH remote execution, sqlite-vec and pgvector, SQLite and
Postgres stores and buses, the builtin dataflow engine, and the in-process and DBOS runtimes. The
`prefect`, `metaflow` and `skypilot` engine adapters leave the builtin dataflow pack for
`cairndev-sh/cairn-extensions`; the platform depends on them from there. The Modal and Kaggle job
providers stay platform. The `redis://` bus branch, which only logs "not implemented", is removed.
MCP presets in core are the pair the example uses. Until the extensions ADR (#2535) lands, the
placement rule is: public but not proven by the example → extensions.

**D6 — DBOS is the public durable runtime; the Temporal task-leg fix and a NATS bus are outside
the gate.** The proof runs every pipeline on in-process and DBOS. Temporal stays installable
(`runtime-temporal`) and documented as running flow legs only until #1378 is fixed; a NATS event
bus stays #969 (the remote-job channel keeps using NATS). Neither is a condition of the flip.

**D7 — Ask and lineage records stay platform-only.** ADR-0120 D3 is unchanged: `cairn-ask` and the
lineage operator are platform packs. The public CLI hides the `ask` verb when the package is
absent, and `cairn lineage` states that lineage records are written by a lineage node this install
does not have, pointing at the run-graph endpoint.

**D8 — builtin contracts come from the contract track.** The eight builtins without a contract
(`artifact_local`, `data_profile`, `memory_local`, `rag_local`, `rag_rerank`, `redact_regex`,
`remote`, `secrets_env`) are migrated under #2243 by that track; this record proposes the names
`artifact.store`, `data.profile`, `memory.store`, `rag.index`, `rag.query`, `rag.rerank`,
`redact.text`, `remote.run`, `secrets.get` and binds nothing to them. The example references
whatever that track lands.

## Verification (verify-upstream, 2026-10-14)

- LoRA fine-tuning on CPU with `peft` and `transformers` on a 0.5B model is feasible in minutes;
  a tiny GPT-2 stand-in trains in seconds. No GPU in the public proof.
- Infisical publishes a self-hostable compose stack; its SDK is `infisicalsdk`.
- `fastmcp` serves a stdio tool server in a few lines; the MCP operator already speaks stdio.
- DBOS already recovers template runs at boot in this codebase; nothing is rebuilt.

## Consequences

- Fourteen builtins and six public contracts (five plus `train.finetune`) in the assembler's
  lists; the carve, the leak scan and `scripts/check_public_set.py` follow.
- A `cairndev[hello]` meta-extra pulls the service extras the example needs (`rag-sqlite-vec`,
  `ssh`, `runtime-dbos`); `[train]` is separate.
- The platform's pyproject gains the extensions dependency for the engine adapters it uses.
- Docs: a golden path per pipeline, the capability catalog rows, `07-runtime.md` (DBOS public,
  Temporal flow-legs-only), the light stack's `proof` profile README.
- What this does not change: the public API, the pack manifest contract, the `Operation` contract.

## Alternatives rejected

- **Fix Temporal task legs before the flip.** Three days and three matrix cells for a runtime the
  public example does not need; DBOS covers durability.
- **Build the NATS bus now.** New backend, new matrix dimension; the Postgres bus already proves
  events.
- **More connectors in core.** Every connector in core is a matrix cell to keep green; the
  extensions repository exists for the rest.
- **Qwen in the default path.** Two gigabytes of dependencies and minutes of CPU for every new
  user; it stays behind the extra.
- **Infisical platform-only.** One day of work gives a public user a real secrets manager.

## Errata (2026-10-15)

Owner decisions of 2026-10-15, taken while implementing D5 (WP4) and D4 (WP3). The D5 text above is unchanged;
these notes govern where they differ.

- **D5, engine adapters — deferred.** The `prefect`, `metaflow` and `skypilot` adapters import the
  builtin dataflow pack's runner (`run_dataflow`), stage registry, result-file readers and SSH
  fetch helpers, and an official → builtin pack import is forbidden
  (`tests/core/test_architecture_boundaries.py`, "operator pack → another operator pack"). The
  move waits for #2583 (promote that engine substrate into `cairn.core.dataflow`) and then the
  extensions ADR (#2535). Until then the public dataflow pack ships the three adapters behind
  their optional extras (`[prefect]`, `[metaflow]`, `[skypilot]`).
- **D5, MCP presets — the sentence was wrong.** Stdio and streamable HTTP are MCP *client
  transports*; both stay in core. The presets (`github`, `slack`, `notion` under
  `cairn/connectors/`) are named server specs with consumers — the core Slack notifier, the
  platform packs' Notion publish operations, the `cairn connect --preset` tests — and all three
  stay in core.
- **D5, `redis://` bus branch — stands.** Removed in WP4: `CAIRN_BUS=redis://…` is refused at
  selection, naming the two supported backends, and the console no longer offers it.
- **D2, D4 — the example's profiles; the stage's name (WP6).** D2's list of what the example
  ships beyond data and its two pieces of glue gains the pack's own deployment profiles,
  `profiles/hello.toml` and `profiles/hello-infisical.toml`, shipped as data (accepted by the
  maintainer 2026-10-15): the capability-bound secret check and the declared MCP server need
  a profile that binds them, and `cairn/profiles/local-dev.toml` stays unchanged. D4's ml flow
  stage is `train.lora` (dotted), as the `cairn.dataflow_stages` entry-point group spells it;
  the operation keeps the slash form, `train/lora`.
- **D4, `train/lora` — an operation and a stage, one implementation.** `train/lora` ships as an
  operation (`cairn.operators`) and as the ml flow stage `train.lora`
  (`cairn.dataflow_stages`); both call `operator_train_local.finetune.finetune`, so the two paths
  cannot drift apart.
- **D4, the stage carries no `implements`.** `train.finetune` is a capability contract (input,
  configuration, output); the launch door resolves an opted-in stage only against a
  `cairn.stage/1` stage contract and refuses one with none installed
  (`cairn/core/dispatch/pipeline_launch.py:297-310`). Parity between the two paths rests on the
  stage validating its config with the operation's input model and its stats with the
  operation's output model (unknown config keys refused).
- **D4, "the one contract this record adds" means one distribution.** `train/eval-loss`
  implements a second capability, `train.eval_loss/1.0.0`, inside the same
  `cairn-contracts-train` distribution, because every builtin operator implements a contract
  (`tests/core/test_builtins_complete.py`). Both capabilities carry a report definition equal to
  their operator's `reports`. The Consequences count "six public contracts (five plus
  `train.finetune`)" was wrong when written: the assembler's `CONTRACTS` already listed twelve
  distributions; with `cairn-contracts-train` it lists thirteen (fourteen builtins unchanged).
