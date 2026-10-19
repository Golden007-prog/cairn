# ADR-0096 — Console pipeline builder: a record-first projection

- **Status:** ACCEPTED
- **Date:** 2026-08-28
- **Epic:** #1762 (slices #1763 builder API · #1764 BuilderView · #1765 publish)
- **Design:** `design/cairn-builder.html` — interactive mock, approved; the mock IS the visual spec.

## Context

The console can launch, inspect, and approve pipelines but cannot *author* one.
Authoring today means hand-writing YAML in a pack — correct, but invisible to
the console user the product is aimed at.

Survey of how comparable platforms do visual authoring:

| Platform | Model | Verdict for cairn |
|---|---|---|
| n8n | free-form canvas, everything is a node, mutable JSON in the DB | rejected — no governance model, arbitrary topology is exactly what our grammar forbids |
| Langflow | drag-drop, typed color-coded ports | typed ports validated our typed-wire direction; canvas rejected |
| Temporal | no builder, code-first, UI is observability | doesn't transfer — cairn pipelines are already declarative DATA, not code |
| **Kestra** | **YAML is the source of truth; No-Code forms generated from plugin JSON Schemas; topology is a live read-only projection; bidirectional form⇄YAML sync** | **adopted** |
| AWS Step Functions Workflow Studio | guided insert-into-DAG from a catalog, emits ASL JSON | adopted (the interaction model) |

Kestra's whole trick — plugin schemas drive forms, validation, and docs from
one source — is cairn's operator contract (`input_schema` / `output_schema` /
`config_schema` on the class, ADR-0068) one language over. We copy the
architecture pattern; integrating Kestra itself was evaluated and rejected
(second control plane next to Temporal, Vue-vs-React, its grammar not ours,
its mutable-DB record vs our hashed pack record).

## Decision

**D1 — The YAML document is the record; the builder is a projection.**
The builder edits an in-memory model rendered ONE WAY to YAML. Publish writes
that YAML into a pack; it is content-hashed and `expected_hash`-pinned at
launch like any shipped pipeline. The console never becomes the source of
truth. (Import = parse; live bidirectional text editing is explicitly out of
v1.)

**D2 — Only legs are nodes.** The palette offers exactly the ADR-0084 leg
kinds: `task:` / `flow:` / `template:` / `gate:` / `loop:`. Everything else is
a frame or property, never a draggable block:
- governance (budget/series/approvers) → a panel on the pipeline
- environment/infra → a launch-time run property (profile backends)
- artifact storage → the typed edges (`consumes`/`produces`)
- triggers (`on:`) → an entry badge
- secrets → env-var NAMES in the record; values stay on the host

**D3 — Guided insert, not free-form canvas.** Steps are added by picking from
the catalog at an insertion point (click "＋ insert" or drop on a connector);
wiring is data (`$input.*` / `$steps.*` pickers filtered to what is reachable
at that step), never hand-drawn lines. Two narrow drag gestures only, both
targeting connectors (always-valid drops): palette-card → connector, and
step-card reorder with chain-splice rewiring. The topology strip is a live,
clickable, NON-editable projection (plain SVG in v1 — no new deps).

**D4 — The server is the only validator.** `POST /builder/validate` is a thin
door to `parse_pipeline_def` + `plan_pipeline` (typed wire-check included) and
returns the same errors the CLI prints. The builder client mirrors cheap
checks for latency but the server verdict is authoritative — the chokepoint
stays a function others call, not a block others copy (#1376 lesson).
Structural governance surfaces at authoring time: an `external_write` /
`paid_compute` leg with no gate on its ancestor path is an error, not a
runtime surprise.

**D5 — Publish is one file write into the dev-pack door.** Runtime pack
INSTALL remains 501 by posture (#1545). Console-authored pipelines land as
DATA in a `workspace` pack under the first `CAIRN_PACK_PATH` entry — created
on first publish (pack.toml `kind="domain"`, `trust_level="workspace"`,
empty `pipelines/`), registered via the idempotent `load_all_packs()`.
Discovery scans `pipelines/` dirs per request (#1125), so the published file
appears in `/pipelines/installed` with no install step. No `CAIRN_PACK_PATH`
⇒ 409 with the exact env line to add — never a silent fallback location.
The pack dir is plain files: git it, PR it, review it like any change.

**D6 — Drafts are client-side in v1.** localStorage, keyed per deployment.
A server draft store (multi-device, multi-author) is deferred until the
authoring loop proves itself; the record on publish is unaffected either way.

**D7 — Operators are never authored in the console.** They are code in
operator packs, built in an IDE with tests (Rule 5). The builder consumes
their schemas from the catalog. Flows (linear stage lists) and domain-pack
pipelines are the console-authorable surface — which by ADR-0084 D4 is the
entire mandatory runnable surface of a domain pack.

## Rejected

- **n8n-style free canvas** — manufactures invalid states our grammar makes
  unrepresentable; most expensive option with negative payoff.
- **Embedding/integrating Kestra** — see Context table.
- **React Flow edit-mode for v1** — the read canvas (GraphCanvas) stays as is;
  the builder's SVG strip is enough until B5, avoiding an edit-mode rabbit
  hole in the first slice.
- **Runtime pack creation via pip/entry-points** — collides with #1545;
  `CAIRN_PACK_PATH` already exists for exactly this.
- **Storing secrets or env VALUES in the document** — names only, presence
  checked against the host, loud failure at publish and at run.

## Consequences

- New API surface: `GET /builder/catalog`, `POST /builder/validate`,
  `POST /builder/publish` (#1763, #1765). `/builder` joins the Vite proxy
  `API_PATHS`.
- D1's import direction is `POST /builder/parse` + `GET /builder/source/{pack}/{name}`
  (#1769): the first is a door to `parse_pipeline_def` returning the def as
  JSON, the second reads an installed pipeline's file through
  `pipeline_resolve`'s own discovery. The console maps that JSON onto the
  builder model (the inverse of `renderYaml`) and never parses the grammar
  itself. Editing YAML by hand, "Edit in Builder", and the blueprint start
  screen are three doors into that ONE importer; editing a shipped pipeline
  opens a workspace COPY, stated in the header and on Publish.
- New console view `BuilderView` (#1764) matching `design/cairn-builder.html`;
  new scoped classes in `cairn.css` (no inline styles).
- The `workspace` pack becomes a first-class concept a deployment may carry;
  curation/trust surfaces should learn `trust_level="workspace"`.
- B4 (ask-to-draft) gets a clean seam: `cairn ask` can emit a builder
  document instead of only pointing at templates.
