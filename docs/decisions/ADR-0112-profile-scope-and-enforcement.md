# ADR-0112 — Bounded deployment profiles and scope enforcement

- **Status:** PROPOSED — maintainer requested this record, architecture guidance and implementation ticket; exact schema and compatibility migration require review before production changes.
- **Date:** 2026-09-25
- **Owner:** Cairn maintainer
- **Implementation:** #2248
- **Evidence:** local working tree at `3f67d348`; no production edits in this decision.
- **Related:** ADR-0026 pack/profile/environment split; ADR-0052 backend configuration; ADR-0068/0070 configuration proposals and implemented mechanisms; ADR-0101/0106 pipeline triggers/launch; proposed ADR-0111 separation; epic #2239.
- **Amendment:** tightens Profile acceptance and compatibility behavior if accepted; does not amend locked ADR text or silently accept other proposals.

## Context and current evidence

A profile selects a deployment's implementations and defaults. An environment applies
that profile with credentials, connections and operational state. Neither is a workflow.
The current outer TOML grammar is closed, but nested configuration is not uniformly typed.

| Classification | Evidence | Finding |
|---|---|---|
| OBSERVED | `cairn/core/pack/profile.py:1`, `:90` | Ownership split and closed outer profile keys already exist. |
| OBSERVED | `cairn/core/pack/profile.py:425`, `:464`, `:566` | Backend keys accept arbitrary scalars; config validation is deferred; MCP env values are strings without secret-reference enforcement here. |
| OBSERVED | `cairn/core/profile_backends.py`; `tests/core/test_profile_backends.py` | Backend descriptors and effective-value resolution already exist; reuse them. |
| OBSERVED | `cairn/core/settings.py:188`; `tests/core/test_runtime_from_profile.py` | A pinned profile runtime wins over OBS_RUNTIME. |
| OBSERVED | `cairn/core/engine/pipeline.py:201`; `cairn/core/triggers/routers/schedules.py:143` | Pipeline declarations and operational schedule records already own triggers. |
| OBSERVED | `cairn/core/engine/pipeline_triggers.py:202` | Legacy cron conversion enables schedules; migration must preserve or explicitly change activation through review. |

All nine tracked profiles parsed with inheritance on 2026-09-24. This is not deployment,
credential, installed-binding or runtime readiness proof.

## Decision (proposed)

### D1 — Profiles contain deployment choices only

Allowed: capability/operator bindings; typed runtime/engine/backend selection; typed
implementation deployment defaults; logical connections and nonsecret endpoints; adopted
policy/knowledge references; environment-owned governance configuration; notification
routing and secret references; existing inheritance and visibility metadata.

Forbidden: pipeline steps, flow definitions, capability/schema definitions, executable
business logic, raw credentials, provisioning scripts/manifests, cron expressions or
trigger activation state, and per-run datasets/model inputs/training hyperparameters.
Workflow semantic defaults belong in domain presets or workflow configuration; request
values belong in run inputs. A governance threshold is deployment authority, not a
user-overridable training knob. Keep existing scope checks and invocation precedence.
Profile defaults cannot weaken mandatory organization/environment policy.

### D2 — Reuse typed configuration boundaries

Extend the existing profile parser, backend descriptors/resolver, operator config schemas
and composition validation. No new registry service, store, resolver or environment knob.
Every accepted setting must have an owner, type, allowed values, consumer and scope.
Reject unknown backend keys, invalid values, unresolved required bindings and unsupported
execution choices before activation/admission and before effects. Doctor and actual
activation must agree; doctor alone is not enforcement. Extension settings must be
validated against installed extension declarations through existing discovery, without
hardcoding domain/provider enums in Core. Exact declaration syntax is a scoped decision.

Structural parsing can remain available without provider dependencies for inspection;
a runnable environment must complete semantic validation. Never silently ignore a setting
or fall back after an explicit unsupported selection. Keep legitimate defaults when a
setting is omitted. Different configuration families retain their documented precedence;
there is no new universal precedence chain.

### D3 — Secret references, not secret values

Profiles may name environment variables or declared connection/secret references; resolve
them only through existing scoped mechanisms. MCP process environments may include typed
nonsecret literal settings and explicit secret references. Do not treat every string as
a secret or invent a regex that claims to prove secrecy. Credential-bearing URLs and raw
credential fields are forbidden; field metadata and reference grammar must drive checks.
Never include resolved secrets in validation errors, effective-config output or provenance.

### D4 — Inheritance is inspectable and tested

