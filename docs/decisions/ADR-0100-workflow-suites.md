# ADR-0100: Workflow suites — the curated agentic-leg catalog for cairn-light

- **Status**: PROPOSED
- **Date**: 2026-09-01
- **Epic**: #1838 (slices #1839–#1845)
- **Relates to**: #1828 (cairn-light), ADR-0084 (one flow concept), ADR-0099/#1826 (services per pipeline)

## Context

cairn-light (#1828) ships a curated surface: proven core + single agentic-leg
workflows, with multi-leg pipelines and ML/GPU packs held. That makes the
workflow catalog the front door — and today it is a flat list.

A 2026-09-01 analysis of a 120-workflow market catalog (AI-SRE / platform
automation), plus market research, established three things:

1. **Incident investigation is table stakes.** The catalog's own top three
   (incident RCA, blast radius, change correlation) already exist as cairn
   flows, and the AI-SRE space is crowded (Causely, Qevlar, Elastic agentic
   SOC, and others).
2. **The catalog's blind spots are cairn's home turf.** It contained zero
   data-pipeline, model-quality, or audit-evidence workflows — the three
   areas where cairn already owns the operators (ingest, data_expectations,
   lineage, eval, model_registry, redact, audit, run provenance) and where
   competition is thin.
3. **The bottleneck is authoring, not building.** 13 of the 17 missing legs
   identified need zero new operators; each is a `.md` flow following the
   proven incident-rca shape.

## Decisions

### D1 — A suite is a tag, not a mechanism

`suite:<name>` joins the existing pack tag vocabulary (peer of `domain:*`),
with a per-flow override in front-matter for packs whose flows span suites.
Catalog API, console, CLI, and docs group by it. Nothing moves, nothing is
renamed, nothing is deleted — additive only, per the #1828 owner rule. A
flow without a suite tag renders ungrouped, exactly as today.

Rejected: suite-as-pack (would force pack restructuring and break existing
flow ownership) and suite-as-new-manifest-field (a tag already carries it).

### D2 — Six suites

| Suite | Scope | Anchor assets today |
|---|---|---|
| `sre` | incidents, on-call | incident pack (5 flows), security_triage |
| `delivery` | changes reaching prod | git pack, change-correlation, pii/redact |
| `data` | ingested data quality | ingest, data_expectations, data_profile, lineage, dataset_readiness |
| `model` | model quality + LLM ops | eval, model_registry, llm-gateway, redteam (held) |
| `ops` | infra housekeeping | monitor/propose, backup profile, compute (held) |
| `governance` | audit + access evidence | run provenance store, audit_store, secrets, connections |

### D3 — Every leg follows the incident-rca shape

Operators gather evidence in parallel → ONE tool-less LLM synthesis step →
schema-pinned artifact → where the leg acts, a gate (#1275) before the
action. No leg in light performs an ungated side effect.

### D4 — Light inclusion bar is the #1828 keep bar

Ran live end-to-end within the last month, has golden-fixture tests, needs
no GPU. A leg that fails the bar is **authored but held** — code and tests
ship, the light catalog hides it, and it graduates by un-holding (zero code
to restore). Held at authoring time: GPU idle reaper (compute pack), red-team
scan brief (redteam pack).

### D5 — Positioning follows the suites, not the incident story

Light leads with governed data + model operations (where the investigation,
the policy-controlled action, and the provenance record combine); incident
legs are included but are not the headline.

### D6 — Rejected leg categories

- **Forecast/advisor legs** (right-sizing, capacity forecasts): time-series
  analytics products; a single LLM leg gives shallow answers and costs
  credibility.
- **History-corpus legs** (incident similarity, known-signature matching):
  need an incident history new users do not have.
- **Auto-remediation beyond gate-guarded reversible actions**: cairn's
  endgame, not its launch. The GPU reaper is the only "auto" leg kept
  (held), because releasing an idle box is small and reversible.

## Suite rosters

New legs per suite are enumerated in the slice issues (#1840–#1845); the
epic (#1838) carries the summary table. Already-shipped flows are tagged in
W0 (#1839).

## Deferred enablers (not part of this ADR's epic)

- CI/test-results operator — one collector unlocks the entire QA family;
  highest-leverage single build post-light.
- Terraform plan reader + apply gate — strongest long-term product story;
  deserves its own ADR.
- K8s collector — check for a standard Kubernetes MCP server first.
- Inbound webhook trigger (#1159) — the trigger door for every "on alert"
  story; ranked above any new workflow.

## Consequences

- The console/CLI catalog gains one grouping dimension (W0) and light's docs
  pass (#1834) can present suites as the product's table of contents.
- 17 new legs are authored as `.md` flows against existing operators; two
  are held at birth by #1828's pack exclusions.
- The suite tag creates no runtime behavior — engines, planners, and
  governance are untouched.
