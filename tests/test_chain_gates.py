# SPDX-License-Identifier: MIT
"""pilot_first, consumer_sweep, anchor_regression."""
import json
import os
import unittest

from helpers import HookTestCase, run_hook, asst_text, asst_tool, tool_result


class PilotFirstTests(HookTestCase):
    def call(self, rows, target):
        return run_hook("guard_pilot_first.py", {
            "transcript_path": self.transcript(rows), "tool_input": {"file_path": target}})

    def writes(self, n, ext="py"):
        return [asst_tool("Edit", {"file_path": f"/w/mod_{i}.{ext}"}) for i in range(1, n + 1)]

    def test_unverified_batch_is_denied(self):
        _, out, _ = self.call(self.writes(3), "/w/mod_9.py")
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_verified_batch_passes(self):
        rows = self.writes(3) + [asst_tool("Bash", {"command": "python3 -m pytest"})]
        self.assertEqual(self.call(rows, "/w/mod_9.py")[1], "")

    def test_different_shapes_pass(self):
        rows = [asst_tool("Edit", {"file_path": f"/w/{n}.py"}) for n in ("a", "b", "c")]
        self.assertEqual(self.call(rows, "/w/d.py")[1], "")

    def test_prose_files_are_exempt(self):
        rows = [asst_tool("Edit", {"file_path": f"/w/draft_v{i}.md"}) for i in range(1, 6)]
        self.assertEqual(self.call(rows, "/w/draft_v9.md")[1], "")

    def test_cooldown(self):
        _, out, _ = self.call(self.writes(3), "/w/mod_9.py")
        reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertEqual(self.call(self.writes(3) + [asst_text(reason)], "/w/mod_9.py")[1], "")

    def test_strong_host_runs_in_shadow_mode(self):
        rows = [asst_text("ok", model="claude-opus-5")] + self.writes(3)
        self.assertEqual(self.call(rows, "/w/mod_9.py"), (0, "", ""))

    def test_weak_host_is_denied(self):
        rows = [asst_text("ok", model="claude-haiku-4-5")] + self.writes(3)
        self.assertIn("deny", self.call(rows, "/w/mod_9.py")[1])


class ConsumerSweepTests(HookTestCase):
    def hub_repo(self, doc=False, touch_metrics=False, touch_loader=False, only_util=False):
        d = self.dir
        self.write("kinds.py", 'KINDS = ["draft", "live", "done"]\n')
        self.write("check.py", "from kinds import KINDS\n\ndef ok(k):\n    return k in KINDS\n")
        self.write("count.py", "from kinds import KINDS\n\ndef tally(rows):\n    return {k: sum(1 for r in rows if r == k) for k in KINDS}\n")
        self.write("metrics.py", "from kinds import KINDS\n\nFINAL_IDX = 2\n\ndef is_final(k):\n    return KINDS.index(k) >= FINAL_IDX\n")
        self.write("loader.py", "from kinds import KINDS\n\ndef decode(codes):\n    return [KINDS[c] for c in codes]\n")
        self.write("util.py", "def pad(x):\n    return x + 1\n")
        self.write("README.md", "# Kinds\n\n| kind | meaning |\n|---|---|\n| draft | a draft |\n| live | in use |\n| done | finished |\n")
        self.git("init", "-q")
        self.git("add", "kinds.py", "check.py", "count.py", "metrics.py", "loader.py", "util.py", "README.md")
        self.git("commit", "-qm", "init")
        if only_util:
            with open(os.path.join(d, "util.py"), "a") as f:
                f.write("\ndef pad2(x):\n    return x + 2\n")
            return d
        self.write("kinds.py", 'KINDS = ["draft", "live", "done", "gone"]\n')
        if doc:
            with open(os.path.join(d, "README.md"), "a") as f:
                f.write("| gone | removed |\n")
        for flag, name in ((touch_metrics, "metrics.py"), (touch_loader, "loader.py")):
            if flag:
                with open(os.path.join(d, name), "a") as f:
                    f.write("# checked: the new value is appended at the tail\n")
        return d

    def run_sweep(self, d):
        return run_hook("guard_consumer_sweep.py", {"cwd": d})

    def test_doc_not_updated_is_blocked(self):
        code, _, err = self.run_sweep(self.hub_repo())
        self.assertEqual(code, 2)
        self.assertIn("consumer_sweep", err)
        self.assertIn("README", err)

    def test_positional_reads_found_even_when_the_doc_changed(self):
        code, _, err = self.run_sweep(self.hub_repo(doc=True))
        self.assertEqual(code, 2)
        self.assertIn("metrics.py", err)
        self.assertIn("Positional", err)

    def test_everything_checked_is_quiet(self):
        code, _, err = self.run_sweep(self.hub_repo(doc=True, touch_metrics=True, touch_loader=True))
        self.assertEqual((code, err.strip()), (0, ""))

    def test_non_hub_change_is_quiet(self):
        code, _, err = self.run_sweep(self.hub_repo(only_util=True))
        self.assertEqual((code, err.strip()), (0, ""))

    def test_multi_line_list_gets_a_new_item(self):
        d = self.hub_repo(only_util=True)
        self.write("kinds.py", 'KINDS = [\n    "draft",\n    "live",\n    "done",\n]\n')
        self.git("add", "kinds.py")
        self.git("commit", "-qm", "multiline")
        self.write("kinds.py", 'KINDS = [\n    "draft",\n    "live",\n    "done",\n    "gone",\n]\n')
        with open(os.path.join(d, "README.md"), "a") as f:
            f.write("| gone | removed |\n")
        code, _, err = self.run_sweep(d)
        self.assertEqual(code, 2)
        self.assertIn("metrics.py", err)

    def test_parallel_copy_and_text_doc(self):
        d = self.hub_repo(doc=True, touch_metrics=True, touch_loader=True)
        self.write("labels.py", 'LABELS = {"draft": "D", "live": "L", "done": "X"}\n')
        self.write("legend.txt", "draft = pencil\nlive = green\ndone = tick\n")
        self.git("add", "labels.py", "legend.txt")
        self.git("commit", "-qm", "parallel copies", "--", "labels.py", "legend.txt")
        code, _, err = self.run_sweep(d)
        self.assertEqual(code, 2)
        self.assertIn("labels.py", err)
        self.assertIn("legend.txt", err)

    def test_loop_guard(self):
        self.assertEqual(run_hook("guard_consumer_sweep.py", {"cwd": self.hub_repo(), "stop_hook_active": True})[0], 0)


