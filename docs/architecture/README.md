# Architecture — the front door

**DOCUMENTED migration scope (2026-09-24):** before changing capabilities, operators,
packs or their contracts, read [contracts and operator migration](19-contracts-and-operator-migration.md)
and the [migration plan](../architecture-fix/operator-contract-migration-plan.md).
All operators must migrate; domain-pack adoption is selective. ADR-0111 remains a
proposal: preserve current contracts until scoped production changes are approved.

Cairn is a governed control plane for ML and agentic work: **pipelines** of governed
**legs** (data/ml flows on an execution engine, agentic templates on a runtime, operator
tasks, human gates), every leg its own **run**, every artifact registered by content.

| Question | Read |
|---|---|
| The system in one page (mental model, boundaries, dependency direction, invariants, safety rules) | root [`CLAUDE.md`](../../CLAUDE.md) — the constitution |
| How it is built, boundary by boundary (CURRENT, verified against source) | [`00-overview.md`](00-overview.md) then the numbered docs: [engine 04](04-engine.md) · [operators 05](05-operators.md) · [packs 06](06-packs.md) · [runtime 07](07-runtime.md) · [governance 08](08-governance.md) · [storage 09](09-storage-and-provenance.md) · [training 10](10-training.md) · [dataset 11](11-dataset.md) · [serving 12](12-serving.md) · [pipelines 13](13-pipelines.md) · [system map 01](01-system-map.md) · [dependency model 02](02-dependency-model.md) · [core concepts 03](03-core-concepts.md) |
| What each subsystem owns and must never do | the nearest `CLAUDE.md` (`cairn/`, `src/cairn/core/<pkg>/`, `cairn/operators/`, `cairn/packs/`, `tests/`, `docs/`) |
| The TARGET / frozen architecture (training and post-training) | [`training/README.md`](training/README.md) · [`training/FREEZE-v1.md`](training/FREEZE-v1.md) · the roadmap [`training/ROADMAP-v1.1-github.md`](training/ROADMAP-v1.1-github.md) |
| Decisions (ADRs, append-only) and candidate decisions | [`../decisions/README.md`](../decisions/README.md) · [`../adr/README.md`](../adr/README.md) |
| **Contracts** — what a shape or promise guarantees, CURRENT vs TARGET | [`../contracts/README.md`](../contracts/README.md) |
| How an execution actually flows, with `path:line` | [`../golden-paths/`](../golden-paths/) |
| What can run today | [`16-capability-catalog.md`](16-capability-catalog.md) (`/catalog`) |
| Known risks and recorded debt | [`15-architecture-risks.md`](15-architecture-risks.md) · [`ARCHITECTURE-HEALTH.md`](ARCHITECTURE-HEALTH.md) |
| Testing layers and what each proves | [`../testing/strategy.md`](../testing/strategy.md) |
| What is supported (the source for public claims) | [`../support-matrix.md`](../support-matrix.md) |
| Security model | [`../security.md`](../security.md) · runbooks [`../runbooks/`](../runbooks/) · compatibility [`../compatibility.md`](../compatibility.md) |
| How work moves: issue → `/work-issue` → review agent → PR → `/release` | [`17-issue-workflow.md`](17-issue-workflow.md) · [`14-developer-guide.md`](14-developer-guide.md) |

Dependency direction in one line: surfaces → dispatch → engine/runtime/dataflow → storage;
packs depend on core, core never imports a pack; operator packs import
`cairn.core.operator` only; `client/`, `ask/`, `sdk/` import nothing from `cairn.core`
(root `CLAUDE.md` §3; enforced by `tests/core/test_architecture_boundaries.py`).

Rules for this tree (`docs/CLAUDE.md`): architecture docs **explain**, they do not decide;
every claim is tagged OBSERVED / DOCUMENTED / INFERRED / CONFLICT with an absolute
`path:line`; when code and docs disagree, the conflict is recorded, never resolved by
editing the doc to match.

## Pending architecture proposals

DOCUMENTED / PROPOSED: [Capability contract conformance](18-proposed-contract-conformance.md) explains [ADR-0108](../decisions/ADR-0108-capability-contract-conformance.md). Maintainer decision pending; the current architecture and frozen training baseline remain unchanged.

**DOCUMENTED profile-scope review:** consult [ADR-0112](../decisions/ADR-0112-profile-scope-and-enforcement.md) before changing deployment configuration. The proposed typed scope and compatibility requirements complement operator migration; they are not yet runtime enforcement.
