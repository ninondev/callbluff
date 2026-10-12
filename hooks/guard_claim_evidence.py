#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""claim_evidence (Stop hook): show the evidence before you claim it. English and Chinese wording.

Two shapes:
  A. Contact claim. "I read 101 files" while the session made far fewer Read, Grep,
     Glob or shell-read calls. Fires when the claimed count is at least 5 and the
     real count is below max(2, 20% of the claim).
  B. Verification claim. "Tests pass" or "I verified it, all good" while the session
     has no trace of running anything.
Exempt: quoted text; a reply that says it is not verified yet; a claim sentence that
limits itself ("the script scanned 80, I did not read 80").
Loop guard: stop_hook_active, plus a 2 hour cooldown. Strong hosts run it in shadow mode.
Not covered: claims that cite a stale number or a fact from memory.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from firelog import log_fire  # noqa: E402
import hostctx  # noqa: E402
from _shared import strip_quoted  # noqa: E402

GATE = "claim_evidence"
SIGNATURE = f"callbluff:{GATE}"
CLAIM_A = re.compile(
    r"\b(?:i|we)?\s*(?:have\s+|'ve\s+)?(?:read|reviewed|went through|absorbed|scanned|processed|opened|looked at|checked)\s+"
    r"(?:through\s+)?(?:all\s+)?(?:of\s+)?(?:the\s+)?(\d{1,4})\s+"
    r"(?:\w+\s+)?(?:files?|documents?|docs|records?|sessions?|transcripts?|pages?|articles?|cards?)\b", re.I)
CLAIM_B = re.compile(
    r"\b(?:(?:i|we)\s+)?(?:have\s+)?(?:verified|tested|ran the tests?|checked)\b[^.\n]{0,40}?"
    r"\b(?:pass(?:es|ed|ing)?|all green|works?|working|correct|no (?:problems?|issues?|errors?)|fine|ok)\b"
    r"|\b(?:all|the)\s+tests?\s+(?:are\s+)?(?:pass(?:es|ed|ing)?|green)\b"
    r"|\btests?\s+(?:all\s+)?pass(?:ed)?\b", re.I)
CLAIM_A_ZH = re.compile(r"(?:读|听|看|吸收|扫|过)(?:了|完|过)[^,。;\n]{0,12}?(\d{1,4})\s*(?:个|份|篇|张|条)?\s*(?:文件|文档|语料|卡|篇章|会话|records?|files?)")
CLAIM_B_ZH = re.compile(
    r"(?:验证|测试|自测|跑)(?:过|完|了)[^,。;\n]{0,15}?(?:通过|全绿|全部通过|正常|没问题|无误|OK|pass)"
    r"|(?:全部|所有)?(?:测试|用例)(?:全绿|全部通过)|(?<![不未没])(?:测试|自测|检查)通过|已(?:经)?验证|全部通过")
NEG_ZH = re.compile(r"未验证|待验证|尚未|还没|没有验证|没跑|未运行|没运行")
SELF_LIMIT_ZH = re.compile(r"不是|并非|不到|少于|而非|没有|不等于|只是程序|程序扫")
NEG = re.compile(r"\b(?:unverified|not verified|not yet verified|not tested|untested|have not|haven't|did not|didn't|"
                 r"yet to|not yet|never ran|could not|couldn't)\b", re.I)
SELF_LIMIT = re.compile(r"\b(?:not|only|fewer|less than|just the script|the script|the program|didn't|did not)\b", re.I)
RUN_CMD = re.compile(r"\b(?:pytest|unittest|python3?\s|node\s|npm test|go test|cargo test|make test|bash\s|sh\s|\./)")
READ_TOOLS = {"Read", "Grep", "Glob"}
BASH_READ = re.compile(r"\b(?:cat|sed|head|tail|less|more|grep|rg|awk|jq|wc|bat)\b")


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
    last_text = ""
    n_reads = 0
    ran = False
    try:
        with open(tpath, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return 0
    already = hostctx.recently_fired(lines, SIGNATURE)
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        for blk in ((row.get("message") or {}).get("content") or []):
            if not isinstance(blk, dict):
                continue
            if blk.get("type") == "tool_use":
                name = blk.get("name", "")
                if name in READ_TOOLS:
                    n_reads += 1
                elif name == "Bash":
                    cmd = str((blk.get("input") or {}).get("command", ""))
                    if BASH_READ.search(cmd):
                        n_reads += 1
                    if RUN_CMD.search(cmd):
                        ran = True
            elif row.get("type") == "assistant" and blk.get("type") == "text" and blk.get("text"):
                last_text = blk["text"]
    if already or not last_text:
        return 0
    body = strip_quoted(last_text)
    if NEG.search(body) or NEG_ZH.search(body):
        return 0
    mode = hostctx.mode_for(GATE, tpath, payload.get("agent_id"))
    for m, limit in [(x, SELF_LIMIT) for x in CLAIM_A.finditer(body)] + \
                    [(x, SELF_LIMIT_ZH) for x in CLAIM_A_ZH.finditer(body)]:
        n = int(m.group(1))
        window = body[max(0, m.start() - 20):m.end() + 40]
        if limit.search(window):
            continue
        if n >= 5 and n_reads < max(2, n * 0.2):
            log_fire(GATE, f"A: claimed contact with {n}, real reads {n_reads}", payload.get("cwd") or "", tpath or "", mode)
            if mode == "shadow":
                return 0
            sys.stderr.write(
                f"[{SIGNATURE}] You claim you went through {n} items, but this session made only {n_reads} "
                "Read/Grep calls. Show the evidence first: do the reading, or reduce the claim to what you "
                "really read (name the few items you opened).")
            return 2
    if (CLAIM_B.search(body) or CLAIM_B_ZH.search(body)) and not ran:
        log_fire(GATE, "B: verification claim, no run in session", payload.get("cwd") or "", tpath or "", mode)
        if mode == "shadow":
            return 0
        sys.stderr.write(
            f"[{SIGNATURE}] You claim the work was verified or the tests pass, but this session never ran a "
            "command. Show the evidence first: run it and leave the output in the session, or change "
            "\"verified\" to \"not run\".")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
