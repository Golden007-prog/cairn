# examples/ — kitchen-sink reference bundle

## Start here: the `hello-cairn` example pack

[`hello-cairn/`](hello-cairn/README.md) is a small domain pack that runs on the engine
and its builtin operator packs alone (no other pack, no ML dependency, no API key). Its
pipeline profiles a sample dataset, stops at a **data gate**, runs a flow leg, stages a
summary, pauses at a **person gate**, and publishes only after approval, under a declared
**budget**. Load it with `CAIRN_PACK_PATH=examples/hello-cairn`, or install it as the
`cairn-pack-hello` wheel. Its README is the quickstart walkthrough. Tests:
`tests/examples/test_hello_cairn.py`.

The rest of this file describes the older template reference bundle.

A single, **verified** reference set showing the full surface area of every
artifact type in templates_v2. These files are **not loaded by the default
engine** (the loaders read `templates/builtin`, `skills/builtin`,
`policies/builtin`). They live here as a copy-paste reference and as the
fixture for `tests/templates_v2/test_examples_bundle.py`, which proves they
parse and compile against the real engine.

## Why a bundle, not "one template"?

The schema deliberately splits the surface area across **three artifact types +
runtime config**. You cannot put everything in one template:

| Capability | Lives in | Field |
|---|---|---|
| tools | template | `tools.required / dynamic / tool_k` |
| graph (all 6 BT nodes) | template | `flow: state_machine` |
| multi-model params | template | `models.router/fast/strong` (+ extras) |
| compose other templates | template | `subgraph_node` |
| structured output | template | `output.schema` |
| evals | template | `evals` |
| **budget** | template | `budget` (added for this bundle) |
| memory | skill | `memory_enabled` |
| triggers / scheduled | skill | `triggers.{patterns,metrics,alerts,schedule,manual}` |
| skill chaining (cascade) | skill | `chain_skills` |
| python escape hatch | skill | `custom_logic` |
| action proposals | skill | `action_candidates` |
| approval / deny rules | **policy** | `PolicySpec.rules` (4 effects) |
| background lane | **runtime** | chosen at dispatch — not a YAML field |

## Files

| File | Demonstrates |
|---|---|

## Two honest caveats

1. **`subgraph_node` resolution is hardwired** to `templates/builtin` /
   `templates` (see `compiler._compile_bt` → `resolve_template_path`). So the
   example composes the real builtin **`generic-investigation`**. The YAML is
   identical for any target — this is a genuine demonstration of composition.

2. **`budget` enforcement is partial, by design.** Only `max_wall_seconds` is
   enforced today (the Runner wraps the run in `asyncio.wait_for`). `max_cost_usd`
   and `max_tokens` are **declared intent, not enforced** — there is no
   cost/token accounting subsystem yet (`InvestigationResult.llm_cost_usd` is
   still a dead default). Nothing fakes enforcement; the fields exist so the
   accounting layer can honor them later.

## What a registry would include (beyond template + skills + python + evals)

The full inventory the registry must resolve:

- **Artifacts:** templates, skills, **policies**, **output schemas**
- **Code (3 separate registries):** `condition_registry` (guards),
  `python_function_registry` (python_node), `custom_logic` modules (skill hooks)
- **Evals:** datasets + metrics + thresholds
- **Integration bindings** (referenced by name, must resolve): **executors**
  (`action_candidate.type` → write-saga handler), **approval channels /
  notifiers** (e.g. `slack:#prod-oncall`), **backend adapters + capability→tool
  bindings**
- **Provenance:** version, content hash (`TemplateRef.hash/tag`), trust tier,
  and a **dependency manifest** so `validate` can fail fast on a missing
  capability/executor/schema before runtime.

## Verify

```bash
OPENAI_API_KEY=sk-test-dummy uv run pytest tests/templates_v2/test_examples_bundle.py -q
```
