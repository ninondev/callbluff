# SPDX-License-Identifier: GPL-3.0-only
"""delivery_inventory: tested only through the Stop hook JSON and exit code."""
import unittest

from helpers import HookTestCase, run_hook, asst_text, asst_tool, user_text


class DeliveryInventoryTests(HookTestCase):
    def check_stop(self, text, expected, files=5, cross_turn=False, active=False, current_text=None, file_paths=None):
        rows = []
        for path in file_paths or [f"/w/mod_{i:02d}.py" for i in range(files)]:
            if cross_turn:
                rows.append(user_text("continue"))
            rows.append(asst_tool("Edit", {"file_path": path}))
        if cross_turn:
            rows.append(user_text("how is it going?"))
        rows.append(asst_text(text))
        payload = {"transcript_path": self.transcript(rows), "stop_hook_active": active}
        if current_text is not None:
            payload["last_assistant_message"] = current_text
        code, _, err = run_hook("guard_delivery_inventory.py", payload)
        self.assertEqual(code, expected, msg=f"{text}\n{err}")
        if expected == 2:
            self.assertIn("delivery_inventory", err)

    def test_batch_completion_claims_without_inventory_are_blocked(self):
        for text in (
            "All 12 modules are now updated.",
            "12 modules have been migrated, all done.",
            "All done.",
            "Done.",
            "Everything is done.",
            "All tasks are complete.",
            "I have updated every call to log_old.",
            "I've now converted all the modules to the new API.",
            "Finished! All the log_old calls in mod_01 to mod_18 were replaced with log_new.",
            "Every module has been refactored.",
        ):
            with self.subTest(text=text):
                self.check_stop(text, 2)

    def test_inventory_and_small_jobs_pass(self):
        self.check_stop("11 modules are done; mod_09 uses a fallback implementation; the firewall item is not done (no permission).", 0)
        self.check_stop("5 items are done; to-do: acceptance of mod_04.", 0)
        self.check_stop("5 items are done; 2 more remain.", 0)
        self.check_stop("Everything is done.", 0, files=1)
        self.check_stop("All tasks are complete.", 0, active=True)

    def test_complete_file_inventory_with_passing_checks(self):
        self.check_stop(
            "All tasks are complete.\n"
            + "".join(f"- mod_{i:02d}.py: fixed, tests pass.\n" for i in range(5)), 0)
        self.check_stop(
            "All tasks are complete.\n| file | check |\n"
            + "".join(f"| `mod_{i:02d}.py` | pytest passed |\n" for i in range(5)), 0)

    def test_incomplete_or_unverified_lists_still_block(self):
        for listing in (
            "".join(f"- mod_{i:02d}.py: tests pass.\n" for i in range(4)),
            "".join(f"- mod_{i:02d}.py\n" for i in range(5)),
            "- mod_00.py, mod_01.py, mod_02.py, mod_03.py, mod_04.py: tests pass.\n",
        ):
            with self.subTest(listing=listing):
                self.check_stop("All tasks are complete.\n" + listing, 2)

    def test_same_base_name_needs_full_paths(self):
        paths = ["/w/a/main.py", "/w/b/main.py", "/w/a/app.py", "/w/b/app.py"]
        self.check_stop("All tasks are complete.\n- main.py: tests pass.\n- app.py: tests pass.", 2, file_paths=paths)
        self.check_stop(
            "All tasks are complete.\n- /w/a/main.py: tests pass.\n- /w/b/main.py: tests pass.\n"
            "- /w/a/app.py: tests pass.\n- /w/b/app.py: tests pass.", 0, file_paths=paths)

    def test_an_example_list_does_not_count_as_the_inventory(self):
        listing = "".join(f"- mod_{i:02d}.py: tests pass.\n" for i in range(5))
        for example in ("```text\n" + listing + "```", "> " + listing.replace("\n", "\n> "), "“" + listing + "”"):
            with self.subTest(example=example):
                self.check_stop("All tasks are complete.\nExample list:\n" + example, 2)

    def test_current_reply_beats_a_stale_transcript(self):
        self.check_stop("All tasks are complete.", 0, current_text="The third run is still going, moved to the background.")
        self.check_stop("The third run is still going.", 2, current_text="All tasks are complete.")
        self.check_stop("All tasks are complete.", 0, current_text="")


class RegressionRealCompletionWordingOnly(HookTestCase):
    """Fix (c): fire only on a real completion statement, never on a mention of completion."""

    check_stop = DeliveryInventoryTests.check_stop

    def test_progress_reports_do_not_fire(self):
        for text in (
            "The reproducibility run is still going (about 20 minutes a round, 19 in). When it is done I will "
            "write the result into the state file and commit.",
            "The third round is still running in the background. Once it comes back I will merge all three rounds into one volume.",
            "The first step is done, the rest is running.",
            "3 of 12 modules are done, 9 to go.",
        ):
            with self.subTest(text=text):
                self.check_stop(text, 0, cross_turn=True)

    def test_conditions_plans_and_deferrals_do_not_fire(self):
        for text in (
            "Wait until all tasks are complete before you deploy.",
            "If all tasks are complete, I will deliver.",
            "If the tests pass, then all tasks are complete.",
            "I will tell you when everything is done.",
            "Once all the modules are updated we can ship.",
            "All modules will be updated after review.",
            "All tasks are done after the review.",
            "The plan is to get everything done by Friday.",
            "Are all tasks complete? Not yet.",
            "Is everything done?",
        ):
            with self.subTest(text=text):
                self.check_stop(text, 0)

    def test_negations_do_not_fire(self):
        for text in (
            "I can't say that all tasks are complete.",
            "Not all tasks are complete.",
            "It is not true that everything is done.",
            "No, not everything is done.",
            "All tasks are not done.",
            "Not all done.",
            "The tasks haven't all been completed.",
        ):
            with self.subTest(text=text):
                self.check_stop(text, 0)

    def test_quotes_code_and_identifiers_do_not_fire(self):
        for text in (
            "The log says \"all tasks are complete\" and I am checking that.",
            "Example: `all tasks are complete`.",
            "> all tasks are complete.\nThat is a quote.",
            "```text\nAll tasks are complete.\n```\nThat is test input.",
            "The `all_done` flag is set by the scheduler.",
            "I renamed the all-done banner.",
        ):
            with self.subTest(text=text):
                self.check_stop(text, 0)

    def test_a_real_statement_after_a_mention_still_fires(self):
        for text in (
            "Another job is still running. All tasks are complete.",
            "Waiting on logs. Everything is done.",
            "The old plan cannot be reused. All tasks are complete.",
            "Quoted sample: “not all done”. In fact, all tasks are complete.",
        ):
            with self.subTest(text=text):
                self.check_stop(text, 2)

    def test_hedged_completion_does_not_fire(self):
        for text in (
            "All tasks are complete except the firewall item.",
            "All modules are updated, but two tests failed.",
            "Everything is done, with a workaround for mod_09.",
        ):
            with self.subTest(text=text):
                self.check_stop(text, 0)


if __name__ == "__main__":
    unittest.main()
