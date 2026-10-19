"""Hermetic rejecting cases for the scope checker; no host or provider needed."""
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check_change_scope.py"
spec = importlib.util.spec_from_file_location("change_scope", SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.root / "a.py").write_text("x = 1\n")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        self.plan = {"base": self.git("rev-parse", "HEAD").strip(), "files": ["a.py"],
                     "max_added": 3, "max_deleted": 2, "max_new_files": 0}

    def git(self, *args):
        return guard.git(self.root, *args)

    def test_targeted_edit_passes(self):
        (self.root / "a.py").write_text("x = 2\n")
        self.assertEqual(guard.check(self.root, self.plan)["violations"], [])

    def test_untracked_file_and_new_file_budget_fail(self):
        (self.root / "extra.py").write_text("x = 3\n")
        errors = guard.check(self.root, self.plan)["violations"]
        self.assertIn("outside plan: extra.py", errors)
        self.assertIn("new_files: 1 exceeds 0", "; ".join(errors))

    def test_staged_and_unstaged_growth_count_together(self):
        (self.root / "a.py").write_text("x = 1\n" * 3)
        self.git("add", ".")
        (self.root / "a.py").write_text("x = 1\n" * 6)
        self.assertIn("added: 5 exceeds 3", "; ".join(guard.check(self.root, self.plan)["violations"]))

    def test_budgets_count_code_lines_not_docstrings_comments_or_markdown(self):
        """#2284 — the maintainer rule: code lines (source + tests) count; comments,
        docstrings and Markdown do not. Rejecting case: a fourth CODE line fails."""
        self.plan["files"] += ["NOTES.md"]
        self.plan["max_new_files"] = 1
        (self.root / "NOTES.md").write_text("# forty lines of prose\n" * 40)
        (self.root / "a.py").write_text('"""A module docstring\n\nthat spans lines.\n"""\n'
                                        "# a comment\nx = 1\n\n\ndef f():\n"
                                        '    """Docstring."""\n    # inner comment\n    return 1\n')
        result = guard.check(self.root, self.plan)
        self.assertEqual(result["violations"], [], result)
        self.assertEqual(result["totals"]["added"], 2)              # `def f():` and `return 1`
        self.assertGreater(result["totals"]["raw_added"], 10)
        self.assertEqual(result["code"]["NOTES.md"], (0, 0))
        (self.root / "a.py").write_text((self.root / "a.py").read_text() + "y = 2\nz = 3\n")
        self.assertIn("added: 4 exceeds 3", "; ".join(guard.check(self.root, self.plan)["violations"]))

    def test_deleting_a_docstring_is_not_a_deletion_and_a_broken_file_counts_raw(self):
        (self.root / "a.py").write_text('"""doc"""\nx = 1\n')
        self.git("add", ".")
        self.git("commit", "-qm", "with docstring")
        self.plan["base"] = self.git("rev-parse", "HEAD").strip()
        (self.root / "a.py").write_text("x = 1\n")
        self.assertEqual(guard.check(self.root, self.plan)["totals"]["deleted"], 0)
        (self.root / "a.py").write_text("def broken(:\n    pass\n# c\n\n")
        self.assertEqual(guard.check(self.root, self.plan)["totals"]["added"], 4)   # unparsable: raw, nothing excluded

    def test_a_trailing_comment_does_not_hide_a_code_line(self):
        """Review F1 — `x = 1  # noqa` is code. Also: `#` inside a string is string
        content, code sharing a line with a docstring counts, and a flow template is code."""
        text = ('"""doc"""; import os\n'                       # the import counts (shares the docstring line)
                'x = 1  # noqa: E501\n'
                'SPEC = """\n# not a comment, yaml\nkey: v\n"""\n'
                '# only a comment\n\n')
        self.assertEqual(guard.code_lines(text), {1, 2, 3, 4, 5, 6})
        self.assertEqual(guard.code_lines(""), set())
        (self.root / "a.py").write_text("x = 1\n" + "".join(f"v{i} = {i}  # set\n" for i in range(4)))
        self.assertIn("added: 4 exceeds 3", "; ".join(guard.check(self.root, self.plan)["violations"]))
        self.plan["files"] += ["flows/t.md", "p/skills/s.md", "docs/t.md"]
        self.plan["max_new_files"] = 3
        for d in ("flows", "p/skills", "docs"):
            (self.root / d).mkdir(parents=True)
        (self.root / "flows/t.md").write_text("---\nflow: x\n---\n")
        (self.root / "p/skills/s.md").write_text("prompt\n")
        (self.root / "docs/t.md").write_text("prose\n" * 9)
        result = guard.check(self.root, {**self.plan, "max_added": 8})
        self.assertEqual([result["code"][n] for n in ("flows/t.md", "p/skills/s.md", "docs/t.md")], [(3, 0), (1, 0), (0, 0)])

    def test_untracked_binary_is_not_silently_counted(self):
        (self.root / "data.bin").write_bytes(b"a\0b")
        with self.assertRaises(ValueError):
            guard.check(self.root, self.plan)

    def test_deleted_file_is_scoped(self):
        (self.root / "a.py").unlink()
        self.plan["files"] = ["other.py"]
        self.assertIn("outside plan: a.py", guard.check(self.root, self.plan)["violations"])

    def test_pending_move_and_outside_root(self):
        event = {"tool_name": "apply_patch", "tool_input": {"command":
                 "*** Update File: a.py\n*** Move to: b.py\n"}}
        self.assertEqual(guard.pending_paths(event), ["a.py", "b.py"])
        errors = guard.check(self.root, self.plan, ["../outside.py", "b.py"])["violations"]
        self.assertEqual(len(errors), 2)

    def test_plan_validation_rejects_traversal_and_boolean_budget(self):
        path = self.root / ".git/plan.json"
        for key, value in [("files", ["../bad"]), ("max_added", True)]:
            path.write_text(json.dumps({**self.plan, key: value}))
            with self.assertRaises(ValueError):
                guard.load_plan(path)

    def test_strict_cli_rejects_missing_and_out_of_scope_plan(self):
        path = self.root / ".git/plan.json"
        command = [str(Path(__import__('sys').executable)), str(SCRIPT), "--strict", "--plan", str(path)]
        self.assertNotEqual(subprocess.run(command, cwd=self.root, capture_output=True).returncode, 0)
        path.write_text(json.dumps(self.plan))
        (self.root / "extra.py").write_text("x = 3\n")
        self.assertEqual(subprocess.run(command, cwd=self.root, capture_output=True).returncode, 1)

    def test_malformed_plan_types_fail_cleanly(self):
        path = self.root / ".git/plan.json"
        for value in ([], {"base": 3}, {"base": None}):
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                guard.load_plan(path)

    def test_subdirectory_hook(self):
        # the host registrations (.claude/settings.json, .codex/hooks.json) are agent files,
        # private for now: test_agent_files_single_source.py checks them (#2424)
        (self.root / "sub").mkdir()
        self.plan["files"].append("sub/new.py")
        (self.root / ".git/cairn-change.json").write_text(json.dumps(self.plan))
        event = {"cwd": str(self.root / "sub"), "hook_event_name": "PreToolUse",
                 "tool_name": "Write", "tool_input": {"file_path": "new.py"}}
        result = subprocess.run([__import__('sys').executable, str(SCRIPT), "--hook"],
                                input=json.dumps(event), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_hook_is_advisory_and_reports_pending_write(self):
        event = {"cwd": str(self.root), "hook_event_name": "PreToolUse",
                 "tool_name": "Write", "tool_input": {"file_path": "new.py"}}
        result = subprocess.run([__import__('sys').executable, str(SCRIPT), "--hook"],
                                input=json.dumps(event), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("No change plan", json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"])


if __name__ == "__main__":
    unittest.main()
