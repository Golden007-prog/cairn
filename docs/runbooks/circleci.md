# CircleCI validation for the private Cairn repository

> **CI gate since 2026-10-12 (owner decision).** CircleCI runs validation; GitHub Actions
> keeps only publishing (release, images, chart). The 2026-10-06 pause (#2277) ended once the
> selector stopped running every job for documentation and process paths: `docs/`, `.agents/`,
> `.claude/`, `.codex/`, `.github/`, `design/`, `deploy/helm/`, root Markdown and licence
> files select the policy job only, and the Docker job runs only for image inputs
> (Dockerfiles, compose files, `deploy/light|prod|images`, the lockfiles and manifests). The
> duplicate Actions validation jobs are retired in a follow-up after the first green pipeline.
> To pause again, set `circleci_enabled` to `false` in `.circleci/config.yml`.

Status: cloud connection verified on 2026-09-15; configuration prepared locally.
Cloud validation and execution remain pending before this becomes a required check.
GitHub Actions remains unchanged during migration.

## Observed connection (2026-09-15)

- Organization: `cairndev` (`1c6fa522-d4e1-438d-b07e-8e2b39c9c684`).
- Project: `cairn` (`32550e26-240c-4833-80ff-e4cf99718c46`).
- Existing pipeline: `cairn-builds-test` (`a0270b49-9fca-4b8d-97f0-b9f6d476be11`).
- GitHub App checkout and config repository: `evs-cmd/cairn`; config path:
  `.circleci/config.yml`. Dynamic configuration and GitHub status reporting are enabled.
- Enabled automatic cancellation of redundant workflows and verified the settings readback.
- Updated existing trigger `1444c12e-277c-43e4-8591-c6a1bdb53087` from `all-pushes`
  to `only-build-prs`. Readback confirms PR `opened`, `reopened`, and `synchronize`
  events, plus default-branch and tag pushes. No duplicate trigger was created.
  This preset does not restrict the PR target; the selector currently compares against
  `main`. Other target branches require qualification before using CI as a merge gate.

Evidence: authenticated CircleCI API v3 project, pipeline, trigger and project-settings
responses. These are point-in-time observations, not proof of a successful checkout or
build. No build was launched during this setup. Local tests: 45 passed (architecture
boundaries and check selection). The new CI files still need publication to GitHub.

## Connect

1. Install the CircleCI GitHub App with access to **only evs-cmd/cairn**. The repository
   owner authorizes that installation; no token belongs in this repository or chat.
2. Create the CircleCI project/pipeline using `.circleci/config.yml` and enable dynamic
   configuration if the project requires an explicit setting. The setup uses the official
   `circleci/continuation@1.1.0` orb and CircleCI's injected continuation key.
3. Configure one trigger for PR creation/updates and default-branch pushes; CircleCI's
   `only-build-prs` preset also includes tag pushes. Initially use PRs targeting `main`.
   Avoid a simultaneous all-branch push trigger that duplicates PR builds. Enable
   cancellation of superseded branch workflows; preserve needed main/release runs.
4. Connect GitHub status/check reporting. Validate a passing and deliberately failing PR
   before choosing the actual returned workflow/check name as required in GitHub.
   Never require only `cairn-setup`: it selects checks but does not prove they passed.
5. After equivalent coverage and real runs are confirmed, separately approve retirement
   of duplicate GitHub validation workflows. Existing release/deployment/image publishing
   workflows are not replaced by this setup.

The CircleCI MCP is optional for setup inspection/control and may not expose installation,
project creation or trigger settings. Use the CircleCI/GitHub UI for unsupported operations.
Private GitHub branch protection still requires an eligible GitHub plan; CircleCI reporting
alone cannot block merging. Connecting the project may start metered builds.

## Coverage and cost

The selector compares the complete branch diff against the merge base with `origin/main`,
including deletions and both sides of renames. This initial configuration supports PRs
**targeting main only**; qualify other targets before enabling them. Missing base, empty
diff, main pushes, enforcement/configuration/dependency changes and unknown paths select
all checks. Selection is recorded as a setup artifact.

- Policy runs for every pipeline: architecture refusal tests and selector tests.
- Console changes select lint, styles, tests, build and generated API type freshness.
- Python/core/operator changes keep the full suite except the existing DBOS exclusion.
- The same changes run the `public` job (#2424, ADR-0120 D1): a locked install of the
  public set only (`uv sync --extra dev`, no `platform`/`ssh`/`ask`), `scripts/check_public_set.py`
  refusing any private distribution, the tests/core files listed in `.circleci/public_core_tests.txt`
  (259 of 490 today; the held-back ones and why are at the end of that file), and the CLI on
  the example pack. Moving or rewriting the held-back files is the rest of #2424.
- HTTP/API changes select Python and console consumers.
- Packaging/manifests and unknown paths select wheels and engine image smoke as well.
- Only README and Markdown runbook-only changes get the lightweight policy path.
- Main adds Python 3.12 alongside 3.13 and selects the full pipeline.

Dependency download caches are keyed by lockfile/runtime/architecture. Venvs and test
results are not reused as correctness evidence. Docker layer caching is not enabled.
The Docker job reuses the built frontend via a workspace and never pushes an image.
Small executors handle selection/policy/packaging; medium handles tests/frontend/images.
Measure job credits, cache hit rates and timings before increasing parallelism or narrowing
coverage. This setup makes no claim to a measured runtime or monthly build count.

API compatibility fails on breaking changes regardless of labels. A breaking migration
needs an explicitly approved compatibility policy; there is no automatic label waiver.
Semgrep runs without the old error-masking retry; existing findings may therefore fail.
No production credentials, GPU training, live model calls or deployments are configured.

## Validation and activation

Local selector tests:

```bash
env -u CAIRN_STORAGE PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/pytest --noconftest -c /dev/null tests/core/test_architecture_boundaries.py tests/core/test_circleci_selection.py -q
```

Validate the setup and generated continuation with the CircleCI CLI/API when available.
Local YAML parsing and selection tests do not establish cloud schema, image availability,
executor capacity or private checkout correctness. Test both console-only and full paths,
confirm JUnit output, check failures propagate to `cairn-validation`, and verify cancellation
never reports success. Do not retire the old workflow or claim CI readiness until then.
