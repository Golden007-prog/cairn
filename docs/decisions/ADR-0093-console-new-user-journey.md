# ADR-0093 — Console new-user journey: playground-first, five blessed workflows, promote-to-pipeline

Status: **PROPOSED** (2026-08-22 — epic #1564, slices #1565–#1574).

Relates to / builds on: **ADR-0073/0075** (playground — the sandbox-first
surface this journey leads with; verified live as built), **ADR-0054** (ONE
account, admin-invite — kept, improved with invite links), **#1489 H1**
(README repositioning — same "what cairn is now" story, in-product),
**ADR-0092/#1537** (authoring ergonomics — the `visibility` manifest flag
lands there-adjacent), **#1339/#774** (prod-parity blockers this journey
depends on), **#683/#1159** (trigger form + `on:` arming — consumed by the
promote wizard). Full evidence and research citations:
`design/console-user-journey-2026-08-22.md`.

## 1. Context

Live inspection (2026-08-22, prod box via tunnel — public ingress down, see
D0) shows the console's core loop now works end-to-end: bundled sample → ▶ Run
→ live graph → done in ~40s with a receipt; the first-run checklist (#782),
workspace switcher, connectors catalog, and the ADR-0073 playground (gate
what-if → "adopt threshold files a policy proposal", HITL practiced in
sandbox, "Run for real" promote CTA) are all real. Research across 17
workflow/ML platforms (Prefect, Dagster+, Temporal, n8n, Zapier, Astro,
Retool, Inngest, W&B, Langfuse, LangSmith, Braintrust, Databricks, Modal,
Replicate, HF, OpenAI) shows cairn already implements several table-stakes
patterns (run-before-connect, template gallery, checklist) and one
best-in-class one (governance-as-review via gate what-if).

What blocks a new user today is not the loop but its frame:
- Public prod is down (duplicate `mlflow.cairndev.sh` Caddyfile block →
  Caddy crash-loop on every boot), and even when up, the deploy path ships no
  SPA (#1339) and Caddy buffers SSE (#774) — the demo moment dies at the proxy.
- The gallery drowns 5 curated workflows under 10 dev/smoke templates and a
  deprecated trio whose card copy is "DEPRECATED (ADR-0081 P1c, #1050)…".
- There is no promote-to-pipeline step: pipeline launch has no schedule/
  trigger; only `cron` arms (#1159); schedule inputs are raw textareas (#683);
  the Pipelines page lists campaign residue.
- Nav orphans (Connectors — checklist step 1! — Triggers, Users), a 7-way
  environment picker leaking `campaign`/`e2e`/dup-prods, a ~10-field run form
  with `Notion page ID` exposed, and the known fake/dead spots
  (`docs/audits/console.md`).

## 2. Decision

1. **Journey shape: playground-first, connect lazily, promote explicitly.**
   New user lands → runs the keyless sandboxed sample FIRST (checklist step 0)
   → sees the receipt → connects a provider only when a chosen workflow's
   contract requires it (capability-driven prompts) → "Run for real" →
   **"Make it recurring"**: one wizard that launches a pipeline with a
   form-based schedule (cron as escape hatch), created inactive-by-default
   with an explicit Activate toggle — the Zapier/n8n/Astro promotion pattern.
   Integration order: nothing → LLM (gateway preferred) → data → GPU/SSH →
   MCP/SaaS; never an up-front gate (Temporal's credentials-first flow is the
   researched anti-pattern).
2. **Five blessed workflows** (curated copy + sample tiers + live acceptance +
   receipt): dataset-readiness→`readiness` (proven), incident-rca→`rca`,
   security-triage→`triage`, eval (install it — currently a curated dead-end),
   finetune→`e2e-finetune`; quantize optional sixth. **A `visibility:
   internal` manifest tier** hides `*-smoke`, `e2e-fullstack`, `dpo-*`,
   `closedbook-measure`, `orchestrated-incident`, and the deprecated fix trio
   from gallery and pipeline picker. Inference/RAG/Serving stay visibly
   "upcoming".
3. **Environments collapse to `sandbox` / `production`** for users (playground
   binds `sandbox`, not `campaign`); other envs become admin-only.
4. **Seeded generosity as governance**: a fresh workspace gets demo content
   (wire the existing `demo_seed.py`) and a default series budget, framed as
   "you cannot accidentally overspend" (Modal/prepaid-as-budget pattern);
   HITL work surfaces as a "Waiting on you" badge fed by the Decisions inbox.
5. **Admin-invite stays** (governed posture; no self-signup); add an
   invite-link flow and fix the Email-labeled username field.
6. **Not doing:** a drag-and-drop pipeline composer (record-first authoring is
   the posture; picker + SDK cover creation).

## 3. Consequences

- Time-to-first-green-run for a keyless user is already ~40s once the frame is
  fixed; the deltas are curation, navigation, and the promote wizard — mostly
  console work plus two small backend features (visibility tier, budget/demo
  seeding) and the already-tracked trigger work (#683/#1159).
- Hiding internal templates changes what `registry/installed-workflows`
  returns per audience; the flag must be data (manifest), not a console-side
  hardcode, so CLI/API stay able to see everything with `--all`.
- Prod parity (#1339/#774 + the Caddyfile fix) is a hard prerequisite: until
  then the public product contradicts every claim in this journey.

## 4. Rollout

P0 unblock + first impression (D0 Caddyfile fix; #1339/#774; gallery curation;
nav + checklist reorder) → P1 promote story (recurring wizard, HITL badge, env
consolidation, run-form fold, seeding, bless workflows 2–4, invite links) →
P2 honesty sweep + tutorial. Slice list in the epic.
