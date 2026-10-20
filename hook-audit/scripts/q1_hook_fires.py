# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ninon
"""Q1, step 1: count guard-hook fires and draw a stratified sample for labeling.

A "fire" is a hook that blocked the agent. Two sources are supported:

  * Transcripts (default). Three record shapes count as a fire:
      - a system row with subtype "stop_hook_summary" and non-empty hookErrors
        (a Stop hook that sent the agent back to work);
      - a tool_result with is_error set whose text starts with
        "<Event>:<Tool> hook error" (a PreToolUse/PostToolUse block);
      - an attachment of type "hook_blocking_error".
    Shadow-mode hooks that only log never show up in a transcript.
  * A fire log (--firelog): JSON lines with at least "gate" (or "hook") and
    "ts"; optional "mode" and "transcript_path". Use this if your hooks keep
    their own log, which is the only way to see shadow-mode fires.

Fires repeated in forked or resumed transcripts are counted once, keyed on
(timestamp, hook, event).

stdout shows only counts per hook. With --sample-out, a CSV of locators
(file, line, timestamp) is written for a rater to fill in; it holds no
message text.

Usage:
  python3 -I q1_hook_fires.py [ROOT] [--exclude REGEX]
      [--firelog FILE] [--judged FILE]
      [--per-hook N] [--alloc HOOK|EVENT=N ...] [--seed N] [--sample-out FILE.csv]
"""
import argparse
import collections
import csv
import json
import os
import random
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DEFAULT_ROOT, all_transcripts, block_text, blocks, parse_ts, rows  # noqa: E402

TOOL_HOOK_ERR = re.compile(r"^\s*(\w+)(?::(\w+))?\s+hook error\b")
BRACKET_CMD = re.compile(r"^\s*\[([^\]]+)\]")
SCRIPT = re.compile(r"[\w.\-]+\.(?:py|sh|zsh|bash|js|mjs|ts|rb|pl|go)\b")


def hook_name(text, fallback="unknown"):
    """Best-effort hook id: the script basename named in the command."""
    m = BRACKET_CMD.match(text or "")
    cmd = m.group(1) if m else (text or "")
    names = SCRIPT.findall(cmd)
    if names:
        return os.path.basename(names[-1])
    if m:
        return os.path.basename(cmd.split()[-1]) if cmd.split() else fallback
    return fallback


def fires_in_transcript(path):
    out = []
    for i, r in rows(path):
        t = r.get("type")
        ts = r.get("timestamp", "")
        if t == "system" and r.get("subtype") == "stop_hook_summary":
            errs = [e for e in (r.get("hookErrors") or []) if isinstance(e, str) and e.strip()]
            if not errs:
                continue
            infos = [x.get("command", "") for x in (r.get("hookInfos") or []) if isinstance(x, dict)]
            for e in errs:
                name = hook_name(e)
                if name == "unknown" and len(infos) == 1:
                    name = hook_name(infos[0])
                out.append({"hook": name, "event": "Stop", "ts": ts, "file": path, "line": i})
        elif t == "user":
            for b in blocks(r):
                if b.get("type") != "tool_result" or not b.get("is_error"):
                    continue
                txt = block_text(b)
                m = TOOL_HOOK_ERR.match(txt)
                if m:
                    rest = txt[m.end():].lstrip(": ")
                    out.append({"hook": hook_name(rest), "event": m.group(1), "ts": ts, "file": path, "line": i})
        elif t == "attachment":
            a = r.get("attachment") if isinstance(r.get("attachment"), dict) else {}
            if a.get("type") == "hook_blocking_error":
                ev = str(a.get("hookEvent") or a.get("hookName") or "unknown").split(":")[0]
                out.append({"hook": hook_name(a.get("command", "")), "event": ev, "ts": ts, "file": path, "line": i})
    return out


def from_transcripts(root, exclude):
    seen = set()
    fires = []
    for p in all_transcripts(root, exclude):
        for f in fires_in_transcript(p):
            k = (f["ts"], f["hook"], f["event"])
            if k in seen:
                continue
            seen.add(k)
            fires.append(f)
    return fires


def norm_ts(s):
    """Fire-log timestamps may be ISO or 'YYYY-MM-DD HH:MM:SS' local time."""
    e = parse_ts(s)
    if e is None:
        try:
            e = datetime.strptime(str(s), "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            return str(s)
    return datetime.fromtimestamp(e).isoformat(timespec="seconds")


def from_firelog(path, judged):
    fires = []
    with open(path, errors="replace") as fh:
        for i, line in enumerate(fh):
            try:
                f = json.loads(line)
            except ValueError:
                continue
            gate = f.get("gate") or f.get("hook") or "unknown"
            if (str(f.get("ts")), gate) in judged:
                continue
            fires.append({"hook": gate, "event": f.get("mode") or "block", "ts": norm_ts(f.get("ts")),
                          "file": f.get("transcript_path") or "", "line": i})
    return fires


def load_judged(path):
    if not path:
        return set()
    out = set()
    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                v = json.loads(line)
            except ValueError:
                continue
            out.add((str(v.get("ts")), v.get("gate") or v.get("hook")))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("root", nargs="?", default=DEFAULT_ROOT, help="transcript folder (default ~/.claude/projects)")
    ap.add_argument("--exclude", help="regex; skip transcript paths that match (e.g. benchmark folders)")
    ap.add_argument("--firelog", help="read fires from this JSONL log instead of transcripts")
    ap.add_argument("--judged", help="JSONL of fires already judged (keys ts, gate); these are skipped")
    ap.add_argument("--per-hook", type=int, default=0, help="sample up to N fires from each stratum")
    ap.add_argument("--alloc", action="append", default=[], metavar="HOOK|EVENT=N",
                    help="per-stratum sample size; overrides --per-hook for that stratum (repeatable)")
    ap.add_argument("--seed", type=int, default=20261008)
    ap.add_argument("--sample-out", help="write the sample locators to this CSV")
    a = ap.parse_args(argv)

    if a.firelog:
        fires = from_firelog(a.firelog, load_judged(a.judged))
        source = "fire log"
    else:
        fires = from_transcripts(a.root, a.exclude)
        source = "transcripts"

    strata = collections.defaultdict(list)
    for f in fires:
        strata[(f["hook"], f["event"])].append(f)

    print(f"source: {source}")
    print(f"fires: {len(fires)} in {len(strata)} strata (hook, event or mode)")
    print("hook\tevent\tfires")
    for (h, e), fs in sorted(strata.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        print(f"{h}\t{e}\t{len(fs)}")

    alloc = {}
    for item in a.alloc:
        k, _, n = item.rpartition("=")
        alloc[k] = int(n)
    if a.sample_out and (a.per_hook > 0 or alloc):
        rng = random.Random(a.seed)
        out = []
        for key in sorted(strata):
            pool = strata[key]
            n = alloc.get(f"{key[0]}|{key[1]}", a.per_hook)
            for f in rng.sample(pool, min(n, len(pool))):
                out.append((key, len(pool), f))
        with open(a.sample_out, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["sample_id", "hook", "stratum", "population", "file", "line", "timestamp",
                        "label", "behavior_after", "note"])
            for n, (key, pop, f) in enumerate(out, 1):
                w.writerow([f"S{n:03d}", key[0], f"{key[0]}|{key[1]}", pop, f["file"], f["line"], f["ts"], "", "", ""])
        print(f"sample: {len(out)} rows written (seed {a.seed}); fill in label = TP, FP or UNCLEAR")
    return 0


if __name__ == "__main__":
    sys.exit(main())
