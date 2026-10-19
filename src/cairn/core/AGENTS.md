# cairn/core — what stays in core and why

ADR-0120 D3 sends the product modules to the private platform (`packages/cairn-platform`,
import name `cairn_platform`, #2421). Core never imports `cairn_platform`; it reaches the
platform only through the entry-point groups (`cairn.routers`, `cairn.model_catalogs`,
`cairn.seeds`, and the CLI's `cairn.cli`). Wave 1 moved `judge_scorecard`, `eval_mining`, `storage/capture` and the
`capture`, `datasets` and `judges` routers; wave 2a moved the product-only CLI commands
(`cairn_platform.cli`); wave 2b turned the dispatcher's by-name product calls into the
platform's `cairn.run_hooks` providers (`cairn_platform.hooks`); wave 2c moved the product
routes out of the public routers; wave 2d moved `POST /pipelines/suggest`
(`cairn_platform.routers.recommender`), the `playground`, `insights`, `training_runs` and
`model_catalog` routers, and the modules they freed: `pilot`, `playground/`, `adapter_chain`;
wave 2e made the series cap a `run.admitting` provider (`cairn_platform.hooks:series`) —
the dispatcher fires and settles/releases the reservation by run id; wave 2f (decision 7)
made the starter budget a `workspace.created` provider (`cairn_platform.hooks:workspace`,
fired by the `admin` router), moved `score_run`'s training-history write and divergence
reaction into the platform's `scoring` hook (the continuous-eval consumer fires the run
hooks too), and moved the nine series-chain modules (`pilot_ledger`, `pilot_scorecard`,
`proxy_ranking`, `recommender`, `models_catalog` + its `cairn.model_catalogs` registration,
`training_history`, `storage/training_runs`, `mid_training`, `fidelity`) and the
`platform-demo` seed (`showcase_seed`, `demo_scenarios` + its `cairn.seeds` registration;
`POST /demo/seed?showcase=1` runs it by name, 404 when absent).
#2427 moved the console's surfaces — `core/console`, `core/chat`, the `/ui/api` routers and
the `setup`, `features`, `console`, `webui`, `builder`, `chat`, `mcp` providers, plus the
`/admin/*` and `/demo/*` half of `admin` — to `cairn_platform`; `/auth/*`, `/workspaces` and
`/variables` are the public `triggers/routers/accounts.py`; the gateway's models are core's.
ADR-0120 D2 as amended (#2427, owner 2026-10-13; ADR-0123 D1 carries it): `jobs` (the pull
lane) and `/oauth/callback` (`triggers/routers/oauth.py`, the redirect of `cairn connect --via
oauth`) are D2; `ask` is the platform's (`cairn_platform/routers/ask.py`).
#2497 closed the product-modules inventory (owner decisions 2026-10-13): `actions/` retired to
the platform (`cairn_platform.actions`; `engine/runner.py` keeps the `ActionHandler` protocol and
the platform's `actions` `cairn.routers` provider injects the handler through `RouterDeps.runner`
— the public core executes no actions by itself), `cairn/integrations/observability` moved to
`cairn_platform.integrations.observability` (the incident pack and the clickstack, blast-radius,
git and logs-analytics packs import it there), and every other module stays in core by decision.
A module that is product logic leaves core; one that stays is justified here.

What stays in core by decision, and why:

| Module(s) | Why it is core (owner decision) |
|---|---|
| `model_license_gate` | A fail-closed gate is governance — the licence door held by `admission_door.py`, `preflight.py` and `dispatch/dispatcher.py` (#2421 decision 1, 2026-10-12). |
| `eval/`, `storage/eval_ledger` | A run's verdicts and failure labels are part of the governed record, like the audit log; the platform's `scoring` hook keeps calling `score_run` (#2497 row 1). |
| `storage/judges`, `judge_agreement`, `storage/judge_labels` | A versioned, immutable judge is a governance object the leg executor resolves fail-closed (`judge:<name>`) before any paid call; the judges router stays the platform's (#2497 row 2). |
| `dataset_roles`, `eval_cutoff` | Role assignment and the eval cutoff are what the intake and holdout doors enforce (#2497 row 4). |
| `storage/knowledge_corpora` | BYO corpora are a public-core feature served by the `rag_local` builtin; the console's knowledge router stays the platform's (#2497 row 5). |
