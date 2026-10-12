# SPDX-License-Identifier: GPL-3.0-only
"""Fire log: every time a gate fires, append one line to ~/.callbluff/firelog.jsonl.

Local only, standard library only. A failed write is ignored: the log must never
get in the way of a gate or a session.
"""
import json
import os
import time


def log_fire(gate, detail="", cwd="", tpath="", mode="block"):
    if os.environ.get("CALLBLUFF_NO_FIRELOG"):
        return
    try:
        d = os.environ.get("CALLBLUFF_HOME") or os.path.expanduser("~/.callbluff")
        os.makedirs(d, exist_ok=True)
        row = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "gate": gate,
            "cwd": cwd or os.getcwd(),
            "detail": str(detail)[:200],
        }
        if tpath:  # lets you go back to the transcript and judge the fire later
            row["transcript_path"] = tpath
        if mode != "block":  # shadow = logged but not blocked
            row["mode"] = mode
        with open(os.path.join(d, "firelog.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass
