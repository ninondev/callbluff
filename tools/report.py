#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""callbluff report: read ~/.callbluff/firelog.jsonl and print gate fire counts.

Behind the /callbluff:report command; also runnable by hand. Standard library only,
local data only. Usage: report.py [--days=N | --all]
"""
import json
import os
import sys
import time
from collections import Counter


def log_path():
    d = os.environ.get("CALLBLUFF_HOME") or os.path.expanduser("~/.callbluff")
    return os.path.join(d, "firelog.jsonl")


def load(days=None):
    p = log_path()
    if not os.path.exists(p):
        return []
    rows = []
    cutoff = None
    if days:
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    with open(p, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cutoff and r.get("ts", "")[:10] < cutoff:
                continue
            rows.append(r)
    return rows


def main():
    days = 7
    for a in sys.argv[1:]:
        if a.startswith("--days="):
            days = int(a.split("=")[1])
        elif a == "--all":
            days = None
    rows = load(days)
    span = f"last {days} days" if days else "all history"
    if not rows:
        print(f"callbluff report ({span}): no fires. The gates are on and nothing tripped.")
        return
    blocked = [r for r in rows if r.get("mode", "block") == "block"]
    shadow = [r for r in rows if r.get("mode") == "shadow"]
    print(f"callbluff report ({span}): {len(rows)} fires ({len(blocked)} blocked or asked, {len(shadow)} logged in shadow mode)\n")
    reg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gates.json")
    with open(reg_path, encoding="utf-8") as f:
        label = {k: v.get("desc", "") for k, v in json.load(f).items() if not k.startswith("_")}
    for gate, n in Counter(r["gate"] for r in rows).most_common():
        print(f"  {gate} x{n}: {label.get(gate, '')}")
    by_proj = Counter(os.path.basename(r.get("cwd", "") or "?") for r in rows)
    if len(by_proj) > 1:
        print("\nBy project:")
        for proj, n in by_proj.most_common(8):
            print(f"  {proj} x{n}")
    print("\nLast 5 fires:")
    for r in rows[-5:]:
        print(f"  [{r['ts']}] {r['gate']}: {r.get('detail', '')[:70]}")
    print("\nA fire is not proof of a mistake. Open the transcript path in the log to judge it.")


if __name__ == "__main__":
    main()
