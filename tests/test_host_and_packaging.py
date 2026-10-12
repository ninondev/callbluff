# SPDX-License-Identifier: GPL-3.0-only
"""hostctx (model detection, cooldown), the shared helpers, and plugin packaging."""
import glob
import json
import os
import re
import sys
import time
import unittest

from helpers import HookTestCase, run_hook, asst_text, ROOT, HERE, HOOKS

sys.path.insert(0, HOOKS)
import hostctx  # noqa: E402
import _shared  # noqa: E402
from model_key import model_key  # noqa: E402

SAMPLE = os.path.join(HERE, "fixtures", "session_sample.jsonl")


class ModelKeyTests(unittest.TestCase):
    def test_families(self):
        for raw, want in (("haiku", "haiku"), ("claude-haiku-4-5-20251001", "haiku"), ("claude-fable-5", "fable"),
                          ("Fable", "fable"), ("claude-opus-5", "opus"), ("claude-sonnet-5", "sonnet"),
                          ("vendor/some-model-7b", "vendor/some-model-7b"), ("", ""), (None, "")):
            with self.subTest(raw=raw):
                self.assertEqual(model_key(raw), want)


class HostContextTests(HookTestCase):
    def test_model_is_read_from_the_sample_session_and_synthetic_rows_are_skipped(self):
        self.assertEqual(hostctx.detect_model_file(SAMPLE), "claude-sonnet-5")
        self.assertTrue(hostctx.is_strong(SAMPLE))

    def test_unknown_or_missing_model_is_not_strong(self):
        self.assertFalse(hostctx.is_strong(self.transcript([asst_text("x", model="vendor/some-model-7b")])))
        self.assertFalse(hostctx.is_strong(self.transcript([asst_text("x")])))
        self.assertFalse(hostctx.is_strong(""))

    def test_giant_tool_result_row_does_not_hide_the_model(self):
        rows = [asst_text("x", model="claude-opus-5"),
                {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "z" * 600000}]}}]
        self.assertEqual(hostctx.host_key(self.transcript(rows)), "opus")

    def test_subagent_reads_its_own_transcript(self):
        parent = self.transcript([asst_text("x", model="claude-opus-5")], name="sess.jsonl")
        os.makedirs(os.path.join(self.dir, "sess", "subagents"))
        self.transcript([asst_text("x", model="claude-haiku-4-5")], name="sess/subagents/agent-abc.jsonl")
        self.assertEqual(hostctx.host_key(parent, "abc"), "haiku")
        self.assertEqual(hostctx.host_key(parent, None), "opus")

    def test_mode_comes_from_the_registry(self):
        opus = self.transcript([asst_text("x", model="claude-opus-5")])
        haiku = self.transcript([asst_text("x", model="claude-haiku-4-5")], name="h.jsonl")
        self.assertEqual(hostctx.mode_for("false_completion", opus), "shadow")
        self.assertEqual(hostctx.mode_for("acceptance_file_guard", opus), "precision")
        self.assertEqual(hostctx.mode_for("execution_check", opus), "block")
        self.assertEqual(hostctx.mode_for("false_completion", haiku), "block")

    def test_cooldown(self):
        def row(sig, ago):
            ts = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - ago))
            return json.dumps({"timestamp": ts, "text": sig})
        self.assertTrue(hostctx.recently_fired([row("SIG", 600)], "SIG"))
        self.assertFalse(hostctx.recently_fired([row("SIG", 3 * 3600)], "SIG"))
        self.assertFalse(hostctx.recently_fired([row("other", 60)], "SIG"))
        self.assertTrue(hostctx.recently_fired(['{"text": "SIG"}'], "SIG"))  # no timestamps: once per session


class SharedHelperTests(HookTestCase):
    def test_changed_files_from_a_subdirectory(self):
        self.git("init", "-q")
        sub = os.path.join(self.dir, "sub")
        p = self.write("mod.py", "x = 1\n", base=sub)
        self.git("add", "sub/mod.py")
        self.git("commit", "-qm", "init")
        with open(p, "w") as f:
            f.write("x = 2\n")
        self.assertEqual(_shared.changed_files(sub), ["mod.py"])

    def test_session_written_files(self):
        from helpers import asst_tool
        t = self.transcript([asst_tool("Edit", {"file_path": "/a/b.py"}), asst_tool("Write", {"file_path": "rel.py"}),
                             asst_tool("Read", {"file_path": "/a/read_only.py"}), asst_tool("Edit", {"file_path": "/a/b.py"})])
        self.assertEqual(_shared.session_written_files(t, "/proj"), ["/a/b.py", "/proj/rel.py"])

    def test_strip_quoted(self):
        text = "He said “all done” and `all done` and 'all done'.\n```\nall done\n```\n> all done\nReal text."
        self.assertEqual(_shared.strip_quoted(text).split().count("done"), 0)
        self.assertIn("Real text.", _shared.strip_quoted(text))
        self.assertIn("don't", _shared.strip_quoted("I don't think it's fine."))


