#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""duration_claim (Stop hook): "I worked 8 hours" when the clock says 6. English and Chinese wording.

Fires when all of these hold:
  1. The last assistant text states how long the agent worked ("worked for 8 hours
     straight", "non-stop for 8h", "wrapped up after 8 hours").
  2. The claimed time is more than 25% above the real span of the transcript (first
     to last timestamp) and more than half an hour above it.
Result: block once, report the real span, ask for a corrected figure.
Narrow on purpose: plans and estimates ("expect 2 more hours", "the batch needs 2 hours")
and quoted text do not count. Loop guard: stop_hook_active, plus a 2 hour cooldown.
Strong hosts run it in shadow mode.
"""
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from firelog import log_fire  # noqa: E402
import hostctx  # noqa: E402
from _shared import strip_quoted  # noqa: E402

GATE = "duration_claim"
SIGNATURE = f"callbluff:{GATE}"
HOURS = r"(?:-?hours?|hrs?|h)\b"
CLAIM = re.compile(
    r"(?:worked|working|been at it|spent|ran|running|non-?stop|nonstop|straight|continuously|all day|wrapped up(?: after| in)?)"
    r"[^,.;\n]{0,15}?(\d+(?:\.\d+)?)\s*" + HOURS +
    r"|(\d+(?:\.\d+)?)\s*" + HOURS + r"\s*(?:of (?:continuous|straight|non-?stop) work|non-?stop|straight|marathon|session)",
    re.I)
CLAIM_ZH = re.compile(
    r"(?:连续|不间断|全程|收官|干了|工作了|跑了|奋战)[^,。;\n]{0,10}?(\d+(?:\.\d+)?)\s*(?:个?小时|h\b)"
    r"|(\d+(?:\.\d+)?)\s*(?:个?小时|h)\s*(?:收官|不间断|连续)")
PLAN_ZH = re.compile(r"预计|还要|再等|大约需要|计划|将要|即将")
PLAN_WORDS = re.compile(r"\b(?:expect|expected|estimate|estimated|another|need|needs|will|plan|planned|about to|going to|remaining|more)\b", re.I)


def parse_ts(line):
    m = re.search(r'"timestamp"\s*:\s*"([^"]+)"', line)
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


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
    first_ts = last_ts = None
    last_text = ""
    try:
        with open(tpath, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return 0
    already = hostctx.recently_fired(lines, SIGNATURE)
    for line in lines:
        ts = parse_ts(line)
        if ts is not None:
            if first_ts is None:
                first_ts = ts
            last_ts = ts
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("type") != "assistant":
            continue
        for blk in ((row.get("message") or {}).get("content") or []):
            if isinstance(blk, dict) and blk.get("type") == "text" and blk.get("text"):
                last_text = blk["text"]
    if already or not last_text or first_ts is None or last_ts is None or last_ts <= first_ts:
        return 0
    actual_h = (last_ts - first_ts) / 3600.0
    last_text = strip_quoted(last_text)
    matches = [(m, PLAN_WORDS) for m in CLAIM.finditer(last_text)] + \
              [(m, PLAN_ZH) for m in CLAIM_ZH.finditer(last_text)]
    for m, plan in matches:
        ctx = last_text[max(0, m.start() - 20):m.start()]
        if plan.search(ctx) or plan.search(m.group(0)):
            continue
        claimed = float(m.group(1) or m.group(2))
        if claimed > actual_h * 1.25 and claimed - actual_h > 0.5:
            mode = hostctx.mode_for(GATE, tpath, payload.get("agent_id"))
            log_fire(GATE, f"claimed {claimed}h actual {actual_h:.1f}h", payload.get("cwd") or "", tpath or "", mode)
            if mode == "shadow":
                return 0
            sys.stderr.write(
                f"[{SIGNATURE}] You claimed {claimed} hours of work, but this session spans {actual_h:.1f} hours "
                "by its timestamps, so the claim is more than 25% too high. Use the timestamps for any time you report, "
                "fix the number or give an honest range, and do not count waiting as continuous work.")
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
