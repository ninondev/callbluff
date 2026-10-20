# SPDX-License-Identifier: MIT
"""The adapter that runs the gates in Codex and DeepSeek Harness.

The payload shapes follow what Codex 0.159.2 sent to a hook in a recorded run
(Bash with tool_input.command, apply_patch with the patch in tool_input.command,
Stop with last_assistant_message) and what DeepSeek Harness's Claude Code hooks
bridge documents (empty transcript_path, no last_assistant_message, dsh tool names).
"""
import glob
import json
import os
import re
import uuid

from helpers import HookTestCase, run_hook, ROOT, needs_non_temp_checkout


class AdapterTestCase(HookTestCase):
    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.dir, "cbhome")
        self.sid = uuid.uuid4().hex

    def call(self, args, payload):
        payload = dict({"session_id": self.sid, "cwd": self.dir, "transcript_path": ""}, **payload)
        import subprocess
        import sys
        r = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "hooks", "adapter.py")] + args,
                           input=json.dumps(payload), capture_output=True, text=True, timeout=120,
                           env=dict(os.environ, CALLBLUFF_HOME=self.home, CALLBLUFF_NO_FIRELOG="1"))
        return r.returncode, r.stdout, r.stderr

    def prompt(self, text):
        return self.call(["record"], {"hook_event_name": "UserPromptSubmit", "prompt": text})

    def tool(self, name, tool_input, response=""):
        return self.call(["record"], {"hook_event_name": "PostToolUse", "tool_name": name,
                                      "tool_input": tool_input, "tool_response": response,
                                      "tool_use_id": uuid.uuid4().hex})

    def stop(self, gate, message=None):
        payload = {"hook_event_name": "Stop", "stop_hook_active": False}
        if message is not None:
            payload["last_assistant_message"] = message
        return self.call(["gate", gate], payload)

    def rows(self):
        path = os.path.join(self.home, "sessions", self.sid + ".jsonl")
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def tool_uses(self):
        out = []
        for row in self.rows():
            for blk in (row.get("message") or {}).get("content") or []:
                if isinstance(blk, dict) and blk.get("type") == "tool_use":
                    out.append((blk["name"], blk["input"]))
        return out


class RecordTests(AdapterTestCase):
    def test_codex_patch_becomes_one_write_or_edit_per_file(self):
        patch = ("*** Begin Patch\n*** Add File: /w/new.py\n+x = 1\n*** Update File: /w/old.py\n@@\n-a\n+b\n"
                 "*** Delete File: /w/gone.py\n*** End Patch")
        self.tool("apply_patch", {"command": patch}, "Success")
        self.assertEqual(self.tool_uses(), [("Write", {"file_path": "/w/new.py"}),
                                            ("Edit", {"file_path": "/w/old.py"})])

    def test_bash_keeps_its_command_and_result(self):
        self.tool("Bash", {"command": "python3 -m pytest -q"}, "3 passed")
        self.assertEqual(self.tool_uses(), [("Bash", {"command": "python3 -m pytest -q"})])
        results = [b for r in self.rows() for b in (r.get("message") or {}).get("content") or []
                   if isinstance(b, dict) and b.get("type") == "tool_result"]
        self.assertEqual(results[0]["content"], "3 passed")

    def test_dsh_tool_names_map_to_claude_code_names(self):
        self.tool("bash", {"command": "ls"})
        self.tool("write", {"file_path": "/w/a.py", "content": "x"})
        self.tool("edit", {"file_path": "/w/a.py", "old_string": "x", "new_string": "y"})
        self.tool("str_replace_editor", {"command": "create", "path": "/w/b.py", "file_text": "z"})
        self.tool("str_replace_editor", {"command": "view", "path": "/w/b.py"})
        self.assertEqual([n for n, _ in self.tool_uses()], ["Bash", "Write", "Edit", "Write", "Read"])
        self.assertEqual(self.tool_uses()[3][1]["file_path"], "/w/b.py")

    def test_prompt_is_a_user_text_row(self):
        self.prompt("fix the login bug")
        row = self.rows()[0]
        self.assertEqual(row["type"], "user")
        self.assertEqual(row["message"]["content"][0]["text"], "fix the login bug")
        self.assertIn("timestamp", row)

    def test_garbage_input_is_ignored(self):
        import subprocess
        import sys
        r = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "hooks", "adapter.py"), "record"],
                           input="not json", capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stderr), (0, ""))


