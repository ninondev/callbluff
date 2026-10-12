#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""false_completion (Stop hook): "fixed" with nothing written. English and Chinese wording.

Fires when all of these hold:
  1. The last assistant text contains a clear completion claim ("I fixed it",
     "the issue is resolved", "optimization is done" ...), outside quotes and
     not negated.
  2. The whole session has no Edit, Write, MultiEdit or NotebookEdit call.
  3. The whole session has no shell write or commit (git commit, git apply,
     redirect, tee, sed -i, patch).
Loop guard: stop_hook_active lets the stop through. A gate failure lets it through too.
Strong hosts run it in shadow mode (logged, not blocked).
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from firelog import log_fire  # noqa: E402
import hostctx  # noqa: E402
from _shared import WRITE_TOOLS, strip_quoted, transcript_rows  # noqa: E402

GATE = "false_completion"
CLAIM = re.compile(
    r"\b(?:i(?:'ve| have)?|we(?:'ve| have)?)\s+(?:now\s+)?(?:fixed|resolved|implemented|optimi[sz]ed)\b"
    r"|\b(?:it|this|that|the (?:bug|issue|problem|error|crash|test|code))\s+(?:is|are|was|has been|have been)\s+(?:now\s+)?(?:fixed|resolved|solved)\b"
    r"|\b(?:has|have) been (?:fixed|resolved|implemented)\b"
    r"|\ball (?:fixed|set)\b"
    r"|\b(?:fix|implementation|optimi[sz]ation|change|refactor)\s+(?:is\s+)?(?:done|complete[d]?|finished|in place)\b"
    r"|\b(?:issue|problem|bug)\s+(?:is\s+)?(?:resolved|solved|fixed)\b",
    re.I)
# Chinese completion phrases (generic words only).
CLAIM_ZH = re.compile(
    r"修好了|已修复|修复完成|已经?修复|问题已解决|已解决|优化完成|优化好了|改好了|已完成修改|实现完成|"
    r"已经?实现|搞定了?|做完了|完成了(?:修复|优化|改造|实现)?")
NEGATED_ZH = re.compile(r"[没未不别还尚将会若如待等][^。！？\n]{0,4}$")
NEGATED = re.compile(r"(?:\bnot\b|n't\b|\bnever\b|\bno\b|\bnothing\b|\bwill\b|\bwould\b|\bcould\b|\bshould\b|\bif\b|\bonce\b|\bunless\b)[^.!?\n]{0,25}$", re.I)
SHELL_WRITE = re.compile(r"git (commit|apply)|>\s*\S|\btee\b|\bsed -i\b|\bpatch\b")


def claims(text):
    body = strip_quoted(text)
    for m in CLAIM.finditer(body):
        before = body[max(0, m.start() - 40):m.start()]
        before = re.split(r"[.!?\n]", before)[-1]
        if NEGATED.search(before + " "):
            continue
        return True
    for m in CLAIM_ZH.finditer(body):
        before = re.split(r"[。！？!?\n]", body[max(0, m.start() - 12):m.start()])[-1]
        if NEGATED_ZH.search(before):
            continue
        return True
    return False


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
    wrote = False
    last_text = ""
    for row in transcript_rows(tpath):
        msg = row.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        texts = []
        for blk in content:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") == "tool_use":
                name = blk.get("name", "")
                if name in WRITE_TOOLS:
                    wrote = True
                if name == "Bash" and SHELL_WRITE.search(str((blk.get("input") or {}).get("command", ""))):
                    wrote = True
            elif blk.get("type") == "text":
                texts.append(blk.get("text", ""))
        if (row.get("type") == "assistant" or msg.get("role") == "assistant") and texts:
            last_text = "\n".join(texts)
    if wrote or not last_text:
        return 0
    if claims(last_text):
        mode = hostctx.mode_for(GATE, tpath, payload.get("agent_id"))
        log_fire(GATE, last_text[:120], payload.get("cwd", ""), tpath or "", mode)
        if mode == "shadow":
            return 0
        sys.stderr.write(
            f"[callbluff:{GATE}] This reply says something is fixed or done, but the session has no file write "
            "and no commit. Do the work and leave a change that can be checked. If you only proposed a fix, "
            "say \"proposed, not applied\". If the work happened outside this session, say where and how to "
            "verify it.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
