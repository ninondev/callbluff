# SPDX-License-Identifier: GPL-3.0-only
"""false_completion, unmeasured_claim, duration_claim, claim_evidence."""
import json
import os
import unittest

from helpers import HookTestCase, run_hook, asst_text, asst_tool, tool_result


class FalseCompletionTests(HookTestCase):
    def stop(self, rows, **payload):
        return run_hook("guard_false_completion.py", dict(transcript_path=self.transcript(rows), **payload))

    def test_claim_with_no_write_is_blocked(self):
        code, _, err = self.stop([asst_text("I looked at the code."), asst_text("I fixed it. The issue is resolved.")])
        self.assertEqual(code, 2)
        self.assertIn("false_completion", err)

    def test_claim_after_a_real_write_passes(self):
        self.assertEqual(self.stop([asst_tool("Edit", {"file_path": "a.py"}), asst_text("I fixed it.")])[0], 0)

    def test_claim_after_a_shell_write_passes(self):
        rows = [asst_tool("Bash", {"command": "sed -i '' 's/a/b/' x.py"}), asst_text("I fixed it.")]
        self.assertEqual(self.stop(rows)[0], 0)

    def test_proposal_without_a_claim_passes(self):
        self.assertEqual(self.stop([asst_text("Proposed fix, not applied yet: change the retry logic.")])[0], 0)

    def test_negated_or_conditional_claims_pass(self):
        for text in ("I have not fixed it yet.", "The bug isn't fixed.", "Once the bug is fixed we can ship.",
                     "If this is resolved by the patch, we are done.", "It would be fixed by the patch."):
            with self.subTest(text=text):
                self.assertEqual(self.stop([asst_text(text)])[0], 0)

    def test_quoted_claims_pass(self):
        text = "I refuse to repeat “I fixed it, the issue is resolved” because nothing was changed in this session."
        self.assertEqual(self.stop([asst_text(text)])[0], 0)

    def test_loop_guard(self):
        self.assertEqual(self.stop([asst_text("I fixed it.")], stop_hook_active=True)[0], 0)

    def test_strong_host_runs_in_shadow_mode(self):
        home = os.path.join(self.dir, "home")
        t = self.transcript([asst_text("I fixed it.", model="claude-sonnet-5")])
        code, _, err = run_hook("guard_false_completion.py", {"transcript_path": t},
                                env={"CALLBLUFF_NO_FIRELOG": "", "CALLBLUFF_HOME": home})
        self.assertEqual((code, err), (0, ""))
        with open(os.path.join(home, "firelog.jsonl")) as f:
            self.assertEqual(json.loads(f.readline())["mode"], "shadow")

    def test_weak_or_unknown_host_still_blocks(self):
        for model in ("claude-haiku-4-5", "some-other-model"):
            with self.subTest(model=model):
                t = self.transcript([asst_text("I fixed it.", model=model)])
                self.assertEqual(run_hook("guard_false_completion.py", {"transcript_path": t})[0], 2)


class UnmeasuredClaimTests(HookTestCase):
    def stop(self, rows):
        return run_hook("guard_unmeasured_claims.py", {"transcript_path": self.transcript(rows)})

    def test_bare_number_is_blocked(self):
        for text in ("It is about 10x faster now.", "Latency was cut by 40%.", "Runtime went from 120 ms to 40 ms.",
                     "That makes it 3 times faster.", "Loading time dropped to 80 ms."):
            with self.subTest(text=text):
                code, _, err = self.stop([asst_text(text)])
                self.assertEqual(code, 2)
                self.assertIn("unmeasured_claim", err)

    def test_benchmark_in_session_passes(self):
        rows = [asst_tool("Bash", {"command": "hyperfine 'python3 old.py' 'python3 new.py'"}),
                asst_text("It is about 10x faster now.")]
        self.assertEqual(self.stop(rows)[0], 0)

    def test_timing_output_passes(self):
        rows = [asst_tool("Bash", {"command": "python3 run.py"}), tool_result("done in 42.1 ms"),
                asst_text("It is about 10x faster now.")]
        self.assertEqual(self.stop(rows)[0], 0)

    def test_complexity_argument_passes(self):
        self.assertEqual(self.stop([asst_text("The new loop is O(n) instead of O(n^2), so it should scale better.")])[0], 0)

    def test_quoted_claim_passes(self):
        self.assertEqual(self.stop([asst_text("The old note said “10x faster”, which I could not confirm.")])[0], 0)


def ts(h, m=0):
    return f"2026-01-01T{h:02d}:{m:02d}:00Z"


class DurationClaimTests(HookTestCase):
    def stop(self, hours, text):
        rows = [asst_text("start", ts=ts(1)), asst_text(text, ts=ts(1 + hours))]
        return run_hook("guard_duration_claim.py", {"transcript_path": self.transcript(rows)})

    def test_inflated_claim_is_blocked(self):
        code, _, err = self.stop(6, "I worked non-stop for 8 hours on this.")
        self.assertEqual(code, 2)
        self.assertIn("duration_claim", err)

    def test_accurate_claim_passes(self):
        self.assertEqual(self.stop(6, "I worked for 6 hours on this.")[0], 0)

    def test_plan_and_estimate_pass(self):
        for text in ("I expect another 8 hours of work.", "The batch needs 8 hours to run.",
                     "Estimated 8 hours of work remain."):
            with self.subTest(text=text):
                self.assertEqual(self.stop(2, text)[0], 0)

    def test_quoted_duration_passes(self):
        self.assertEqual(self.stop(2, "The old report said “worked non-stop for 8 hours”.")[0], 0)


class ClaimEvidenceTests(HookTestCase):
    def stop(self, rows):
        return run_hook("guard_claim_evidence.py", {"transcript_path": self.transcript(rows)})

    def test_contact_claim_without_reads_is_blocked(self):
        code, _, err = self.stop([asst_text("I read through all 101 files.")])
        self.assertEqual(code, 2)
        self.assertIn("claim_evidence", err)

    def test_contact_claim_with_reads_passes(self):
        rows = [asst_tool("Read", {"file_path": f"/w/f{i}.txt"}) for i in range(30)]
        rows.append(asst_text("I read through all 101 files."))
        self.assertEqual(self.stop(rows)[0], 0)

    def test_shell_reads_count_as_contact(self):
        rows = [asst_tool("Bash", {"command": f"cat f{i}.txt"}) for i in range(30)]
        rows.append(asst_text("I read 101 files."))
        self.assertEqual(self.stop(rows)[0], 0)

    def test_verification_claim_without_a_run_is_blocked(self):
        for text in ("I verified it and all tests pass.", "All tests are green.", "Tests pass."):
            with self.subTest(text=text):
                self.assertEqual(self.stop([asst_text(text)])[0], 2)

    def test_verification_claim_with_a_run_passes(self):
        rows = [asst_tool("Bash", {"command": "python3 -m pytest"}), asst_text("All tests are green.")]
        self.assertEqual(self.stop(rows)[0], 0)

    def test_quoted_claim_passes(self):
        self.assertEqual(self.stop([asst_text("The log line says “all tests pass” but I have not looked at it.")])[0], 0)

    def test_honest_unverified_passes(self):
        self.assertEqual(self.stop([asst_text("I have not verified this yet; tests pass in the old log only.")])[0], 0)

    def test_self_limiting_sentence_passes(self):
        self.assertEqual(self.stop([asst_text("The script scanned 80 files; I did not read 80 files myself.")])[0], 0)


if __name__ == "__main__":
    unittest.main()