class SmokeTest(HookTestCase):
    def test_every_stop_hook_accepts_the_sample_session(self):
        for script in sorted(os.path.basename(p) for p in glob.glob(os.path.join(HOOKS, "guard_*.py"))):
            with self.subTest(script=script):
                code, _, err = run_hook(script, {"transcript_path": SAMPLE, "cwd": self.dir, "tool_input": {}})
                self.assertIn(code, (0, 2), err)
                self.assertNotIn("Traceback", err)

    def test_every_hook_survives_garbage_input(self):
        import subprocess
        for script in sorted(os.path.basename(p) for p in glob.glob(os.path.join(HOOKS, "guard_*.py"))):
            with self.subTest(script=script):
                r = subprocess.run([sys.executable, "-B", os.path.join(HOOKS, script)], input="not json",
                                   capture_output=True, text=True, timeout=30)
                self.assertEqual((r.returncode, r.stderr), (0, ""))


class PackagingTests(unittest.TestCase):
    def load(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            return json.load(f)

    def test_manifest(self):
        m = self.load(".claude-plugin/plugin.json")
        self.assertEqual(m["name"], "callbluff")
        self.assertEqual(set(m), {"name", "version", "description", "author"})

    def test_marketplace_points_at_this_plugin(self):
        mk = self.load(".claude-plugin/marketplace.json")
        self.assertEqual(mk["name"], "callbluff")
        self.assertEqual([p["name"] for p in mk["plugins"]], ["callbluff"])
        self.assertTrue(os.path.isdir(os.path.join(ROOT, mk["plugins"][0]["source"])))

    def test_every_hook_command_points_at_a_real_script(self):
        hooks = self.load("hooks/hooks.json")["hooks"]
        scripts = set()
        for groups in hooks.values():
            for group in groups:
                for h in group["hooks"]:
                    m = re.search(r"\$\{CLAUDE_PLUGIN_ROOT\}/(\S+?)\"", h["command"])
                    self.assertIsNotNone(m, h["command"])
                    self.assertTrue(os.path.isfile(os.path.join(ROOT, m.group(1))), m.group(1))
                    scripts.add(m.group(1))
        self.assertEqual(scripts, {g["hook"] for k, g in self.load("gates.json").items() if not k.startswith("_")})

    def test_registry_modes_are_known(self):
        for name, g in self.load("gates.json").items():
            if not name.startswith("_"):
                self.assertIn(g["strong_mode"], ("block", "shadow", "precision"), name)
                self.assertTrue(os.path.isfile(os.path.join(ROOT, g["hook"])), name)

    def test_every_gate_message_names_its_gate(self):
        for name, g in self.load("gates.json").items():
            if not name.startswith("_"):
                with open(os.path.join(ROOT, g["hook"]), encoding="utf-8") as f:
                    src = f.read()
                self.assertIn(f'GATE = "{name}"', src)

    def test_commands_exist(self):
        for name in ("report", "audit"):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, "commands", f"{name}.md")))

    def test_source_files_carry_the_license_header(self):
        for p in glob.glob(os.path.join(ROOT, "hooks", "*.py")) + glob.glob(os.path.join(ROOT, "tools", "*.py")) \
                + glob.glob(os.path.join(HERE, "*.py")):
            with open(p, encoding="utf-8") as f:
                head = f.read(400)
            self.assertIn("SPDX-License-Identifier: GPL-3.0-only", head, p)

    # Chinese is allowed only where it is functional (claim-detection patterns and their tests) and in the Chinese README.
    CHINESE_OK = {
        "hooks/_shared.py", "hooks/guard_false_completion.py", "hooks/guard_unmeasured_claims.py",
        "hooks/guard_delivery_inventory.py", "hooks/guard_duration_claim.py", "hooks/guard_claim_evidence.py",
        "tests/test_chinese_claims.py", "README.zh-Hans.md",
    }

    def test_chinese_text_appears_only_in_claim_patterns_and_their_tests(self):
        bad = []
        for dirpath, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", ".work", "__pycache__")]
            for fn in files:
                if fn in ("LICENSE", "CLA.md") or fn.endswith((".pyc", ".png", ".gif")):
                    continue  # images are binary; their bytes are not text
                p = os.path.join(dirpath, fn)
                rel = os.path.relpath(p, ROOT)
                if rel in self.CHINESE_OK:
                    continue
                with open(p, encoding="utf-8", errors="replace") as f:
                    for n, line in enumerate(f, 1):
                        if "](README.zh-Hans.md)" in line:
                            continue  # the language switch link
                        if any(0x3000 <= ord(ch) <= 0x9fff or 0xff00 <= ord(ch) <= 0xffef for ch in line):
                            bad.append(f"{rel}:{n}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
