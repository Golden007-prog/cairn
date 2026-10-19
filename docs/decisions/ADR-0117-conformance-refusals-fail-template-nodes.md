# ADR-0117 — A shared-contract refusal fails a template operation node

**Status:** accepted (maintainer, 2026-10-06, #2241 slice C plan D1/D3) — a scoped amendment
of the compiler's degrade-to-finding policy (#568) under ADR-0084 D8 (the compiler is amended,
never replaced).

## Context
The template operation node (`engine/compiler.py`) turns any exception from an in-process
operator into `{"last_action_status": "failure", "findings": {...}}` and lets the graph
continue; only operators declaring `remote_job`/`docker` re-raise (#568). #2241 B put the
shared-contract checks at the one invocation seam; on a template node a refusal would have
become a `done` run with a finding — a false success for exactly the class of failure the
seam exists to make loud, and a divergence from task legs, which fail.

## Decision
1. `ContractConformanceError` (identity drift, input/configuration refusal before the call,
   output refusal after it) re-raises from the operation node and fails the run, whatever the
   operator's executors. Every other exception keeps #568's behaviour byte-for-byte.
2. Inside an admitted leg (graph state stamped `contract_invocations.__leg__` by the
   dispatcher from the pipeline record), an opted-in operator whose node has no recorded entry
   refuses with the same error: no envelope there is lost provenance, not legacy. An
   unstamped graph (the dev loop, ADR-0106 D6 — no record) is legacy.
3. The channel propagates into subgraph seeds re-keyed under the subgraph node (nested,
   embedded, durable and Map-body arms), so a child's opted-in nodes are governed as the
   parent's are. A flattened gated child (#569/#581) keeps its nodes' bare refs in the parent
   graph, so its compile factory carries `<child template>/` and each node looks up exactly
   the key the launch door recorded — no suffix matching, so a legacy node beside a
   same-named opted-in child node is untouched.

## Consequences
A template leg and a task leg now fail on the same contract refusal; the compiler gains one
`except` arm, two small helpers, a `contract_prefix` on its node factory and the prefixed
lookup key, nothing else. Operators without `implements` are untouched.
Evidence: `tests/core/test_engine_compiler_operation.py` (refusal fails the run; stamped-but-
missing refuses; legacy unchanged; subgraph propagation).
