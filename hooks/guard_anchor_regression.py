#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""anchor_regression (Stop hook): you changed the ruler, so measure again.

Fires when all of these hold:
  1. The session wrote a detector-type file: a guard, a grader, a probe or the anchor
     suite itself (see DETECTOR), in a tree that has tests/selftest.py.
  2. After the last such write, no anchor command ran successfully. A command that
     mentions selftest, pytest or unittest counts only if its output shows a pass
     (N/N passed, PASS, OK) and no failure. A command that was typed but never
     executed, or ran and ended red, does not count.
Result: block once and ask for a rerun of the anchor suite before closing.
Scope is narrow on purpose: a repository without tests/selftest.py is not governed
by this gate. Loop guard: stop_hook_active, plus a 2 hour cooldown.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from firelog import log_fire  # noqa: E402
import hostctx  # noqa: E402
from _shared import WRITE_TOOLS  # noqa: E402

GATE = "anchor_regression"
SIGNATURE = f"callbluff:{GATE}"
DETECTOR = re.compile(r"^(guard_\w+\.py|grade_\w+\.py|probes\.py|selftest\.py)$")
ANCHOR_CMD = re.compile(r"selftest|pytest|unittest")
# Zero counts are not failures: FAIL=0, FAIL: 0, 0 FAIL, 0 failed, errors=0.
ZERO_COUNT = re.compile(r"\b(?:FAIL(?:ED|URES?)?|ERRORS?)\s*[=:]\s*0\b|\b0\s+(?:FAIL(?:ED|URES?)?|ERRORS?)\b", re.I)
PASS_STYLE = re.compile(r"\d+/\d+\s*(?:passed|pass|ok)\b", re.I)


def has_anchor_set(path):
    """The detector file must sit in a tree that has tests/selftest.py (within 7 levels up)."""
    d = os.path.dirname(os.path.abspath(path)) if path else ""
    for _ in range(7):
        if not d:
            return False
        if os.path.exists(os.path.join(d, "tests", "selftest.py")) or (
                os.path.basename(d) == "tests" and os.path.exists(os.path.join(d, "selftest.py"))):
            return True
        nd = os.path.dirname(d)
        if nd == d:
            return False
        d = nd
    return False


def anchor_succeeded(out):
    up = ZERO_COUNT.sub("", out).upper()
    if "FAIL" in up:
        return False
    if not (PASS_STYLE.search(out) or "PASS" in up or re.search(r"\bOK\b", up)):
        return False
    return "NOT FOUND" not in up and "ERROR" not in up


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if payload.get("stop_hook_active"):
        return 0
    tpath = payload.get("transcript_path")
    if not tpath:
        return 0
    detectors_touched = []
    anchored_after = True
    pending_anchor = False
    already_warned = False
    try:
        with open(tpath, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        already_warned = hostctx.recently_fired(lines, SIGNATURE)
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            for blk in ((row.get("message") or {}).get("content") or []):
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "tool_result" and pending_anchor:
                    if anchor_succeeded(str(blk.get("content", ""))):
                        anchored_after = True
                    pending_anchor = False
                    continue
                if blk.get("type") != "tool_use":
                    continue
                name = blk.get("name", "")
                inp = blk.get("input") or {}
                if name in WRITE_TOOLS:
                    p = inp.get("file_path") or inp.get("notebook_path") or ""
                    if DETECTOR.match(os.path.basename(p)) and has_anchor_set(p):
                        detectors_touched.append(os.path.basename(p))
                        anchored_after = False
                        pending_anchor = False
                elif name == "Bash" and ANCHOR_CMD.search(str(inp.get("command", ""))):
                    pending_anchor = True
    except OSError:
        return 0
    if already_warned or anchored_after or not detectors_touched:
        return 0
    uniq = sorted(set(detectors_touched))
    log_fire(GATE, f"detectors={uniq[:4]}", payload.get("cwd") or "", tpath or "")
    sys.stderr.write(
        f"[{SIGNATURE}] This session changed detector files ({', '.join(uniq[:4])}) and did not rerun the "
        "anchor suite afterwards. If you change the ruler you must measure again: run tests/selftest.py "
        "(or the matching --selftest), and close only when it is green. If a check fails, log it; do not "
        "change the standard to fit.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
