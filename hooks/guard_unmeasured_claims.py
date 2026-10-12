#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""unmeasured_claim (Stop hook): performance numbers without a measurement.

Fires when the last assistant text states a measured-sounding performance result
("3x faster", "cut latency by 40%", "from 120 ms to 40 ms") and the session has no
trace of measuring: no benchmark or timing command, and no timing output.
The word list is deliberately narrow. Complexity arguments ("O(n) instead of
O(n^2)") do not fire.
Loop guard: stop_hook_active. Strong hosts run it in shadow mode.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from firelog import log_fire  # noqa: E402
import hostctx  # noqa: E402
from _shared import strip_quoted  # noqa: E402

GATE = "unmeasured_claim"
NUM = r"\d+(?:\.\d+)?"
UNIT = r"(?:ms|msec|milliseconds?|s|secs?|seconds?)"
PERF_CLAIM = re.compile(
    rf"\b{NUM}\s*(?:x|times)\s+(?:faster|quicker|speed-?up)\b"
    rf"|\b{NUM}\s*%\s+(?:faster|quicker|less (?:time|latency))\b"
    rf"|\b(?:faster|quicker|speed-?up)\s+by\s+(?:about\s+|roughly\s+|around\s+)?{NUM}\s*(?:x|times|%)"
    rf"|\b(?:cut|cuts|cutting|reduc(?:e|ed|es|ing)|lower(?:ed|s)?|improv(?:e|ed|es|ing)|speed(?:s|ed)? up|shorten(?:ed|s)?)\b[^.\n]{{0,40}}?\bby\s+(?:about\s+|roughly\s+|around\s+)?{NUM}\s*(?:x|times|%)"
    rf"|\b(?:down|dropped|drops|reduced|cut)\s+to\s+{NUM}\s*{UNIT}\b"
    rf"|\b{NUM}\s*{UNIT}\s*(?:->|→|to)\s*{NUM}\s*{UNIT}\b"
    rf"|\bspeed-?up of {NUM}",
    re.I)
# Chinese performance claims (generic words only).
ZH_NUM = r"\d+(?:\.\d+)?"
ZH_UNIT = r"(?:ms|毫秒|s|秒)"
PERF_CLAIM_ZH = re.compile(
    rf"快了?\s*(?:约|大概|大约)?\s*{ZH_NUM}\s*(?:倍|%)"
    rf"|(?:提升|提高|加速|降低|减少)了?\s*(?:约|大概|大约)?\s*{ZH_NUM}\s*(?:倍|%)"
    rf"|(?:降|缩短)(?:到|至)\s*{ZH_NUM}\s*{ZH_UNIT}"
    rf"|{ZH_NUM}\s*{ZH_UNIT}\s*(?:→|->|变成|降到)\s*{ZH_NUM}\s*{ZH_UNIT}")
MEASURE_CMD = re.compile(r"\b(time|timeit|hyperfine|bench|benchmark|perf stat|pytest-benchmark|ab -n|wrk)\b")
MEASURE_OUT = re.compile(r"\d+(\.\d+)?\s*(ms|µs|us|ns)\b|\breal\s+\d+m|\bMean\b.*\bms\b|\belapsed\b|耗时|用时\s*\d|\btook\s+\d+(?:\.\d+)?\s*(?:ms|s|sec)\b")


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
    measured = False
    last_text = ""
    try:
        with open(tpath, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = row.get("message") or {}
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                texts = []
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    t = blk.get("type")
                    if t == "tool_use" and blk.get("name") == "Bash":
                        if MEASURE_CMD.search(str((blk.get("input") or {}).get("command", ""))):
                            measured = True
                    elif t == "tool_result":
                        raw = blk.get("content")
                        s = raw if isinstance(raw, str) else json.dumps(raw) if raw else ""
                        if MEASURE_OUT.search(s):
                            measured = True
                    elif t == "text":
                        texts.append(blk.get("text", ""))
                if (row.get("type") == "assistant" or msg.get("role") == "assistant") and texts:
                    last_text = "\n".join(texts)
    except OSError:
        return 0
    if measured or not last_text:
        return 0
    body = strip_quoted(last_text)
    if PERF_CLAIM.search(body) or PERF_CLAIM_ZH.search(body):
        mode = hostctx.mode_for(GATE, tpath, payload.get("agent_id"))
        log_fire(GATE, last_text[:120], payload.get("cwd", ""), tpath or "", mode)
        if mode == "shadow":
            return 0
        sys.stderr.write(
            f"[callbluff:{GATE}] The reply gives a concrete performance number, but nothing in the session "
            "measured anything (no benchmark, no timing). Numbers must come from a measurement: build a "
            "minimal benchmark and report before and after, or rewrite the claim as \"expected gain, not "
            "measured\".")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
