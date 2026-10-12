#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""acceptance_file_guard (PreToolUse: Edit, Write, NotebookEdit, MultiEdit, Bash).

Editing a test or acceptance file is not forbidden. It is escalated to a user
confirmation (permissionDecision "ask"), because a common way to fake a fix is to
loosen the test until it passes. Legitimate test maintenance is normal work, so
the gate never denies.

What it leaves alone:
  - files that do not exist yet (a new test file cannot weaken anything);
  - files in scratch or temporary directories (/tmp, $TMPDIR, scratch/, tmp/ ...).
These two cases are neither asked about nor written to the fire log.

Modes (see hostctx.py and gates.json):
  - block host: ask for every edit of a protected file, with a 2 hour cooldown per file;
  - precision host: ask only when the session ran that test, it failed, and the test
    was then weakened (fewer assertions, new skip). Other weakening is logged in
    shadow mode only; plain maintenance passes silently.
A project can set its own list in .callbluff.json: {"protected_globs": ["..."]}.
"""
import fnmatch
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from firelog import log_fire  # noqa: E402
import hostctx  # noqa: E402

GATE = "acceptance_file_guard"
DEFAULT_GLOBS = [
    "test_*.py", "*_test.py", "tests/*", "tests/**", "conftest.py",
    "*.test.js", "*.test.ts", "*.spec.js", "*.spec.ts", "__tests__/*", "__tests__/**",
    "*_test.go", "spec/*", "spec/**",
]
SCRATCH_PARTS = {"tmp", "temp", "scratch", "scratchpad", ".tmp", ".scratch"}


def load_globs(cwd):
    p = os.path.join(cwd or ".", ".callbluff.json")
    try:
        with open(p, encoding="utf-8") as f:
            g = json.load(f).get("protected_globs")
        if isinstance(g, list) and g:
            return [str(x) for x in g]
    except Exception:
        pass
    return DEFAULT_GLOBS


def is_protected(path, cwd, globs):
    rel = os.path.relpath(path, cwd) if cwd and os.path.isabs(path) else path
    rel = rel.replace(os.sep, "/")
    base = os.path.basename(rel)
    for g in globs:
        if fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(base, g):
            return True
        if g.endswith("/**") and rel.startswith(g[:-3] + "/"):
            return True
    return False


def _temp_roots():
    roots = {"/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp", "/var/folders", "/private/var/folders"}
    for cand in (tempfile.gettempdir(), os.environ.get("TMPDIR", "")):
        if cand:
            roots.add(os.path.realpath(cand))
            roots.add(os.path.abspath(cand))
    return roots


def is_scratch(abs_path, cwd=""):
    """True for a path inside a temporary or scratch directory.

    Two rules: the path lies under a system temp root, or one of its directory names
    is tmp, temp, scratch and so on. The name rule looks only at the part of the path
    below the project directory, so a project that happens to live in ~/tmp/app is
    not treated as scratch.
    """
    p = os.path.abspath(abs_path)
    real = os.path.realpath(p)
    for root in _temp_roots():
        root = root.rstrip("/")
        if root and (p == root or p.startswith(root + "/") or real.startswith(root + "/")):
            return True
    base = os.path.abspath(cwd) if cwd else ""
    rel = os.path.relpath(p, base) if base and p.startswith(base + os.sep) else p
    parts = {x.lower() for x in rel.split(os.sep)[:-1]}
    return bool(parts & SCRATCH_PARTS)


BASH_WRITE = re.compile(r"(?:>>?|\btee\b|\bcp\b\s+\S+|\bmv\b\s+\S+)\s*([\w./-]*(?:test|spec|conftest)[\w./-]*)", re.I)


def bash_write_target(cmd):
    """Detect a shell command that writes a test file (`cat >> tests/test_x.py` skips the Edit hook)."""
    m = BASH_WRITE.search(cmd or "")
    return m.group(1) if m else ""


ASSERT_RE = re.compile(r"\bassert\w*\b|\bexpect\s*\(|\.to(?:Be|Equal|Match|Throw|Have|Contain)\w*\s*\("
                       r"|\bshould\b|\bt\.(?:Errorf?|Fatalf?)\s*\(|\bXCTAssert\w*", re.I)
SKIP_RE = re.compile(r"pytest\.mark\.(?:skip|skipif|xfail)|unittest\.skip|@skip\b"
                     r"|\b(?:it|test|describe)\.(?:skip|only)\s*\(|\bx(?:it|test|describe)\s*\("
                     r"|@Ignore\b|\bt\.Skip\s*\(", re.I)


def weakening(tool_name, tool_input, abs_path):
    """Describe how an edit weakens a test (fewer assertions, new skip). Empty string if it does not."""
    if tool_name == "Edit":
        old = tool_input.get("old_string") or ""
        new = tool_input.get("new_string") or ""
    elif tool_name in ("Write", "NotebookEdit"):
        new = tool_input.get("content") or tool_input.get("new_source") or ""
        try:
            with open(abs_path, encoding="utf-8", errors="replace") as f:
                old = f.read()
        except OSError:
            return ""
    elif tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if ">>" in cmd:
            return ""  # appending does not weaken
        return "shell command overwrites or moves a test file; content cannot be checked"
    else:
        return ""
    a, b = len(ASSERT_RE.findall(old)), len(ASSERT_RE.findall(new))
    if b < a:
        return f"assertions {a} -> {b}"
    if len(SKIP_RE.findall(new)) > len(SKIP_RE.findall(old)):
        return "new skip/only/xfail marker"
    return ""


FAIL_MARK = re.compile(r"\bFAIL(?:ED|URES?)?\b|\bError\b|Traceback|AssertionError|\bfailed\b", re.I)


def failed_run_before(lines, test_path):
    """True if this session ran the test file earlier and the output shows a failure."""
    base = os.path.basename(test_path)
    stem = os.path.splitext(base)[0]
    keys = {base, stem}
    if stem.startswith("test_"):
        keys.add(stem[5:])
    pending = False
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        for blk in ((row.get("message") or {}).get("content") or []):
            if not isinstance(blk, dict):
                continue
            if blk.get("type") == "tool_use" and blk.get("name") == "Bash":
                cmd = str((blk.get("input") or {}).get("command", ""))
                pending = any(k in cmd for k in keys) and bool(
                    re.search(r"pytest|unittest|python3?\b|node|go test|cargo|npm test|make test", cmd))
            elif blk.get("type") == "tool_result" and pending:
                if FAIL_MARK.search(str(blk.get("content", ""))):
                    return True
                pending = False
    return False


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    tool_input = payload.get("tool_input") or {}
    tool_name = payload.get("tool_name") or ""
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not path and tool_name == "Bash":
        path = bash_write_target(tool_input.get("command", ""))
    if not path:
        return 0
    cwd = payload.get("cwd") or ""
    if not is_protected(path, cwd, load_globs(cwd)):
        return 0
    abs_path = path if os.path.isabs(path) else os.path.join(cwd or ".", path)
    if is_scratch(abs_path, cwd):
        return 0  # scratch and temporary directories are not acceptance files
    if not os.path.exists(abs_path):
        return 0  # a new file cannot weaken an existing test
    tpath = payload.get("transcript_path")
    extra = ""
    mode = hostctx.mode_for(GATE, tpath, payload.get("agent_id"))
    if mode == "precision":
        w = weakening(tool_name, tool_input, abs_path)
        if not w:
            return 0
        extra = f" (weakening: {w})"
        lines = hostctx.tail_lines(tpath, hostctx.COOLDOWN_TAIL) if tpath else []
        if tool_name == "Bash" or not failed_run_before(lines, abs_path):
            log_fire(GATE, os.path.basename(path) + extra + " [no red run before]", cwd, tpath or "", "shadow")
            return 0
        extra += " [red run, then weakened]"
    sig = f"callbluff:{GATE}] Target is a test or acceptance file: {os.path.basename(path)}"
    if tpath:
        try:
            if hostctx.recently_fired(hostctx.tail_lines(tpath, hostctx.COOLDOWN_TAIL), sig):
                return 0
        except OSError:
            pass
    log_fire(GATE, os.path.basename(path) + extra, cwd, tpath or "")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": (
                f"[{sig}{extra}. Changing a test to make a task pass hides a failure instead of fixing it. "
                "If this is real test maintenance, please confirm."),
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
