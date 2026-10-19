# ADR-0133 — Private issue links become plain text in the public tree; image names keep `evs-cmd`

- **Status:** ACCEPTED 2026-10-16 by the owner ("evs-cmd 1": keep the image names, drop the
  private issue links).
- **Date:** 2026-10-16
- **Issues:** #2430 (the pre-flip scan: `docs/oss/preflip-scan-2026-10-15.md`), #2399.
- **Amends** ADR-0123 D6, ADR-0124 D2 (their issue-reference clauses) and ADR-0124 D5, which
  kept issue references to `evs-cmd` in the public tree and exempted them from the leak scan.
  **Keeps** ADR-0123 D6 / ADR-0124 D2 for image names and the registry.

## Context

1. ADR-0123 D6 (2026-10-13) let the public tree keep its references to `evs-cmd` issues and
   pull requests, and ADR-0124 D5 had the leak scan allow them. The pre-flip scan of
   2026-10-15 counted 54 such references in the assembled tree — `evs-cmd/packd#N`,
   `evs-cmd/cairn#N` and GitHub issue/PR URLs under the `evs-cmd` organisation — in code comments,
   docs, tests, `pyproject.toml` and `Dockerfile.engine`.
2. Those trackers are private. A public reader who follows one gets a 404; a reference that
   cannot be opened tells them nothing, and it names a private organisation's repository.
3. The image names are different: `ghcr.io/evs-cmd/cairn-train-gpu` and its siblings are the
   images actually published, and the public tree's tests and docs refer to them as such.
   Renaming them would point at images that do not exist under `cairndev-sh`.

## Decision

**D1 — Private issue and PR references read as plain text in the public tree.** The assembler
(`scrub_issue_links`, after every file is written) rewrites them: a Markdown link keeps its
text and loses its URL; "(issue evs-cmd/…#N)" and a bare `evs-cmd/<repo>#N` or issue URL
become "an internal issue"; a pull request URL, or one written as "PR …" / "pull request …",
becomes "an internal pull request". A trailing `#fragment` goes with the link. ADRs are included:
only the reference changes, never the decision text.

**D2 — The leak scan flags any left.** Any GitHub URL (https or `git@`) under the `evs-cmd`
organisation and any `evs-cmd/<repo>#N` reference in the assembled tree is a leak, with no ADR exemption (ADR-0124 D5's issue-reference
allowance is withdrawn).

**D3 — Image names and the registry keep `evs-cmd`.** `ghcr.io/evs-cmd/…` stays where the
public tree names a published image (ADR-0123 D6, ADR-0124 D2 unchanged). The engine image the
public release workflow publishes is `ghcr.io/cairndev-sh/cairn-engine`, as before.

**D4 — The monorepo keeps its references.** Only the public tree is rewritten; the monorepo and
the platform repository keep the links, where they resolve.

## Consequences

- A public reader sees "an internal issue" where the monorepo has a link; the history behind it
  stays with the maintainers.
- After the flip (ADR-0132), new public code references public issues on `cairndev-sh/cairn`;
  the rewrite runs once more at the flip and is then retired with the assembler.

## Verification

- `tests/scripts/test_assemble_public_tree.py::test_private_issue_links_read_as_plain_text_and_images_keep_their_names`:
  the assembled tree has no private issue reference or private GitHub URL; `pyproject.toml`
  carries "an internal issue"; a probe file with both forms is flagged by the scan and fully
  rewritten by the scrub.
- An assembly from main: 0 leak hits; `ghcr.io/evs-cmd` image names still present.
