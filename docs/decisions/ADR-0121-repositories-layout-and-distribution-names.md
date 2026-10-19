# ADR-0121 — Repositories, source layout and distribution names for the public core

**Status:** accepted (2026-10-13, #2401, epic #2399; amended by ADR-0123). D1–D4 record owner decisions of
2026-10-11; D5–D7 need approval. Depends on ADR-0120 (what is public).

## Context
The public core (ADR-0120) and the private platform live in one repository today. Its history
holds personal email addresses, old infrastructure addresses and commits made under an
employer address, and 2,396 issues and pull requests that were never written for the public.

The layout reflects how the code grew. The engine is a flat `cairn/` tree. The 36 operator
packs sit inside it (`cairn/operators/*/*/src`) behind an exclude list. The 5 contract wheels
sit at the top level (`contracts/*`). Agent instruction files ship inside the engine wheel.

The PyPI name `cairn` belongs to another project. The 36 operator distributions are named
`operator-*`. 48 operator manifests declare `publisher = "agentops"`, the name of an
unrelated commercial product. The image variable is still `OBSAGENT_IMAGE`.

## Decision
**D1 — Two repositories, the public one with fresh history.** The public repository is
`github.com/cairndev-sh/cairn` (owner, 2026-10-12), today a project page of six commits
(README, LICENSE, assets); the core arrives there as one import commit, so the engine's
history in this repository is not carried over. This repository becomes the private `cairn-platform`
repository, carved with `git filter-repo` so its history is kept. The platform depends on the
public wheels, pinned to a SemVer minor, and its CI also runs nightly against the public
`main`.

**D2 — Source layout.** The public repository uses:

```
src/cairn/          the engine (import name `cairn`)
packages/           cairn-client, cairn-sdk, the 12 cairn-operator-* builtins,
                    cairn-contracts-{dataflow,audit,mcp}
examples/           the example pack
deploy/             light (compose), helm
docs/               contributor docs, architecture, contract specs, accepted ADRs
tests/
```

The platform repository mirrors it: `packs/`, `packages/`, `console/`, `core-ext/`, `deploy/`,
`docs/`.

**D3 — Distribution names.** The engine distribution is `cairndev`. The import name `cairn`
and the `cairn` command do not change. Operator packs become `cairn-operator-<name>` (36
renames). Domain packs become `cairn-pack-<name>`. `cairn-contracts-*`, `cairn-client` and
`cairn-sdk` keep their names. Every name is reserved on PyPI before the first upload, because
PyPI names are permanent. Package URLs point at `github.com/cairndev-sh/cairn`.

**D4 — User docs live in the public repository.** (Revised 2026-10-12; the first draft kept
them in the landing repository, which is private.) The user documentation site
(docs.cairndev.sh, a Starlight app today at obs-agent-landing `apps/docs`) moves into the
public repository as `docs/site` at the fresh-repo moment and deploys from there, so a
documentation fix and the code change it describes land in one pull request and track one
version. The public `docs/` also holds the contributor material: architecture, contract
specifications, accepted ADRs and the development guide. The landing repository (marketing
site and blog) stays private. Process material (issue workflow, audits, ideas, roadmap,
`status.yaml`, ADR drafts, `docs/architecture-fix`) stays in the platform.

**D5 — Identity strings.** `publisher = "cairn"` in every operator manifest. `OBSAGENT_IMAGE`
becomes `CAIRN_IMAGE` in compose, Helm and workflows.

**D6 — What a published distribution carries.** Every wheel and sdist carries LICENSE,
NOTICE, `[project.urls]`, authors and `py.typed`. None carries `CLAUDE.md`, `AGENTS.md`,
tests, `design/` or `.claude/`. `THIRD_PARTY_NOTICES.md` and a CycloneDX SBOM ship with the
engine and its image.

**D7 — Release and contribution mechanics.**
- Versioning: SemVer, with a keep-a-changelog `CHANGELOG.md`.
- Publishing on a tag: PyPI trusted publishing, a GHCR image and a Helm OCI chart.
- CI: validation runs on CircleCI (owner decision, 2026-10-12): the `policy`, `python`
  (3.12–3.14), `web`, `package` and `docker` jobs, selected per change by
  `.circleci/select_checks.py`. GitHub Actions keeps only publishing: the release workflow
  (PyPI trusted publishing is bound to Actions' OIDC), the GHCR image and the Helm chart.
  No self-hosted runner anywhere.
- Contributions: Apache-2.0 with DCO sign-off, enforced by a check.
- Repository files: Contributor Covenant code of conduct, `GOVERNANCE.md` (maintainer-led),
  `SUPPORT.md`, and `SECURITY.md` with a role address.

## Consequences
- Recorded identity survives the renames. Pipeline records hold operator refs (`pack/name`)
  and contract IDs and digests, not distribution names, and contract bytes are not touched.
- Issues and pull requests in this repository stay private. Public history starts at the
  first public commit, and public issues start empty.
- The move is one large change per repository, made once.
- Every path in `CLAUDE.md`, the docs and the skills changes in the public repository.

## Not decided here
The boundary (ADR-0120). The environment prefix (ADR-0122).

## Verification
#2427 (platform repository), #2428 (public repository), #2429 (renames) and #2430 (security
review and release), against the checklist in `design/oss-release-plan.md` §6.