class AnchorRegressionTests(HookTestCase):
    def tree(self):
        self.write("tests/selftest.py", "# anchor set\n")
        return self.dir

    def stop(self, rows):
        return run_hook("guard_anchor_regression.py", {"transcript_path": self.transcript(rows)})

    def det(self, name="guard_x.py"):
        return asst_tool("Edit", {"file_path": os.path.join(self.dir, name)})

    def test_detector_changed_without_a_rerun_is_blocked(self):
        self.tree()
        code, _, err = self.stop([self.det(), asst_text("Done.")])
        self.assertEqual(code, 2)
        self.assertIn("anchor_regression", err)

    def test_a_run_before_the_change_does_not_count(self):
        self.tree()
        rows = [asst_tool("Bash", {"command": "python3 tests/selftest.py"}), tool_result("12/12 passed"), self.det()]
        self.assertEqual(self.stop(rows)[0], 2)

    def test_green_rerun_after_the_change_passes(self):
        self.tree()
        rows = [self.det(), asst_tool("Bash", {"command": "python3 tests/selftest.py"}), tool_result("12/12 passed")]
        self.assertEqual(self.stop(rows)[0], 0)

    def test_command_that_never_ran_does_not_count(self):
        self.tree()
        rows = [self.det(), asst_tool("Bash", {"command": "python3 tests/selftest.py"}), tool_result("bash: python3: command not found")]
        self.assertEqual(self.stop(rows)[0], 2)

    def test_red_rerun_does_not_count(self):
        self.tree()
        rows = [self.det(), asst_tool("Bash", {"command": "python3 tests/selftest.py"}), tool_result("FAIL case 3\n11/12 passed")]
        self.assertEqual(self.stop(rows)[0], 2)

    def test_zero_failure_counts_are_not_failures(self):
        self.tree()
        rows = [self.det(), asst_tool("Bash", {"command": "pytest"}), tool_result("12 passed, 0 failed\nFAIL=0")]
        self.assertEqual(self.stop(rows)[0], 0)

    def test_non_detector_file_is_ignored(self):
        self.tree()
        self.assertEqual(self.stop([self.det("util.py")])[0], 0)

    def test_repo_without_an_anchor_set_is_not_governed(self):
        self.assertEqual(self.stop([self.det("probes.py"), self.det("guard_health.py")])[0], 0)


if __name__ == "__main__":
    unittest.main()
