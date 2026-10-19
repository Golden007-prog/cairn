# ADR-0135 — The public core ships no authoring SDK; the public tree carries no CircleCI configuration and no platform-only record

- **Status:** ACCEPTED 2026-10-19 by the owner ("cairn sdk should be removed. it doesn't really
  work"; "we don't need CircleCI on public cairn"; the docs: "drop the table from public but keep
  them in private … keep only recommended in public").
- **Date:** 2026-10-19
- **Issue:** #2430 (the flip).
- **Amends** ADR-0120 D1 and ADR-0121 D1/D3 (the public distribution set: `cairn-sdk` leaves
  it) and ADR-0087 (the SDK authoring projection is the platform's). **Implements** ADR-0132's
  "CircleCI is removed from the public repository" for the assembled tree. **Keeps** ADR-0121 D7
  as amended by ADR-0132 (GitHub Actions validates and publishes the public repository).
  **Narrows** ADR-0121 D4 and ADR-0124 D5 (which docs the public tree carries: every ADR, "as
  written") to the records whose subject the public code has.

## Context

1. `cairn-sdk` (`sdk/`, ADR-0087) is the Python authoring projection: a `@pipeline` class that
   `cairn pack build` compiles to pack records. It was listed among the public distributions
   (ADR-0120 D1, ADR-0121 D1) and carried into the public tree as `packages/cairn-sdk`, a base
   dependency of `cairndev`, with its docs page and its tests.
2. The owner's judgement on 2026-10-19: it does not really work, and the first public release
   must not ship a package that would then have to be supported. A version on PyPI can never be
   re-uploaded.
3. The public tree carried the monorepo's `.circleci/` (config, jobs, selector, the public test
   list) beside the generated GitHub Actions workflows. ADR-0132 already decided the public
   repository validates on GitHub Actions only; the directory was a leftover of the generator.
4. `docs/` was carried whole: 134 decision records and every architecture doc, golden path and
   runbook. A classification of each against what ships publicly (2026-10-19) found 14 records
   and 10 docs whose primary subject exists only in the platform — console journeys, serving,
   red-teaming, the training campaign, RL, the PII cascade, product licensing, and
   assessments of the whole monorepo. A public reader would find decisions about code they do
   not have, and the catalog names 177 private things. ADR-0121 D4 meant "accepted ADRs"; 57
   carried records are proposed — but status labels drift (ADR-0039/0040 are "decided" in the
   index and Proposed in the file), so status alone is not the right filter.

## Decision

**D1 — The public core ships no authoring SDK.** The assembler no longer carries `sdk/`,
`tests/sdk/`, or the `cairn-sdk` dependency, workspace member and source; the public set is
`cairndev`, `cairn-client`, the builtin and optional operator packs, the contracts and the
example pack. `cairn-sdk` and `tests/sdk/` are the platform's (the carve maps `sdk/` to
`packages/cairn-sdk/`, beside `cairn-ask`). The docs site has no SDK page.

**D2 — `cairn pack build` registers only where the SDK is importable.** Without `cairn_sdk`
the public CLI mounts a hidden stub that says the command is part of cairn-platform and exits
1 — the rule `cairn ask` already follows (ADR-0127 D7, #2536): a public install lists no verb it
cannot run. The two public tests that compiled an SDK class skip without it; the two whose
subject is the SDK leave the public list.

**D3 — The public tree carries no CircleCI configuration.** The monorepo's CircleCI `public`
and `public-e2e` jobs remain the SOURCE the Actions `ci.yml` is generated from, and nothing
under `.circleci/` is assembled. In the public tree `tests/core` is exactly the public list, so
the Actions job runs `tests/core` directly. The CircleCI runbook is the monorepo's.

**D4 — A record or doc stays with the monorepo and the platform when its primary subject is
only the platform's.** The assembler's `DOCS_PRIVATE_PATHS` names them: ADR-0021, 0039, 0040,
0056, 0063, 0064, 0077, 0079, 0093, 0095, 0096, 0098, 0100, 0107; `architecture/`
10-training, 11-dataset, 12-serving, 16-capability-catalog, 15-architecture-risks and
ARCHITECTURE-HEALTH; `golden-paths/` training, serving and dataset; `runbooks/`
change-scope-hooks (and circleci, D3). The public decisions index keeps no row for a record
that is not carried, and a kept doc's link to a file that is not carried keeps its text and
loses the link, so nothing public dangles. Records whose subject is partly public (workspaces
and accounts, bundles, the eval ledger, the Playground's sandbox, compute lifecycle, the
training-experience contract that thirty public modules cite) stay carried. The monorepo and
the platform keep every record (append-only, ADR-0121 D4).

## Consequences

- The first PyPI release is 32 packages, not 33. `pip install cairndev` pulls no SDK.
- A platform install keeps `cairn pack build`; a public install sees no such verb.
- `scripts/check_public_set.py` refuses a public venv holding `cairn-sdk`.
- Retiring the SDK from the platform as well is a separate decision (its own ADR); nothing
  here deletes it.
- The public tree carries 121 decision records (this one included). Code comments that cite a record not carried
  (ADR-0039 in the job channel, ADR-0093 in the sandbox profile, …) cite history, not a link;
  they stay.

## Verification

- `tests/scripts/test_assemble_public_tree.py`: no `.circleci/` in the output, `ci.yml` runs
  `tests/core`, no `packages/cairn-sdk`, `cairn-sdk` in neither dependencies nor sources.
- `tests/scripts/test_carve_platform.py`: `sdk/` and `tests/sdk/` classify as platform.
- `tests/cli/test_pack_build_stub.py`: the stub without `cairn_sdk`; the real command with it.
- `tests/scripts/test_assemble_public_tree.py`: the D4 records and docs are absent, the index
  has no row for them, and no kept doc links to one.
- The assembled tree's own CI job passes from a fresh install.
