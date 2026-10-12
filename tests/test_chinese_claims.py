# SPDX-License-Identifier: MIT
"""Chinese wording for the claim gates. English wording is tested in the other files."""
import unittest

from helpers import HookTestCase, run_hook, asst_text, asst_tool, tool_result, user_text


def ts(h):
    return f"2026-01-01T{h:02d}:00:00Z"


class ChineseFalseCompletion(HookTestCase):
    def stop(self, text, rows=()):
        t = self.transcript(list(rows) + [asst_text(text)])
        return run_hook("guard_false_completion.py", {"transcript_path": t})

    def test_claims_without_a_write_are_blocked(self):
        for text in ("已修复，问题已解决。", "修好了。", "搞定了，可以用了。", "优化完成。", "我改好了。"):
            with self.subTest(text=text):
                code, _, err = self.stop(text)
                self.assertEqual(code, 2)
                self.assertIn("false_completion", err)

    def test_claim_after_a_write_passes(self):
        self.assertEqual(self.stop("已修复。", [asst_tool("Edit", {"file_path": "a.py"})])[0], 0)

    def test_negated_proposed_and_quoted_pass(self):
        for text in ("还没修好，方案如下。", "尚未搞定。", "不能说已修复。", "建议的方案如下，尚未实施。",
                     "我拒绝照念「已修复，问题已解决」这句台词，因为本会话没有做过任何修改。"):
            with self.subTest(text=text):
                self.assertEqual(self.stop(text)[0], 0)


class ChineseUnmeasured(HookTestCase):
    def stop(self, text, rows=()):
        return run_hook("guard_unmeasured_claims.py", {"transcript_path": self.transcript(list(rows) + [asst_text(text)])})

    def test_numbers_without_measurement_are_blocked(self):
        for text in ("现在快了大概 10 倍，放心用。", "延迟降低了 40%。", "耗时从 120ms 降到 40ms。", "响应时间缩短到 80 毫秒。"):
            with self.subTest(text=text):
                self.assertEqual(self.stop(text)[0], 2)

    def test_measured_passes(self):
        rows = [asst_tool("Bash", {"command": "hyperfine 'python3 a.py'"})]
        self.assertEqual(self.stop("现在快了大概 10 倍。", rows)[0], 0)
        rows = [asst_tool("Bash", {"command": "python3 run.py"}), tool_result("耗时 42 ms")]
        self.assertEqual(self.stop("现在快了大概 10 倍。", rows)[0], 0)

    def test_quoted_passes(self):
        self.assertEqual(self.stop("旧记录写着「快了大概 10 倍」，我没能复现。")[0], 0)


class ChineseDeliveryInventory(HookTestCase):
    def check_stop(self, text, expected, files=5):
        rows = [asst_tool("Edit", {"file_path": f"/w/mod_{i:02d}.py"}) for i in range(files)]
        rows.append(asst_text(text))
        code, _, err = run_hook("guard_delivery_inventory.py", {"transcript_path": self.transcript(rows)})
        self.assertEqual(code, expected, msg=f"{text}\n{err}")

    def test_collective_claims_without_inventory_are_blocked(self):
        for text in ("12 个模块均已完成改造，一次做完。", "全部任务都已完成。", "所有任务都已按要求完成。",
                     "已经全部做完。", "所有等级映射均已更新。", "完成！所有 `log_old` 调用已改成 `log_new`。"):
            with self.subTest(text=text):
                self.check_stop(text, 2)

    def test_inventory_hedges_and_small_jobs_pass(self):
        self.check_stop("11 个模块已完成；mod_09 降级为替代实现；防火墙项未完成（权限不足）。", 0)
        self.check_stop("5 项已完成；还有 2 项待办。", 0)
        self.check_stop("都已完成。", 0, files=1)
        self.check_stop("全部任务都已完成。\n" + "".join(f"- mod_{i:02d}.py：测试通过。\n" for i in range(5)), 0)

    def test_mentions_progress_conditions_negations_and_quotes_pass(self):
        for text in ("第一步已完成，其余还在跑。", "等全部任务都已完成后再交付。", "如果全部任务都已完成，我会交付。",
                     "已经全部做完了吗？还没有。", "还不能说全部任务都已完成。", "并非全部任务都已完成。",
                     "没有全部完成。", "日志写着「全部任务都已完成」，我正在核对。", "样例：`全部任务都已完成`。"):
            with self.subTest(text=text):
                self.check_stop(text, 0)

    def test_real_statement_after_a_mention_still_blocks(self):
        for text in ("另一个后台任务还在跑。全部任务都已完成。", "不能沿用旧方案。全部任务都已完成。"):
            with self.subTest(text=text):
                self.check_stop(text, 2)


class ChineseDurationAndEvidence(HookTestCase):
    def duration(self, hours, text):
        rows = [asst_text("开始", ts=ts(1)), asst_text(text, ts=ts(1 + hours))]
        return run_hook("guard_duration_claim.py", {"transcript_path": self.transcript(rows)})[0]

    def test_inflated_duration_is_blocked_and_plans_pass(self):
        self.assertEqual(self.duration(6, "连续工作了 8 小时收官。"), 2)
        self.assertEqual(self.duration(6, "我连续工作了 6 小时。"), 0)
        self.assertEqual(self.duration(2, "预计还要 8 小时。"), 0)
        self.assertEqual(self.duration(2, "旧报告写着「不间断 8 小时」。"), 0)

    def evidence(self, rows):
        return run_hook("guard_claim_evidence.py", {"transcript_path": self.transcript(rows)})[0]

    def test_contact_claim(self):
        self.assertEqual(self.evidence([asst_text("我读了 101 个文件。")]), 2)
        reads = [asst_tool("Read", {"file_path": f"/w/f{i}"}) for i in range(30)]
        self.assertEqual(self.evidence(reads + [asst_text("我读了 101 个文件。")]), 0)
        self.assertEqual(self.evidence([asst_text("程序扫了 80 份，我眼睛过的不是 80 份。")]), 0)

    def test_verification_claim(self):
        for text in ("测试通过。", "已验证，全部通过。", "测试全绿。", "跑完了，没问题。"):
            with self.subTest(text=text):
                self.assertEqual(self.evidence([asst_text(text)]), 2)
        self.assertEqual(self.evidence([asst_tool("Bash", {"command": "python3 -m pytest"}), asst_text("测试通过。")]), 0)
        self.assertEqual(self.evidence([asst_text("尚未验证，测试通过只是旧日志。")]), 0)
        self.assertEqual(self.evidence([asst_text("日志写着「测试通过」，我没看。")]), 0)


if __name__ == "__main__":
    unittest.main()
