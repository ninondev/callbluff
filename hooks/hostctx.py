# SPDX-License-Identifier: GPL-3.0-only
"""Host context: which model runs the session, which gate mode applies, cooldowns.

The session model is read from the transcript, because hooks get no model id in
their payload. Rows whose model is "<synthetic>" are skipped.

Mode policy (a default, not a measured result):
- Models in STRONG_FAMILIES run the pattern-matching gates in "shadow" mode:
  the gate judges and logs, but does not block. The test-file gate runs in
  "precision" mode and asks only when a test is weakened right after a red run.
- Other or unrecognised models get every gate in "block" mode.
The per-gate choice lives in gates.json (strong_mode).

Reading the model: a very large tool_result row can fill a fixed tail window, so
the tail read doubles from 128 KB up to 8 MB until it finds an assistant row.
A hook running inside a subagent receives the parent transcript path; the
subagent's own transcript sits at <parent without .jsonl>/subagents/agent-<id>.jsonl
and is read first.
"""
import json
import os
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from model_key import model_key  # noqa: E402

STRONG_FAMILIES = {"fable", "opus", "sonnet"}
COOLDOWN_HOURS = 2.0

MODEL_SCAN_MAX = 8 * 1024 * 1024
COOLDOWN_TAIL = 8 * 1024 * 1024


def tail_lines(path, nbytes=131072):
    """Read the tail of a file; long transcripts can be tens of MB."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > nbytes:
                f.seek(size - nbytes)
                f.readline()  # drop the possibly cut first line
            return f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def detect_model(lines):
    for line in reversed(lines):
        if '"model"' not in line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("type") == "assistant":
            m = (row.get("message") or {}).get("model")
            if m and not str(m).startswith("<"):
                return str(m)
    return ""


def detect_model_file(path, max_bytes=MODEL_SCAN_MAX):
    if not path:
        return ""
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    win = 131072
    while True:
        m = detect_model(tail_lines(path, win))
        if m or win >= size or win >= max_bytes:
            return m
        win = min(win * 4, max_bytes)


def subagent_transcript(tpath, agent_id):
    if not tpath or not agent_id:
        return ""
    aid = str(agent_id)
    if aid.startswith("agent-"):
        aid = aid[len("agent-"):]
    base = tpath[:-6] if tpath.endswith(".jsonl") else tpath
    cand = os.path.join(base, "subagents", f"agent-{aid}.jsonl")
    return cand if os.path.exists(cand) else ""


def host_key(tpath, agent_id=None):
    if not tpath:
        return ""
    sub = subagent_transcript(tpath, agent_id)
    if sub:
        m = detect_model_file(sub)
        if m:
            return model_key(m)
    return model_key(detect_model_file(tpath))


def is_strong(tpath, agent_id=None):
    return host_key(tpath, agent_id) in STRONG_FAMILIES


def mode_for(gate, tpath, agent_id=None):
    """Mode of a gate on the current host: block, shadow or precision."""
    if not is_strong(tpath, agent_id):
        return "block"
    try:
        with open(os.path.join(KIT, "gates.json"), encoding="utf-8") as f:
            reg = json.load(f)
        return str((reg.get(gate) or {}).get("strong_mode") or "block")
    except Exception:
        return "block"


def _row_epoch(line):
    try:
        ts = json.loads(line).get("timestamp")
    except (json.JSONDecodeError, AttributeError):
        return None
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def recently_fired(lines, signature, hours=COOLDOWN_HOURS):
    """True if the last line holding `signature` is less than `hours` old.

    If that line has no timestamp, the next timestamped line within 5 lines is used.
    With no timestamps at all this falls back to "once per session" (returns True).
    """
    idx = -1
    for i, line in enumerate(lines):
        if signature in line:
            idx = i
    if idx < 0:
        return False
    for j in range(idx, min(idx + 6, len(lines))):
        ep = _row_epoch(lines[j])
        if ep is not None:
            return (time.time() - ep) < hours * 3600
    return True
