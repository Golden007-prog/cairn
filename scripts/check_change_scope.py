"""Small-change scope/budget checks. Local hooks are advisory; --strict fails CLI checks.

Budgets count CODE lines (maintainer rule, 2026-10-06, #2284): for ``.py`` files only lines
that carry a code token count (blank, comment-only and docstring lines do not); Markdown
documentation never counts; executable Markdown (flows/templates/skills) does. Raw counts are reported beside.
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import re
import tokenize
import subprocess
import sys
from pathlib import Path


def git(root: Path, *args: str) -> str:
    """Run git without shell interpretation."""
    return subprocess.check_output(["git", "-C", str(root), *args], text=True)


def load_plan(path: Path) -> dict:
    """Validate explicit file scope and budgets; a plan is not approval evidence."""
    plan = json.loads(path.read_text())
    if not isinstance(plan, dict) or not isinstance(plan.get("base"), str):
        raise ValueError("plan must be an object with a string base")
    if not re.fullmatch(r"[0-9a-f]{40}", plan["base"]):
        raise ValueError("base must be a full commit SHA")
    files = plan.get("files")
    if not isinstance(files, list) or not files or not all(isinstance(p, str) for p in files):
        raise ValueError("files must be a nonempty list of repository-relative paths")
    for name in files:
        p = Path(name)
        if p.is_absolute() or ".." in p.parts or name != p.as_posix() or name == ".":
            raise ValueError(f"invalid scope path: {name}")
    for key in ("max_added", "max_deleted", "max_new_files"):
        if type(plan.get(key)) is not int or plan[key] < 0:
            raise ValueError(f"{key} must be a nonnegative integer")
    return plan


def changes(root: Path, base: str) -> dict[str, tuple[int, int]]:
    """Count the whole diff, including staged/unstaged edits and untracked files."""
    result = {}
    raw = git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--numstat", "-z", base, "--")
    for row in filter(None, raw.split("\0")):
        added, deleted, name = row.split("\t", 2)
        if added == "-" or deleted == "-":
            raise ValueError(f"binary change requires separate review: {name}")
        result[name] = (int(added), int(deleted))
    for name in filter(None, git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")):
        path = root / name
        if path.is_symlink() or path.stat().st_size > 2_000_000:
            raise ValueError(f"untracked symlink/large file requires separate review: {name}")
        text = path.read_text()
        if "\0" in text:
            raise ValueError(f"untracked binary requires separate review: {name}")
        result[name] = (len(text.splitlines()), 0)
    return result


_EXECUTABLE_MD = {"flows", "templates", "skills"}
_NOT_CODE = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
             tokenize.ENCODING, tokenize.ENDMARKER}


def _docstring_starts(tree: ast.AST) -> set[tuple[int, int]]:
    """(line, col) of every module/class/function docstring expression."""
    starts = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                starts.add((first.lineno, first.col_offset))
    return starts


def code_lines(text: str) -> set[int]:
    """1-based lines of a Python source that carry CODE: at least one token that is not a
    comment, blank/structural token or the docstring itself. A trailing comment does not
    hide its line; a ``#`` inside a string is string content; a file that does not parse
    counts every line (conservative). Lines are numbered the way git, tokenize and ast
    number them (``\n`` only)."""
    total = text.count("\n") + (not text.endswith("\n") and bool(text))
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
        docstrings = _docstring_starts(ast.parse(text))
    except (SyntaxError, tokenize.TokenError, ValueError):
        return set(range(1, total + 1))
    lines: set[int] = set()
    for tok in tokens:
        if tok.type in _NOT_CODE or (tok.type == tokenize.STRING and tok.start in docstrings):
            continue
        lines.update(range(tok.start[0], tok.end[0] + 1))
    return lines


def hunk_lines(root: Path, base: str, name: str) -> tuple[set[int], set[int]]:
    """(old line numbers deleted, new line numbers added) for a tracked file's diff."""
    raw = git(root, "diff", "--no-ext-diff", "--no-renames", "-U0", base, "--", name)
    old, new = set(), set()
    for m in re.finditer(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", raw, re.M):
        old.update(range(int(m.group(1)), int(m.group(1)) + int(m.group(2) or 1)) if m.group(2) != "0" else ())
        new.update(range(int(m.group(3)), int(m.group(3)) + int(m.group(4) or 1)) if m.group(4) != "0" else ())
    return old, new


def code_counts(root: Path, base: str, name: str, raw: tuple[int, int], tracked: bool) -> tuple[int, int]:
    """Added/deleted CODE lines for one file. Markdown documentation never counts — but
    executable Markdown (flow templates under ``flows/`` or the legacy ``templates/``
    alias, skill prompts under ``skills/``) is behaviour and counts raw; a ``.py`` file
    keeps only its code lines on each side; every other file counts raw."""
    if name.endswith(".md"):
        return raw if _EXECUTABLE_MD & set(Path(name).parts) else (0, 0)
    if not name.endswith(".py"):
        return raw
    path = root / name
    new_text = path.read_text() if path.exists() else ""
    if not tracked:
        return (len(code_lines(new_text)), 0)
    old_lines, new_lines = hunk_lines(root, base, name)
    try:
        old_text = git(root, "show", f"{base}:{name}")
    except subprocess.CalledProcessError:
        old_text = ""
    return (len(new_lines & code_lines(new_text)), len(old_lines & code_lines(old_text)))


def check(root: Path, plan: dict, pending: list[str] | None = None) -> dict:
    """Return measured totals and violations; never mutate files or run project code."""
    diff = changes(root, plan["base"])
    baseline = set(git(root, "ls-tree", "-r", "--name-only", "-z", plan["base"]).split("\0"))
    violations = []
    names = set(diff)
    for name in pending or []:
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            violations.append(f"path escapes repository: {name}")
        else:
            names.add(path.relative_to(root).as_posix())
    for name in sorted(names - set(plan["files"])):
        violations.append(f"outside plan: {name}")
    code = {name: code_counts(root, plan["base"], name, raw, name in baseline) for name, raw in diff.items()}
    totals = {"added": sum(a for a, _ in code.values()),
              "deleted": sum(d for _, d in code.values()),
              "new_files": sum(name not in baseline for name in diff),
              "raw_added": sum(a for a, _ in diff.values()),
              "raw_deleted": sum(d for _, d in diff.values())}
    for key in ("added", "deleted", "new_files"):
        if totals[key] > plan[f"max_{key}"]:
            suffix = f" (code lines; raw {totals['raw_' + key]})" if key != "new_files" else ""
            violations.append(f"{key}: {totals[key]} exceeds {plan[f'max_{key}']}{suffix}")
    return {"totals": totals, "files": diff, "code": code, "violations": violations}


def pending_paths(event: dict) -> list[str]:
    """Recognize native edit inputs; arbitrary shell/MCP writes need post-checks."""
    inp = event.get("tool_input", {})
    if not isinstance(inp, dict):
        return []
    if event.get("tool_name") == "apply_patch":
        patch = inp.get("command", "")
        return re.findall(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", patch, re.M)
    name = inp.get("file_path")
    return [name] if isinstance(name, str) else []


def hook_message(event: dict, message: str) -> dict:
    """Use common advisory output; never claim to block an already-completed edit."""
    kind = event.get("hook_event_name", "")
    if kind in ("SessionStart", "PreToolUse", "PostToolUse"):
        return {"hookSpecificOutput": {"hookEventName": kind, "additionalContext": message}}
    return {"systemMessage": message}


def main() -> int:
    """CLI strict checks and advisory host hooks share the same evaluator."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--base", help="CI merge-base override; never trust a PR's chosen base")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--hook", action="store_true")
    args = parser.parse_args()
    event = {}
    try:
        event = json.load(sys.stdin) if args.hook else {}
        root = Path(git(Path(event.get("cwd", Path.cwd())), "rev-parse", "--show-toplevel").strip()).resolve()
        path = args.plan or Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "cairn-change.json").strip())
        if not path.exists():
            message = "No change plan. Before code edits, follow docs/architecture/17-issue-workflow.md#small-change-discipline."
            if args.hook:
                # Avoid repeating a reminder on every read-only tool call.
                if event.get("hook_event_name") == "SessionStart" or pending_paths(event):
                    print(json.dumps(hook_message(event, message)))
                return 0
            raise ValueError(message)
        plan = load_plan(path)
        if args.base:
            if not re.fullmatch(r"[0-9a-f]{40}", args.base):
                raise ValueError("--base must be a full commit SHA")
            plan["base"] = args.base
        pending = [str(Path(event.get("cwd", root)) / name) for name in pending_paths(event)]
        result = check(root, plan, pending)
        if args.hook:
            if result["violations"]:
                print(json.dumps(hook_message(event, "Scope check: " + "; ".join(result["violations"]))))
        else:
            print(json.dumps(result, indent=2))
        return int(args.strict and not args.hook and bool(result["violations"]))
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        message = f"Change-scope check unavailable: {exc}"
        if args.hook:
            print(json.dumps(hook_message(event, message)))
            return 0
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
