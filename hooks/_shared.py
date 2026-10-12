# SPDX-License-Identifier: MIT
"""Constants and helpers shared by all gates."""
import json
import os
import re
import subprocess

WRITE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
VERIFY = re.compile(r"\b(python3?|pytest|unittest|node|go test|cargo (test|check)|npm test|make test)\b")

# Text that quotes someone else or shows code does not count as the agent's own claim.
QUOTED = re.compile(
    r"```[\s\S]*?```|~~~[\s\S]*?~~~|(?m:^\s*>[^\n]*)|"
    r"「[^」]*」|『[^』]*』|“[^”]*”|‘[^’]*’|\"[^\"\n]*\"|(?<![\w])'[^'\n]*'(?![\w])")
INLINE_CODE = re.compile(r"`[^`\n]*`")


def canon(path):
    """Signature that ignores digits in the file name: mod_5.py and mod_11.py match."""
    return os.path.dirname(path) + "/" + re.sub(r"\d+", "#", os.path.basename(path))


def strip_quoted(text):
    """Remove quotes, code fences and inline code before looking for claims."""
    return INLINE_CODE.sub(" ", QUOTED.sub(" ", text or ""))


def transcript_rows(tpath):
    """Yield parsed JSON rows of a transcript; unreadable lines are skipped.
    A line that parses to a non-object (null, number, string, array) is not a
    row: transcripts are append logs and a torn write can leave such a line."""
    try:
        with open(tpath, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


def session_written_files(tpath, cwd):
    """Absolute paths of files this session wrote with Edit, Write, MultiEdit or NotebookEdit.

    Files changed by other sessions or by hand never appear here, which is the point:
    a gate may only blame the session for what the session itself did.
    """
    out = []
    for row in transcript_rows(tpath):
        content = (row.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for blk in content:
            if not isinstance(blk, dict) or blk.get("type") != "tool_use":
                continue
            if blk.get("name") not in WRITE_TOOLS:
                continue
            inp = blk.get("input") or {}
            p = inp.get("file_path") or inp.get("notebook_path")
            if not p:
                continue
            p = p if os.path.isabs(p) else os.path.join(cwd or ".", p)
            p = os.path.normpath(p)
            if p not in out:
                out.append(p)
    return out


def changed_files(cwd):
    """Files changed under cwd (relative paths): tracked changes plus untracked files.

    `git diff --name-only` prints repository-root paths and `git ls-files --others` prints
    cwd paths. `--relative` makes both agree when cwd is a subdirectory.
    """
    try:
        r = subprocess.run(["git", "diff", "--relative", "--name-only", "HEAD"], cwd=cwd,
                           capture_output=True, text=True, timeout=30)
        names = set(r.stdout.splitlines()) if r.returncode == 0 else set()
        extra = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=cwd,
                               capture_output=True, text=True, timeout=30)
        if extra.returncode == 0:
            names |= set(extra.stdout.splitlines())  # a repo without HEAD still shows untracked files
        return sorted(n for n in names if n)
    except Exception:
        return []
