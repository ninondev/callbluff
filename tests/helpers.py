# SPDX-License-Identifier: MIT
"""Shared helpers for the test files. Standard library only."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOOKS = os.path.join(ROOT, "hooks")
WORK = os.path.join(HERE, ".work")


def run_hook(script, payload, env=None):
    """Run a hook script with a JSON payload on stdin. Returns (code, stdout, stderr)."""
    e = dict(os.environ, CALLBLUFF_NO_FIRELOG="1")
    e.update(env or {})
    r = subprocess.run([sys.executable, "-B", os.path.join(HOOKS, script)],
                       input=json.dumps(payload), capture_output=True, text=True, timeout=60, env=e)
    return r.returncode, r.stdout, r.stderr


def asst_text(text, model=None, ts=None):
    row = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}
    if model:
        row["message"]["model"] = model
    if ts:
        row["timestamp"] = ts
    return row


def asst_tool(name, tool_input=None, ts=None):
    row = {"type": "assistant", "message": {"role": "assistant",
           "content": [{"type": "tool_use", "name": name, "input": tool_input or {}}]}}
    if ts:
        row["timestamp"] = ts
    return row


def tool_result(text):
    return {"type": "user", "message": {"role": "user",
            "content": [{"type": "tool_result", "content": text}]}}


def user_text(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


class HookTestCase(unittest.TestCase):
    """Gives each test its own work directory under tests/.work (outside any system temp
    directory, because the test-file gate treats temp directories as scratch)."""

    def setUp(self):
        os.makedirs(WORK, exist_ok=True)
        self.dir = os.path.join(WORK, uuid.uuid4().hex[:10])
        os.makedirs(self.dir)
        self.addCleanup(shutil.rmtree, self.dir, True)

    def transcript(self, rows, name="t.jsonl"):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        return path

    def write(self, rel, text, base=None):
        p = os.path.join(base or self.dir, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def git(self, *args, cwd=None):
        cmd = ["git", "-c", "user.email=test", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
        subprocess.run(cmd + list(args), cwd=cwd or self.dir, check=True, capture_output=True)
