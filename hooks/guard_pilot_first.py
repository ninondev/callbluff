#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""pilot_first (PreToolUse: Edit, Write): try one file before you roll out to all of them.

Fires when all of these hold:
  0. The target is a code file. Prose files (.md, .txt, .markdown, .rst) are exempt:
     numbered drafts such as draft_v7.md and draft_v8.md are not "isomorphic modules".
  1. The session already wrote at least 3 files with the same name once digits are
     ignored (mod_5.py and mod_11.py count as the same shape).
  2. Nothing was run to verify them since (no python, pytest, unittest, node, go test ...).
  3. The file about to be written has that same shape again.
Result: deny once and ask the agent to verify one written file first. After a
verification the condition no longer holds, so the next write passes.

Loop guard: a 2 hour cooldown. Strong hosts run it in shadow mode (logged, not denied);
see gates.json.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from firelog import log_fire  # noqa: E402
import hostctx  # noqa: E402
from _shared import WRITE_TOOLS, VERIFY, canon  # noqa: E402

GATE = "pilot_first"
SIGNATURE = f"callbluff:{GATE}"
PROSE_EXT = {".md", ".markdown", ".txt", ".rst"}


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    target = (payload.get("tool_input") or {}).get("file_path") or ""
    tpath = payload.get("transcript_path")
    if not target or not tpath:
        return 0
    if os.path.splitext(target)[1].lower() in PROSE_EXT:
        return 0
    written = []
    verified_after = True
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
                if not isinstance(blk, dict) or blk.get("type") != "tool_use":
                    continue
                name = blk.get("name", "")
                if name in WRITE_TOOLS:
                    p = (blk.get("input") or {}).get("file_path") or (blk.get("input") or {}).get("notebook_path")
                    if p:
                        written.append(p)
                        verified_after = False
                elif name == "Bash" and VERIFY.search(str((blk.get("input") or {}).get("command", ""))):
                    verified_after = True
    except OSError:
        return 0
    if already_warned or verified_after:
        return 0
    sig = canon(target)
    same = [p for p in written if canon(p) == sig and os.path.basename(p) != os.path.basename(target)]
    if len(same) >= 3:
        mode = hostctx.mode_for(GATE, tpath, payload.get("agent_id"))
        log_fire(GATE, f"same={len(same)} target={os.path.basename(target)}", "", tpath or "", mode)
        if mode == "shadow":
            return 0
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"[{SIGNATURE}] You have written {len(same)} files of the same shape "
                    f"({os.path.basename(same[0])} and others) and verified none of them. "
                    "Pilot first: pick one written file and run its check (run it, import it, test it) to "
                    "confirm the change is safe on the odd cases, then continue. A batch can hide a module "
                    "that does not fit."),
            }
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
