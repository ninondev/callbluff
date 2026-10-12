# SPDX-License-Identifier: GPL-3.0-only
import os
import sys
import unittest

from helpers import HookTestCase, run_hook, asst_text, asst_tool, HOOKS

sys.path.insert(0, HOOKS)
import guard_execution_check as ec  # noqa: E402

NEW_SYNTAX = (
    "class Box[T]:\n"
    "    def __init__(self, item: T) -> None:\n"
    "        self.item = item\n\n"
    "    def get(self) -> T:\n"
    "        return self.item\n\n\n"
    "def first[T](items: list[T]) -> T:\n"
    "    return items[0]\n\n\n"
    "type Pair[T] = tuple[T, T]\n"
)


class ExecutionCheckTests(HookTestCase):
    def stop(self, written, extra_rows=(), **payload):
        rows = [asst_tool("Write", {"file_path": p}) for p in written] + list(extra_rows)
        rows.append(asst_text("Done."))
        payload.setdefault("cwd", self.dir)
        return run_hook("guard_execution_check.py", dict(transcript_path=self.transcript(rows), **payload))

    def test_syntax_error_in_written_file_is_blocked(self):
        p = self.write("a.py", "def f(:\n    pass\n")
        code, _, err = self.stop([p])
        self.assertEqual(code, 2)
        self.assertIn("execution_check", err)
        self.assertIn("a.py", err)

    def test_undefined_name_in_written_file_is_blocked(self):
        p = self.write("b.py", "import os\n\ndef f():\n    return old_helper(os.getcwd())\n")
        code, _, err = self.stop([p])
        self.assertEqual(code, 2)
        self.assertIn("old_helper", err)

    def test_non_object_json_lines_in_transcript_do_not_crash(self):
        # a torn append can leave a line that parses to null or a number; the
        # gate must skip it, not crash on row.get
        path = self.transcript([])
        with open(path, "a", encoding="utf-8") as file:
            for line in ("null\n", "42\n", '"text"\n', "[1, 2]\n"):
                file.write(line)
        code, out, err = run_hook("guard_execution_check.py",
                                  dict(transcript_path=path, cwd=self.dir))
        self.assertEqual((code, err), (0, ""))

    def test_clean_file_passes(self):
        p = self.write("c.py", "import os\n\ndef f(x):\n    return [y for y in os.listdir(x)]\n")
        self.assertEqual(self.stop([p])[0], 0)

    def test_loop_guard(self):
        p = self.write("a.py", "def f(:\n")
        self.assertEqual(self.stop([p], stop_hook_active=True)[0], 0)

    def test_invalid_json_is_blocked(self):
        p = self.write("c.json", "{not json")
        code, _, err = self.stop([p])
        self.assertEqual(code, 2)
        self.assertIn("invalid JSON", err)

    def test_no_pyc_files_are_left_behind(self):
        p = self.write("d.py", "x = 1\n")
        self.stop([p])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "__pycache__")))

    def test_lambda_and_except_names_are_bound(self):
        p = self.write("e.py", "def f(items):\n    try:\n        return sorted(items, key=lambda x: x.k)\n"
                               "    except KeyError as err:\n        return str(err)\n")
        self.assertEqual(self.stop([p])[0], 0)

    def test_non_utf8_source_does_not_crash(self):
        p = os.path.join(self.dir, "g.py")
        with open(p, "wb") as f:
            f.write("# -*- coding: latin-1 -*-\n# caf\xe9\nx = 1\n".encode("latin-1"))
        self.assertEqual(ec.undefined_names(p), [])

    def test_subdirectory_session(self):
        # cwd below the repository root: edits to tracked files must still be seen.
        self.git("init", "-q")
        sub = os.path.join(self.dir, "sub")
        p = self.write("mod.py", "x = 1\n", base=sub)
        self.git("add", "sub/mod.py")
        self.git("commit", "-qm", "init")
        with open(p, "w") as f:
            f.write("y = undefined_thing\n")
        code, _, err = self.stop([p], cwd=sub)
        self.assertEqual(code, 2)
        self.assertIn("undefined_thing", err)


