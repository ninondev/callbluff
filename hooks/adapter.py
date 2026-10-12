#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Runs the gates in hosts that give hooks no Claude Code transcript (Codex, DeepSeek Harness).

The gates read a Claude Code transcript. Codex passes its own rollout file instead, and
DeepSeek Harness's Claude Code hooks bridge passes an empty transcript_path. So this
adapter keeps a small transcript of its own, in the Claude Code row format, built from
the hook events themselves:

  adapter.py record        UserPromptSubmit and PostToolUse: append the prompt, or the
                           tool call and its result, to the session file.
  adapter.py gate NAME     PreToolUse and Stop: run hooks/guard_NAME.py with
                           transcript_path pointing at the session file, and pass its
                           answer back unchanged.

Session files live in $CALLBLUFF_HOME/sessions/ (default ~/.callbluff/sessions/), one per
session id, and stay on this computer.

Tool names are mapped to Claude Code's: Codex's Bash keeps its name and apply_patch
becomes one Write or Edit per file in the patch; DeepSeek Harness's bash, read, write,
edit and str_replace_editor become Bash, Read, Write and Edit.

Loop guard: DeepSeek Harness always reports stop_hook_active as false. So when the same
gate has blocked twice since the last prompt, the adapter lets the stop through.
Any failure of the adapter lets the action or the stop through.
"""
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_RESULT = 20000
MAX_BLOCKS = 2

TOOL_MAP = {"bash": "Bash", "read": "Read", "write": "Write", "edit": "Edit"}
PATCH_FILE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$", re.M)


def state_dir():
    base = os.environ.get("CALLBLUFF_HOME") or os.path.expanduser("~/.callbluff")
    return os.path.join(base, "sessions")


def session_file(payload):
    sid = re.sub(r"[^A-Za-z0-9._-]", "_", str(payload.get("session_id") or "unknown"))[:120]
    return os.path.join(state_dir(), sid + ".jsonl")


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def append_rows(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_rows(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            out = []
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
            return out
    except OSError:
        return []


def patch_calls(patch):
    """One (tool, input) pair per file in a Codex apply_patch body."""
    calls = []
    for m in PATCH_FILE.finditer(patch or ""):
        if m.group(1) == "Add":
            calls.append(("Write", {"file_path": m.group(2)}))
        elif m.group(1) == "Update":
            calls.append(("Edit", {"file_path": m.group(2)}))
        elif m.group(3):
            calls.append(("Write", {"file_path": m.group(3)}))
        # a deleted file is not something the session wrote
    return calls


def claude_calls(tool_name, tool_input):
    """Map a host tool call to Claude Code tool calls: a list of (name, input)."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool_name == "apply_patch":
        return patch_calls(tool_input.get("command") or tool_input.get("patch") or tool_input.get("input"))
    if tool_name == "str_replace_editor":
        cmd = tool_input.get("command")
        path = tool_input.get("path") or tool_input.get("file_path")
        if cmd == "create":
            return [("Write", {"file_path": path, "content": tool_input.get("file_text") or ""})]
        if cmd in ("str_replace", "insert"):
            return [("Edit", {"file_path": path, "old_string": tool_input.get("old_str") or "",
                              "new_string": tool_input.get("new_str") or ""})]
        if cmd == "view":
            return [("Read", {"file_path": path})]
        return []
    name = TOOL_MAP.get(tool_name, tool_name)
    return [(name, tool_input)]