Retain existing extends/cycle detection and established merge behavior. Show effective
nonsecret settings with their source (parent, child, environment fallback or default),
including policy adoption and runtime selection. Validate the merged result. Preserve
policy authority and fail closed on invalid explicit settings. A profile named sandbox
is not proof of isolation; mock mode and execution authorization are independent controls.

### D5 — Triggers stay outside profiles

Reusable declarations belong to declared pipelines; deployment-specific schedules belong
to existing trigger/schedule management. Stored records own workspace, selected environment,
inputs and enabled/paused state. Receiver credentials belong to connections/environment.
Reject cron, triggers and schedule state in profile files. Preserve ADR-0106's launch door;
do not add a scheduler/store or independently runnable flow. Legacy trigger spellings and
activation defaults need explicit compatibility tests, not incidental changes in this work.

### D6 — Compatibility and migration

Inventory all shipped profiles and known external consumers before tightening validation.
Classify each existing key, its schema and real consumer. Provide actionable diagnostics
and a migration mapping for rejected keys; do not add blanket allowlists or silently drop
values. Roll out through reviewed slices. Existing valid profiles keep their behavior;
intentional behavior changes require explicit review and before/after tests. Do not switch
production runtimes or policy adoption merely to make names appear consistent.

| Profile | Migration obligation |
|---|---|
| local-dev | Validate local bindings and explicit optional service defaults. |
| secure | Preserve inherited bindings, redaction and audit. |
| production | Reconcile comments recommending OBS_RUNTIME=temporal with inherited in_process; decide intended behavior explicitly. |
| prod | Preserve policy adoption and memory; expose inherited in_process honestly. |
| prod-dbos | Prove DBOS selection and production governance inheritance. |
| prod-infisical | Explicitly resolve secure-only inheritance versus production naming; no silent policy change. |
| campaign | Type engine, target, metadata endpoint and artifact selection; keep deployment-specific values out of generic contracts. |
| e2e | Correct obsolete closed-schema comments; validate S3/notification bindings. |
| sandbox | Verify actual mock/authorization behavior separately from backend names. |

## Enforcement and acceptance evidence

- All nine profiles have structural, merged-schema and applicable installed-binding tests.
- Reject unknown backend keys, invalid types/enums, unresolved required bindings, raw
  credentials in credential fields, workflow content and schedules; demonstrate no effects.
- Accept supported extension settings, nonsecret literals and secret references; exercise
  inheritance, override conflicts, cycles, policy authority and precedence.
- Actual runtime/engine selection agrees with inspected effective configuration. Invalid
  explicit choices cannot silently fall back. Existing governed invocation is retained.
- Validation is shared across applicable file/UI/CLI activation paths; test those paths,
  not only a parser helper. Preserve API/console and storage twins if affected.
- CI runs the rejecting cases and profile inventory checks without weakening existing gates.
  Public docs and compatibility notes describe only verified behavior.

## Consequences, alternatives and limits

Profiles become easier to audit but existing permissive extension values may need migration.
Prefer extending existing descriptors over a new profile DSL or generic infrastructure
layer. Merely documenting allowed keys is insufficient; rejecting tests and activation
checks are required. Moving packages to separate repositories, extracting executors,
all-domain migration and changing trigger activation policy are outside this decision.
No performance/scalability claim is established by these configuration checks.

## Concrete scope and compatibility proposal — 2026-10-06

**PROPOSED; preparation authorized, production schema not accepted.** Nine shipped profiles
contain 53 explicit leaf settings; [inventory](../architecture-fix/profile-field-review.csv)
records all of them without secret values. This is an explicit-source inventory, not proof
of every inherited setting's runtime consumer. All nine parsed through `load_profile`.
Every merged backend key has an existing `profile_backends.get_field` descriptor.

### Schema and precedence candidate

Retain current outer profile keys and their meanings; do not introduce a new profile format.
`capabilities` remains legacy pack-provider disambiguation; `bindings` selects operators.
For new semantic capability bindings, check the exact required revision against the chosen
operator. Do not merge those two existing tables into one ambiguous selector.

Validate `backends` using the existing `BackendField` descriptor owner: strict boolean for
`toggle`, declared string choice for `select`, string for `text`, plus field-specific rules
for connections and URLs. Reject unknown keys. Derived `<field>_connection` keys are allowed
only where the descriptor permits a connection; validate against the selected provider.
An explicit empty string is valid only if the field explicitly permits absence; otherwise
refuse, rather than silently using an environment value. The current resolver at
`cairn/core/profile_backends.py:335` falls through on blank values; this is an intentional
proposed compatibility change, not a claim about existing behavior.

