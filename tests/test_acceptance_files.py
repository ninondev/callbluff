# SPDX-License-Identifier: MIT
import json
import os
import tempfile
import unittest

from helpers import HookTestCase, run_hook, asst_text, asst_tool, tool_result


class AcceptanceFileGuardTests(HookTestCase):
    def call(self, tool, tool_input, rows=None, **env):
        t = self.transcript(rows or [asst_text("working", model="claude-haiku-4-5")])
        return run_hook("guard_acceptance_files.py", {
            "tool_name": tool, "tool_input": tool_input, "cwd": self.dir, "transcript_path": t}, env)

    def decision(self, out):
        return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out.strip() else None

    def test_existing_test_file_asks(self):
        p = self.write("tests/test_a.py", "def test_x():\n    assert 1\n")
        code, out, _ = self.call("Edit", {"file_path": p, "old_string": "1", "new_string": "2"})
        self.assertEqual(code, 0)
        self.assertEqual(self.decision(out), "ask")
        self.assertIn("acceptance_file_guard", out)

    def test_ordinary_source_file_passes(self):
        p = self.write("src/app.py", "x = 1\n")
        self.assertEqual(self.call("Edit", {"file_path": p, "old_string": "1", "new_string": "2"})[1], "")

    def test_bash_append_to_existing_test_file_asks(self):
        self.write("tests/test_a.py", "x = 1\n")
        _, out, _ = self.call("Bash", {"command": "cat >> tests/test_a.py <<'EOF'\nassert 1\nEOF"})
        self.assertEqual(self.decision(out), "ask")

    def test_read_only_bash_passes(self):
        self.write("tests/test_a.py", "x = 1\n")
        self.assertEqual(self.call("Bash", {"command": "cat tests/test_a.py"})[1], "")

    def test_custom_globs(self):
        self.write(".callbluff.json", json.dumps({"protected_globs": ["golden/*"]}))
        gold = self.write("golden/a.txt", "x\n")
        plain = self.write("tests/test_b.py", "x\n")
        self.assertEqual(self.decision(self.call("Edit", {"file_path": gold, "old_string": "x", "new_string": "y"})[1]), "ask")
        self.assertEqual(self.call("Edit", {"file_path": plain, "old_string": "x", "new_string": "y"})[1], "")

    def test_cooldown_per_file(self):
        p = self.write("tests/test_a.py", "x = 1\n")
        first = self.call("Edit", {"file_path": p, "old_string": "1", "new_string": "2"})[1]
        self.assertEqual(self.decision(first), "ask")
        signature = json.loads(first)["hookSpecificOutput"]["permissionDecisionReason"]
        rows = [asst_text("working", model="claude-haiku-4-5"), asst_text(signature)]
        second = self.call("Edit", {"file_path": p, "old_string": "1", "new_string": "2"}, rows=rows)[1]
        self.assertEqual(second, "")


class PrecisionModeTests(HookTestCase):
    """Strong hosts: ask only when a failing test is weakened right after a red run."""

    def call(self, tool_input, rows, tool="Edit"):
        t = self.transcript(rows)
        return run_hook("guard_acceptance_files.py", {
            "tool_name": tool, "tool_input": tool_input, "cwd": self.dir, "transcript_path": t})

    def host(self):
        return asst_text("ok", model="claude-opus-5")

    def test_adding_a_test_is_silent(self):
        p = self.write("tests/test_a.py", "def test_a():\n    assert 1\n")
        out = self.call({"file_path": p, "old_string": "assert 1",
                         "new_string": "assert 1\n\ndef test_b():\n    assert 2"}, [self.host()])[1]
        self.assertEqual(out, "")

    def test_weakening_after_a_red_run_asks(self):
        p = self.write("tests/test_a.py", "def test_a():\n    assert 1 == 2\n")
        rows = [self.host(), asst_tool("Bash", {"command": "python3 -m pytest tests/test_a.py"}),
                tool_result("FAILED tests/test_a.py::test_a - AssertionError")]
        out = self.call({"file_path": p, "old_string": "assert 1 == 2", "new_string": "pass"}, rows)[1]
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_weakening_without_a_red_run_is_only_logged(self):
        p = self.write("tests/test_a.py", "def test_a():\n    assert 1 == 2\n")
        home = os.path.join(self.dir, "home")
        t = self.transcript([self.host()])
        code, out, _ = run_hook("guard_acceptance_files.py", {
            "tool_name": "Edit", "tool_input": {"file_path": p, "old_string": "assert 1 == 2", "new_string": "pass"},
            "cwd": self.dir, "transcript_path": t}, env={"CALLBLUFF_NO_FIRELOG": "", "CALLBLUFF_HOME": home})
        self.assertEqual((code, out), (0, ""))
        with open(os.path.join(home, "firelog.jsonl")) as f:
            rows = [json.loads(x) for x in f]
        self.assertEqual([r.get("mode") for r in rows], ["shadow"])

    def test_new_skip_marker_after_red_run_asks(self):
        p = self.write("tests/test_a.py", "def test_a():\n    assert 1 == 2\n")
        rows = [self.host(), asst_tool("Bash", {"command": "pytest tests/test_a.py"}), tool_result("1 failed")]
        out = self.call({"file_path": p, "old_string": "def test_a", "new_string": "@pytest.mark.skip\ndef test_a"}, rows)[1]
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "ask")