class RegressionPep695(HookTestCase):
    """Fix (a), part 1: Python 3.12+ syntax must not cause a false alarm."""

    @unittest.skipUnless(sys.version_info >= (3, 12), "needs Python 3.12+")
    def test_type_parameters_are_bound(self):
        p = self.write("generic.py", NEW_SYNTAX)
        code, _, err = run_hook("guard_execution_check.py", {
            "transcript_path": self.transcript([asst_tool("Write", {"file_path": p}), asst_text("ok")]),
            "cwd": self.dir})
        self.assertEqual((code, err), (0, ""))

    @unittest.skipIf(sys.version_info >= (3, 12), "only meaningful on an interpreter older than 3.12")
    def test_hook_on_an_old_interpreter_skips_new_syntax_files(self):
        p = self.write("generic.py", NEW_SYNTAX)
        code, _, err = run_hook("guard_execution_check.py", {
            "transcript_path": self.transcript([asst_tool("Write", {"file_path": p}), asst_text("ok")]),
            "cwd": self.dir})
        self.assertEqual((code, err), (0, ""))

    @unittest.skipUnless(sys.version_info >= (3, 12), "needs Python 3.12+")
    def test_undefined_name_next_to_new_syntax_is_still_found(self):
        p = self.write("generic.py", NEW_SYNTAX + "\nvalue = missing_name\n")
        self.assertEqual([n for n, _ in ec.undefined_names(p)], ["missing_name"])

    @unittest.skipUnless(sys.version_info >= (3, 10), "needs Python 3.10+")
    def test_match_captures_are_bound(self):
        p = self.write("m.py", "def f(cmd):\n    match cmd:\n        case [first, *rest]:\n"
                               "            return first, rest\n        case {'k': v, **others}:\n"
                               "            return v, others\n        case str() as s:\n            return s\n")
        self.assertEqual(ec.undefined_names(p), [])

    def test_old_interpreter_does_not_call_new_syntax_an_error(self):
        # A file with PEP 695 syntax plus a deliberate error: on a pretend 3.11 it is skipped,
        # on a pretend 3.13 the error is reported. This runs on any interpreter.
        p = self.write("old.py", "class C[T]:\n    pass\n\ndef broken(:\n")
        self.assertEqual(ec.check_python(p, "old.py", version=(3, 11)), [])
        self.assertEqual(len(ec.check_python(p, "old.py", version=(3, 13))), 1)

    def test_old_interpreter_still_reports_plain_syntax_errors(self):
        p = self.write("plain.py", "def broken(:\n")
        self.assertEqual(len(ec.check_python(p, "plain.py", version=(3, 11))), 1)

    def test_new_syntax_detector(self):
        for src in ("class C[T]:\n    pass\n", "def f[T](x: T) -> T:\n    return x\n",
                    "async def g[T]():\n    pass\n", "type Alias = int\n", "type L[T] = list[T]\n"):
            self.assertTrue(ec.uses_new_syntax(src), src)
        for src in ("class C(Base):\n    pass\n", "x = d['type']\n", "print('type X = 1')\n"):
            self.assertFalse(ec.uses_new_syntax(src), src)


class RegressionSessionFilesOnly(HookTestCase):
    """Fix (a), part 2: only files the current session wrote are checked."""

    def test_broken_file_from_another_session_is_ignored(self):
        self.git("init", "-q")
        mine = self.write("mine.py", "x = 1\n")
        other = self.write("other.py", "y = nothing_defines_this\n")  # untracked, written by someone else
        rows = [asst_tool("Write", {"file_path": mine}), asst_text("Done.")]
        code, _, err = run_hook("guard_execution_check.py", {
            "transcript_path": self.transcript(rows), "cwd": self.dir})
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(os.path.exists(other))

    def test_modified_tracked_file_from_another_session_is_ignored(self):
        self.git("init", "-q")
        other = self.write("other.py", "y = 1\n")
        self.git("add", "other.py")
        self.git("commit", "-qm", "init")
        with open(other, "w") as f:
            f.write("y = nothing_defines_this\n")
        rows = [asst_text("Nothing to do.")]
        code, _, _ = run_hook("guard_execution_check.py", {
            "transcript_path": self.transcript(rows), "cwd": self.dir})
        self.assertEqual(code, 0)

    def test_broken_file_the_session_wrote_is_still_caught(self):
        self.git("init", "-q")
        mine = self.write("mine.py", "y = nothing_defines_this\n")
        rows = [asst_tool("Edit", {"file_path": mine}), asst_text("Done.")]
        code, _, err = run_hook("guard_execution_check.py", {
            "transcript_path": self.transcript(rows), "cwd": self.dir})
        self.assertEqual(code, 2)
        self.assertIn("nothing_defines_this", err)

    def test_relative_paths_in_the_transcript_resolve_against_cwd(self):
        self.write("rel.py", "z = nothing_defines_this\n")
        rows = [asst_tool("Edit", {"file_path": "rel.py"}), asst_text("Done.")]
        code, _, _ = run_hook("guard_execution_check.py", {
            "transcript_path": self.transcript(rows), "cwd": self.dir})
        self.assertEqual(code, 2)

    def test_no_transcript_means_no_check(self):
        self.write("rel.py", "z = nothing_defines_this\n")
        self.assertEqual(run_hook("guard_execution_check.py", {"cwd": self.dir})[0], 0)


if __name__ == "__main__":
    unittest.main()