Preserve backend precedence (profile → environment → descriptor default) for omitted
values, and operational configuration precedence (run → workflow → profile) within the
current resolver. Semantic defaults do not override mandatory governance. `config` keys
must identify an installed operator; values validate against its existing config schema
and scope metadata, including governance/operational distinctions. No universal merge rule.

Keep existing policy/knowledge omission-versus-empty-list semantics: `None` and `[]` are
not interchangeable. Preserve notification schema and MCP transport fields. Reject raw
credential fields/URLs using declared secret-field semantics, not heuristic string scanning.
Existing environment references remain references; no secret value appears in origin output.

**Extension decision for v1:** no arbitrary backend-extension dictionary and no new discovery
group. Existing backend descriptors cover all shipped backend keys. Provider implementation
extensions belong in the existing installed operator config schema. A genuinely new engine
backend setting requires a separate reviewed descriptor/consumer addition. Broader metadata-
only extension discovery is deferred until a concrete consumer needs it.

### Deployment choices to preserve

Observed merged runtime selection: campaign → temporal; prod-dbos → dbos; all other shipped
profiles → in_process. Preserve these explicit outcomes in this change. Correct misleading
comments separately rather than silently changing production behavior. prod-infisical keeps
its existing secure inheritance; its name is not permission to add prod policies. Sandbox
qualification still requires actual execution authorization/mock proof, not backend naming.

### Activation and evidence

Apply the same typed effective validation at inspection and applicable admission/activation
paths, through existing owners. Return field path and source without raw credential values.
Provider availability and installed binding checks are distinct from parsing. Explicit
invalid configuration cannot invoke a provider; valid omitted defaults remain supported.

Acceptance requires each inventory field's runtime consumer proof, merged-profile tests,
unknown/blank/wrong-type refusal, policy-empty semantics, invalid selection with zero
outbound calls, and API/console parity if effective-config output changes. No current
profile is declared fully conformant by this documentation or parser probe.


## Maintainer acceptance — 2026-10-06

**ACCEPTED WITH EXPLICIT REMAINING DESIGN CONDITIONS.** The maintainer replied "accepted"
to the concrete contracts/profile/invocation review and its stated outstanding work.
This dated status supersedes earlier PROPOSED labels for the bounded decisions below;
historical proposal text is retained rather than rewritten. This is design acceptance,
not implementation completion, runtime qualification, commit or deployment authorization.

Accepted review: [foundation implementation design review](../architecture-fix/foundation-implementation-design-review.md).
Remaining preimplementation planning: supported schema vocabulary/format policy;
profile-field-to-runtime-consumer mapping; individual stage/interface classification;
and resolution propagation through invocation and recovery. Acceptance does not fill
these gaps by assumption. Final file-level plans still follow impact/approval rules;
new contract deltas outside this accepted scope require a further scoped decision.

Other proposed ADRs and F3–F6/specialized designs are not accepted by this response.

Accepted scope: deployment-only profiles; current table meanings and family-specific
precedence; strict typed backend validation using existing descriptors; secret references;
no open backend-extension dictionary in v1; preservation of observed shipped runtime and
policy choices; explicit invalid-setting refusal with the documented compatibility review.
The field inventory is evidence of declarations, not verification of every runtime consumer.
No profile migration or new enforcement is claimed to be installed by accepting this ADR.


## Maintainer acceptance of detailed mappings — 2026-10-06

The maintainer explicitly stated "accepted the design" after reviewing the detailed
mapping update in [foundation implementation design review](../architecture-fix/foundation-implementation-design-review.md).
Acceptance now includes the specified bounded JSON Schema vocabulary (including format
exclusion), profile consumer/ownership mapping, stage/engine interface classification,
and proposed execution/recovery propagation direction. This supersedes earlier pending
acceptance statements for those specified decisions; historical text remains intact.

Exact additive DataflowSpec/OperationContext/activity fields, unresolved gateway/embedder
handoffs and per-kernel helper-versus-invocation choices were not fully specified in that
packet. Resolve them in the affected issue's scoped plan; do not treat this acceptance as
a choice of unspecified API fields or an architecture waiver. Existing issue impact gates
continue to apply to additional contract deltas. Preserve accepted scope without asking
for approval of the same decisions again.

This records design acceptance only. Installed closure, consumer equivalence, lifecycle,
serialization and recovery evidence remain required. No issue is closed and no production
implementation, commit, push or deployment is claimed or authorized by this record.