class StopTests(AdapterTestCase):
    def test_codex_false_completion_is_blocked_and_noted(self):
        self.prompt("fix the login bug")
        code, _, err = self.stop("false_completion", "Fixed: the login bug is resolved.")
        self.assertEqual(code, 2)
        self.assertIn("callbluff:false_completion", err)
        notes = [r for r in self.rows() if r.get("type") == "system"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["callbluff_gate"], "false_completion")

    def test_last_message_is_added_once(self):
        self.prompt("hi")
        self.stop("duration_claim", "Done.")
        self.stop("claim_evidence", "Done.")
        texts = [b["text"] for r in self.rows() if r.get("type") == "assistant"
                 for b in r["message"]["content"] if b.get("type") == "text"]
        self.assertEqual(texts, ["Done."])

    def test_broken_code_written_through_codex_is_blocked(self):
        path = self.write("util.py", "def add(a, b):\n    return a + c\n")
        self.prompt("add a helper")
        self.tool("apply_patch", {"command": f"*** Begin Patch\n*** Add File: {path}\n+...\n*** End Patch"})
        code, _, err = self.stop("execution_check", "Done.")
        self.assertEqual(code, 2)
        self.assertIn("undefined name `c`", err)

    def test_same_gate_lets_the_stop_through_after_two_blocks(self):
        self.write("util.py", "def add(a, b):\n    return a + c\n")
        self.prompt("add a helper")
        self.tool("write", {"file_path": os.path.join(self.dir, "util.py"), "content": "..."})
        codes = [self.stop("execution_check")[0] for _ in range(3)]
        self.assertEqual(codes, [2, 2, 0])
        self.prompt("try again")
        self.assertEqual(self.stop("execution_check")[0], 2)

    def test_dsh_stop_without_the_reply_does_not_block_claim_gates(self):
        self.prompt("fix the login bug")
        for gate in ("false_completion", "unmeasured_claims", "duration_claim", "claim_evidence"):
            with self.subTest(gate=gate):
                self.assertEqual(self.stop(gate)[0], 0)


class PreToolUseTests(AdapterTestCase):
    @needs_non_temp_checkout
    def test_codex_patch_on_an_existing_test_file_asks(self):
        path = self.write("tests/test_login.py", "def test_ok():\n    assert True\n")
        patch = f"*** Begin Patch\n*** Update File: {path}\n@@\n-    assert True\n+    pass\n*** End Patch"
        code, out, _ = self.call(["gate", "acceptance_files"], {
            "hook_event_name": "PreToolUse", "tool_name": "apply_patch", "tool_input": {"command": patch}})
        self.assertEqual(code, 0)
        decision = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "ask")
        self.assertIn("test_login.py", decision["permissionDecisionReason"])

    def test_codex_patch_on_ordinary_code_passes(self):
        path = self.write("app.py", "x = 1\n")
        patch = f"*** Begin Patch\n*** Update File: {path}\n@@\n-x = 1\n+x = 2\n*** End Patch"
        self.assertEqual(self.call(["gate", "acceptance_files"], {
            "hook_event_name": "PreToolUse", "tool_name": "apply_patch", "tool_input": {"command": patch}}),
            (0, "", ""))


class ConfigTests(HookTestCase):
    def load(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            return json.load(f)

    def gates_in(self, rel, root_var):
        out = []
        for groups in self.load(rel)["hooks"].values():
            for group in groups:
                for h in group["hooks"]:
                    m = re.fullmatch(r'python3 "\$\{' + root_var + r'\}/hooks/adapter\.py" (record|gate ([a-z_]+))',
                                     h["command"])
                    self.assertIsNotNone(m, h["command"])
                    if m.group(2):
                        self.assertTrue(os.path.isfile(os.path.join(ROOT, "hooks", f"guard_{m.group(2)}.py")))
                        out.append(m.group(2))
        return out

    def stop_gates(self, rel):
        return [re.search(r"(?:guard_|gate )([a-z_]+)", h["command"]).group(1)
                for h in self.load(rel)["hooks"]["Stop"][0]["hooks"]]

    def test_codex_and_dsh_configs_run_every_gate(self):
        every = {os.path.basename(p)[6:-3] for p in glob.glob(os.path.join(ROOT, "hooks", "guard_*.py"))}
        self.assertEqual(set(self.gates_in("hooks/codex.json", "PLUGIN_ROOT")), every)
        self.assertEqual(set(self.gates_in("hooks/dsh.json", "CLAUDE_PLUGIN_ROOT")), every)

    def test_stop_order_matches_claude_code(self):
        self.assertEqual(self.stop_gates("hooks/codex.json"), self.stop_gates("hooks/hooks.json"))
        self.assertEqual(self.stop_gates("hooks/dsh.json"), self.stop_gates("hooks/hooks.json"))

    def test_codex_manifest(self):
        m = self.load(".codex-plugin/plugin.json")
        self.assertEqual(m["name"], "callbluff")
        self.assertEqual(m["version"], self.load(".claude-plugin/plugin.json")["version"])
        self.assertTrue(os.path.isfile(os.path.join(ROOT, m["hooks"])))


class ParallelStopTests(AdapterTestCase):
    def test_stop_hooks_started_together_add_the_reply_once(self):
        import subprocess
        import sys
        self.prompt("hi")
        payload = json.dumps({"session_id": self.sid, "cwd": self.dir, "transcript_path": "",
                              "hook_event_name": "Stop", "stop_hook_active": False,
                              "last_assistant_message": "All done."})
        env = dict(os.environ, CALLBLUFF_HOME=self.home, CALLBLUFF_NO_FIRELOG="1")
        procs = [subprocess.Popen([sys.executable, "-B", os.path.join(ROOT, "hooks", "adapter.py"), "gate", g],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
                 for g in ("duration_claim", "claim_evidence", "unmeasured_claims", "anchor_regression") * 4]
        for p in procs:  # hand every process its payload before waiting on any of them
            p.stdin.write(payload.encode())
            p.stdin.close()
        for p in procs:
            p.wait(timeout=120)
            p.stdout.close()
            p.stderr.close()
        texts = [b["text"] for r in self.rows() if r.get("type") == "assistant"
                 for b in r["message"]["content"] if b.get("type") == "text"]
        self.assertEqual(texts, ["All done."])