def result_text(resp):
    if isinstance(resp, str):
        text = resp
    else:
        try:
            text = json.dumps(resp, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(resp)
    return text[:MAX_RESULT]


def record(payload):
    path = session_file(payload)
    ts = now_iso()
    event = payload.get("hook_event_name")
    if event == "UserPromptSubmit":
        prompt = payload.get("prompt") or ""
        append_rows(path, [{"type": "user", "timestamp": ts,
                            "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}}])
        return
    if event != "PostToolUse":
        return
    calls = claude_calls(payload.get("tool_name") or "", payload.get("tool_input"))
    if not calls:
        return
    base = str(payload.get("tool_use_id") or ts)
    msg = {"role": "assistant", "content": []}
    if payload.get("model"):
        msg["model"] = payload["model"]
    results = []
    for i, (name, inp) in enumerate(calls):
        uid = f"{base}-{i}"
        msg["content"].append({"type": "tool_use", "id": uid, "name": name, "input": inp})
        results.append({"type": "tool_result", "tool_use_id": uid,
                        "content": result_text(payload.get("tool_response"))})
    append_rows(path, [{"type": "assistant", "timestamp": ts, "message": msg},
                       {"type": "user", "timestamp": ts, "message": {"role": "user", "content": results}}])


class session_lock:
    """Hosts may start all Stop hooks at once; this keeps their read-then-append in order."""

    def __init__(self, path):
        self.path = path + ".lock"
        self.f = None

    def __enter__(self):
        try:
            import fcntl
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self.f = open(self.path, "a")
            fcntl.flock(self.f, fcntl.LOCK_EX)
        except (ImportError, OSError):
            self.f = None  # no lock on this platform: a duplicate row is harmless
        return self

    def __exit__(self, *exc):
        if self.f:
            self.f.close()  # closing releases the lock
        return False


def add_last_message(path, payload):
    """Append the reply that ends the turn, once, so the claim gates can read it."""
    text = payload.get("last_assistant_message")
    if not isinstance(text, str) or not text.strip():
        return
    with session_lock(path):
        for row in reversed(read_rows(path)):
            if row.get("type") == "assistant":
                blocks = (row.get("message") or {}).get("content") or []
                if any(isinstance(b, dict) and b.get("type") == "text" and b.get("text") == text for b in blocks):
                    return
                break
        msg = {"role": "assistant", "content": [{"type": "text", "text": text}]}
        if payload.get("model"):
            msg["model"] = payload["model"]
        append_rows(path, [{"type": "assistant", "timestamp": now_iso(), "message": msg}])


def blocks_since_prompt(path, gate):
    n = 0
    for row in reversed(read_rows(path)):
        if row.get("type") == "user" and any(
                isinstance(b, dict) and b.get("type") == "text"
                for b in ((row.get("message") or {}).get("content") or [])):
            break
        if row.get("type") == "system" and row.get("callbluff_gate") == gate:
            n += 1
    return n


def run_gate(gate, payload):
    script = os.path.join(HERE, f"guard_{gate}.py")
    r = subprocess.run([sys.executable, "-B", script], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout, r.stderr


def gate(name, payload):
    path = session_file(payload)
    event = payload.get("hook_event_name")
    base = dict(payload, transcript_path=path)
    if event == "Stop":
        add_last_message(path, payload)
        if blocks_since_prompt(path, name) >= MAX_BLOCKS:
            return 0, "", ""
        code, out, err = run_gate(name, base)
        if code == 2:
            append_rows(path, [{"type": "system", "timestamp": now_iso(), "callbluff_gate": name,
                                "content": f"Stop hook feedback:\n{err}"}])
        return code, out, err
    if event == "PreToolUse":
        for tool, inp in claude_calls(payload.get("tool_name") or "", payload.get("tool_input")):
            code, out, err = run_gate(name, dict(base, tool_name=tool, tool_input=inp))
            if code != 0 or out.strip():
                return code, out, err
        return 0, "", ""
    return 0, "", ""


def main(argv):
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        if len(argv) >= 2 and argv[1] == "record":
            record(payload)
            return 0
        if len(argv) >= 3 and argv[1] == "gate" and re.fullmatch(r"[a-z_]+", argv[2]):
            code, out, err = gate(argv[2], payload)
            if out:
                sys.stdout.write(out)
            if err:
                sys.stderr.write(err)
            return code
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
