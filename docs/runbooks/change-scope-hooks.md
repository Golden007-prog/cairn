# Small-change hooks: setup and limits

> Since #2284 budgets count **code** lines: for `.py` only lines carrying a code token (blank,
> comment-only and docstring lines are free); Markdown documentation is free, executable
> Markdown (`flows/`, the legacy `templates/` alias, `skills/`) counts; everything else counts raw. The JSON reports `added`/`deleted`
> (code) beside `raw_added`/`raw_deleted`.

The policy lives in [the issue workflow](../architecture/17-issue-workflow.md#small-change-discipline).
This runbook describes the first, report-only rollout, not complete merge enforcement.

## Locations

| Host / component | Repository file | Responsibility |
|---|---|---|
| Codex | `.codex/hooks.json` | Registers SessionStart, PreToolUse, PostToolUse and Stop |
| Claude Code | `.claude/settings.json` | Registers the same lifecycle checks |
| Shared checker | `scripts/check_change_scope.py` | Validates explicit paths and measures cumulative diff budgets |
| Regression tests | `tests/core/test_change_scope.py` | Rejecting/accepting cases using temporary Git repositories |
| CI | `.github/workflows/change-scope.yml` | Tests the checker and reports optional PR plans; not a required merge gate |

Hook commands use Python 3.10+ and Git, with no additional package. They run from inside
the intended repository; `cwd` must identify that checkout. Never run repository hooks from
an untrusted checkout. These files do not automatically activate a host's native Plan mode.

Codex discovers project hooks in its trusted project configuration layer and requires
review/trust for new or changed non-managed hooks. Inspect `/hooks` in the CLI or the
available host settings. Claude loads project hooks through its project settings; inspect
`/hooks` there. Restart/reload if the installed host requires it. Do not bypass hook trust.
The registrations have fixture-level tests, not live installed-host activation proof.

## Per-change plan

Use an isolated checkout with a clean starting state. Do not hide unrelated existing work
with broad exclusions. For a dirty checkout, isolate the task or explicitly reconcile the
whole measured diff before relying on a budget. The checker counts changes from the base,
including staged, unstaged and non-ignored untracked files; it cannot attribute shared edits
to a particular agent. Ignored files are outside this inventory and are not protected by it.

Place a plan at the path returned by `git rev-parse --git-path cairn-change.json` (worktree
specific Git metadata), or pass `--plan /absolute/path.json`. Example:

```json
{
  "base": "REPLACE_WITH_FULL_40_CHARACTER_BASE_COMMIT_SHA",
  "files": ["cairn/example.py", "tests/core/test_example.py"],
  "max_added": 80,
  "max_deleted": 30,
  "max_new_files": 0
}
```

Numbers are an illustration, not repository-wide limits. Choose them from the actual plan.
Paths are explicit, repository-relative, without glob patterns. The Markdown implementation
plan separately records source/test/generated estimates, complexity, dependencies,
abstractions, proof commands and existing authorization. There is deliberately no
`approved: true` field; the plan is not an approval credential.

```bash
python3 scripts/check_change_scope.py --plan /absolute/path.json
python3 scripts/check_change_scope.py --plan /absolute/path.json --strict
python3 -m unittest discover -s tests/core -p test_change_scope.py -v
```

Strict CLI returns nonzero for missing/invalid plans, unsupported inventory, out-of-scope
files and exceeded budgets. `--base FULL_SHA` overrides the plan's base for CI. Renames
count as removal/addition. Binary diffs, untracked symlinks and untracked files larger than
2 MB require separate review; they are not silently excluded. No files are rewritten,
reverted or auto-formatted, and project commands are never executed by the checker.

For CI reporting, an optional reviewed `.github/change-plan.json` carries the same shape.
The workflow derives the merge-base independently. It does not trust the PR plan as proof
of approval, and report-only output is not an architecture verdict.

## What each hook does

- **SessionStart:** reminds the agent when no plan exists; reports existing deviations.
- **PreToolUse:** recognizes native `file_path` and Codex `apply_patch` paths (including
  moves) and reports paths outside the plan. It cannot predict arbitrary shell/MCP writes.
- **PostToolUse:** compares the actual cumulative Git diff with the plan, catching visible
  shell-created files after the fact. It cannot undo a tool's effects.
- **Stop:** reports remaining scope problems without trapping a blocked user conversation
  in a retry loop. It does not certify tests or force an unsupported success claim.

All host hooks are advisory, even if `--strict` is accidentally supplied with `--hook`.
A missing plan prompts planning on session start or recognized native writes. No-plan
read-only calls remain quiet. Checker errors are reported as unavailable, never PASS.

## Next blocking rollout requires proof

Before claiming enforcement, qualify native patches, file tools, shell scripts, MCP writes,
subagents, tool failures and alternate working directories on both installed hosts. Prove
path traversal/symlink refusal and unknown-input behavior. Restrict unsupported mutation
paths using host permissions/sandboxing, not a shell-command regex. Hook timeouts and
configuration bypasses must be tested; an agent able to edit a hook can bypass it.

A required CI gate needs protected checker/policy ownership, trusted exception approval,
review dismissal after changes and server-side branch protection. Test-result receipts must
bind an actual command/exit status to the checked source/configuration snapshot before
stale-test enforcement is claimed. Add measured complexity checks using the existing
language tooling rather than inventing an approximate AST score. Do not install an LLM
judge in per-edit hooks. These capabilities are not part of this initial report-only checker.

Sources checked 2026-09-22: [Codex hooks](https://learn.chatgpt.com/docs/hooks),
[Claude hooks](https://code.claude.com/docs/en/hooks). Exact event coverage depends on the
installed host/version; neither configuration file alone proves activation.