class RegressionNewFilesAndScratch(HookTestCase):
    """Fix (b): no ask and no log entry for new test files and for scratch directories."""

    def run_logged(self, tool, tool_input, model="claude-haiku-4-5", cwd=None):
        home = os.path.join(self.dir, "home")
        t = self.transcript([asst_text("x", model=model)])
        code, out, err = run_hook("guard_acceptance_files.py", {
            "tool_name": tool, "tool_input": tool_input, "cwd": cwd or self.dir, "transcript_path": t},
            env={"CALLBLUFF_NO_FIRELOG": "", "CALLBLUFF_HOME": home})
        log = os.path.join(home, "firelog.jsonl")
        lines = []
        if os.path.exists(log):
            with open(log) as f:
                lines = f.read().splitlines()
        return code, out, lines

    def test_new_test_file_by_write_is_silent_and_unlogged(self):
        for model in ("claude-haiku-4-5", "claude-opus-5"):
            with self.subTest(model=model):
                p = os.path.join(self.dir, "tests", "test_new.py")
                code, out, lines = self.run_logged("Write", {"file_path": p, "content": "def test_x():\n    assert 1\n"}, model)
                self.assertEqual((code, out, lines), (0, "", []))

    def test_new_test_file_by_shell_is_silent_and_unlogged(self):
        for model in ("claude-haiku-4-5", "claude-opus-5"):
            with self.subTest(model=model):
                code, out, lines = self.run_logged("Bash", {"command": "cat > tests/test_new.py <<'EOF'\nassert 1\nEOF"}, model)
                self.assertEqual((code, out, lines), (0, "", []))

    def test_existing_test_in_a_scratch_directory_is_silent_and_unlogged(self):
        for model in ("claude-haiku-4-5", "claude-opus-5"):
            for folder in ("scratch", "tmp", ".scratch"):
                with self.subTest(model=model, folder=folder):
                    p = self.write(f"{folder}/test_copy.py", "def test_x():\n    assert 1\n")
                    code, out, lines = self.run_logged(
                        "Edit", {"file_path": p, "old_string": "assert 1", "new_string": "pass"}, model)
                    self.assertEqual((code, out, lines), (0, "", []))

    def test_existing_test_in_the_system_temp_directory_is_silent_and_unlogged(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "test_copy.py")
            with open(p, "w") as f:
                f.write("def test_x():\n    assert 1\n")
            for model in ("claude-haiku-4-5", "claude-opus-5"):
                with self.subTest(model=model):
                    code, out, lines = self.run_logged(
                        "Edit", {"file_path": p, "old_string": "assert 1", "new_string": "pass"}, model, cwd=tmp)
                    self.assertEqual((code, out, lines), (0, "", []))

    def test_shell_redirect_into_temp_directory_is_silent(self):
        code, out, lines = self.run_logged("Bash", {"command": "python3 -m pytest > /tmp/test_output.txt"})
        self.assertEqual((code, out, lines), (0, "", []))

    def test_a_project_that_lives_under_a_folder_called_tmp_is_still_guarded(self):
        # The folder-name rule looks only below the project directory.
        proj = os.path.join(self.dir, "tmp", "app")
        p = self.write("tests/test_a.py", "x = 1\n", base=proj)
        t = self.transcript([asst_text("x", model="claude-haiku-4-5")])
        code, out, _ = run_hook("guard_acceptance_files.py", {
            "tool_name": "Edit", "tool_input": {"file_path": p, "old_string": "1", "new_string": "2"},
            "cwd": proj, "transcript_path": t})
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_existing_test_file_still_asks_and_is_logged(self):
        p = self.write("tests/test_a.py", "def test_x():\n    assert 1\n")
        code, out, lines = self.run_logged("Edit", {"file_path": p, "old_string": "assert 1", "new_string": "pass"})
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertEqual(len(lines), 1)


if __name__ == "__main__":
    unittest.main()
