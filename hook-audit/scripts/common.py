# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ninon
"""Shared helpers: Wilson intervals and streaming Claude Code transcripts."""
import glob
import json
import math
import os
import re
from datetime import datetime

DEFAULT_ROOT = os.path.join(os.path.expanduser("~"), ".claude", "projects")


def wilson(k, n, z=1.96):
    """Wilson score interval for k successes out of n. Returns (low, high)."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - h) / d), min(1.0, (c + h) / d))


def main_transcripts(root, exclude=None):
    """Main-session transcripts: <root>/<project>/<session>.jsonl.
    Subagent files live one level deeper and are not returned."""
    rx = re.compile(exclude, re.I) if exclude else None
    files = [p for p in glob.glob(os.path.join(root, "*", "*.jsonl")) if not (rx and rx.search(p))]
    return sorted(files)


def all_transcripts(root, exclude=None):
    """Main-session and subagent transcripts."""
    rx = re.compile(exclude, re.I) if exclude else None
    files = glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    return sorted(p for p in files if not (rx and rx.search(p)))


def rows(path):
    """Yield (line_number, row) for every parseable JSON line. Bad lines are skipped."""
    try:
        fh = open(path, errors="replace")
    except OSError:
        return
    with fh:
        for i, line in enumerate(fh):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                yield i, r


def parse_ts(s):
    """ISO-8601 timestamp (with Z or offset) to epoch seconds, or None."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def blocks(row):
    c = (row.get("message") or {}).get("content")
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def block_text(b):
    c = b.get("content")
    if isinstance(c, list):
        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
    return c if isinstance(c, str) else ""


def pct(k, n):
    return 100.0 * k / n if n else 0.0
