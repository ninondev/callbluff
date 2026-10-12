#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""consumer_sweep (Stop hook): a widely used definition changed, its consumers did not.

Fires when all of these hold:
  1. The session changed a .py file that at least 3 other files import (a "hub").
  2. The change added a new string value (an enum value, a key, a status name) and at
     least one of these is stale:
       - a .md/.txt/.rst file that talks about the same values but lacks the new one;
       - a list constant that was changed and is read by position elsewhere
         (NAME[0], NAME.index(...)) in a file that was not changed;
       - a dict or list of strings elsewhere that holds the old values but not the new one.
Result: block once and ask for a sweep of every consumer (imports, docs, positional
references, data files). A file that was changed in the session counts as checked.
Loop guard: stop_hook_active, plus a 2 hour cooldown.
"""
import ast
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from firelog import log_fire  # noqa: E402
import hostctx  # noqa: E402
import _shared  # noqa: E402

GATE = "consumer_sweep"
SIGNATURE = f"callbluff:{GATE}"
SKIP_DIRS = ("node_modules", "__pycache__", "venv")


def _walk(cwd, skip=SKIP_DIRS):
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in skip]
        for fn in files:
            yield root, fn


def _changed_list_constants(cwd, py_changed):
    """Names of upper-case list/tuple constants whose lines were touched by the diff.

    Uses the added line numbers of the diff hunks against the line span of each
    list assignment, so a one-line addition inside a multi-line list is found.
    """
    names = set()
    for c in py_changed:
        try:
            diff = subprocess.run(["git", "diff", "-U0", "HEAD", "--", c], cwd=cwd,
                                  capture_output=True, text=True, timeout=30).stdout
        except Exception:
            continue
        added = set()
        for m in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", diff, re.M):
            start, cnt = int(m.group(1)), int(m.group(2) or "1")
            added.update(range(start, start + max(cnt, 1)))
        if not added:
            continue
        try:
            with open(os.path.join(cwd, c), encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, (ast.List, ast.Tuple)):
                continue
            span = set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
            if not (span & added):
                continue
            for t in node.targets:
                if isinstance(t, ast.Name) and re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", t.id):
                    names.add(t.id)
    return names


def _subscript_consumers(cwd, const_names):
    """Find code that reads the constants by position: NAME[0] or NAME.index(...)."""
    hits = []
    for root, fn in _walk(cwd):
        if not fn.endswith(".py"):
            continue
        fp = os.path.join(root, fn)
        try:
            with open(fp, encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except Exception:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                    and node.value.id in const_names):
                hits.append((os.path.relpath(fp, cwd), node.lineno, node.value.id + "[...]"))
            elif (isinstance(node, ast.Attribute) and node.attr == "index"
                    and isinstance(node.value, ast.Name) and node.value.id in const_names):
                hits.append((os.path.relpath(fp, cwd), node.lineno, node.value.id + ".index()"))
    return hits


def changed_files(cwd):
    """Changed files without caches and without private (underscore) files."""
    return sorted(n for n in _shared.changed_files(cwd)
                  if "__pycache__" not in n and not os.path.basename(n).startswith("_"))


def importers_of(cwd, module_name):
    n = 0
    for root, fn in _walk(cwd):
        if not fn.endswith(".py") or fn == module_name + ".py":
            continue
        try:
            with open(os.path.join(root, fn), encoding="utf-8", errors="replace") as f:
                t = f.read()
        except OSError:
            continue
        if re.search(rf"\b(from|import)\s+{re.escape(module_name)}\b", t):
            n += 1
    return n


def _parallel_literal_hits(cwd, old_lits, stale_lits, changed):
    """Dict key sets and string lists that share at least 2 old values and lack every new one."""
    hits = []
    for root, fn in _walk(cwd):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        rel = os.path.relpath(os.path.join(root, fn), cwd)
        if rel in changed:
            continue
        try:
            with open(os.path.join(root, fn), encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except Exception:
            continue
        for node in ast.walk(tree):
            keys = None
            if isinstance(node, ast.Dict):
                keys = {k.value for k in node.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            elif isinstance(node, (ast.List, ast.Tuple)):
                vals = {e.value for e in node.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)}
                keys = vals if len(vals) >= 2 else None
            if not keys:
                continue
            if len(keys & old_lits) >= 2 and not (keys & stale_lits):
                kind = "dict keys" if isinstance(node, ast.Dict) else "enum list"
                hits.append((rel, node.lineno, kind))
                break  # one report per file is enough
    return hits


def sweep_hits(cwd, exclude_docs=()):
    """Return (hubs, doc_hits, implicit_hits, parallel_hits).

    - doc_hits: .md/.txt/.rst files that share at least 2 unchanged values with the diff
      but miss the newly added values. A document changed in the session counts as checked;
      documents that were not changed are still examined.
    - implicit_hits: positional reads of a changed list constant, outside changed files.
      Independent of the doc check: editing a README does not prove the indexes were checked.
    - parallel_hits: parallel copies (dict keys, string lists) of the old values.
    """
    changed = changed_files(cwd)
    py_changed = [c for c in changed if c.endswith(".py")]
    if not py_changed:
        return [], [], [], []
    hubs = []
    for c in py_changed:
        mod = os.path.splitext(os.path.basename(c))[0]
        if importers_of(cwd, mod) >= 3:
            hubs.append(mod)
    if not hubs:
        return [], [], [], []
    hub_files = [x for x in py_changed if os.path.splitext(os.path.basename(x))[0] in hubs]
    new_lits, old_lits = set(), set()
    for c in hub_files:
        try:
            diff = subprocess.run(["git", "diff", "HEAD", "--", c], cwd=cwd,
                                  capture_output=True, text=True, timeout=30).stdout
        except Exception:
            continue
        for line in diff.splitlines():
            lits = re.findall(r"[\"']([A-Za-z_][A-Za-z0-9_-]{2,30})[\"']", line)
            if line.startswith("+") and not line.startswith("+++"):
                new_lits.update(lits)
            elif line.startswith("-") and not line.startswith("---"):
                old_lits.update(lits)
    stale_lits = new_lits - old_lits  # values that are really new
    doc_hits = []
    if stale_lits:
        for root, fn in _walk(cwd, skip=("node_modules", "__pycache__")):
            if fn.endswith((".md", ".txt", ".rst")) and fn not in exclude_docs and not fn.startswith("_"):
                fp = os.path.join(root, fn)
                rel = os.path.relpath(fp, cwd)
                if rel in changed:
                    continue
                try:
                    with open(fp, encoding="utf-8", errors="replace") as f:
                        t = f.read()
                except OSError:
                    continue
                context_hit = sum(1 for lit in (old_lits | new_lits) - stale_lits if lit in t)
                missing_new = [lit for lit in stale_lits if lit not in t]
                if context_hit >= 2 and missing_new:
                    doc_hits.append((rel, missing_new[:3]))
    implicit_hits = []
    consts = _changed_list_constants(cwd, hub_files)
    if consts:
        implicit_hits = [h for h in _subscript_consumers(cwd, consts) if h[0] not in changed]
    parallel_hits = []
    if stale_lits:
        parallel_hits = _parallel_literal_hits(cwd, old_lits, stale_lits, set(changed))
    return hubs, doc_hits, implicit_hits, parallel_hits


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if payload.get("stop_hook_active"):
        return 0
    cwd = payload.get("cwd") or os.getcwd()
    tpath = payload.get("transcript_path")
    if tpath:
        try:
            if hostctx.recently_fired(hostctx.tail_lines(tpath, hostctx.COOLDOWN_TAIL), SIGNATURE):
                return 0
        except OSError:
            pass
    hubs, doc_hits, implicit_hits, parallel_hits = sweep_hits(cwd)
    if not doc_hits and not implicit_hits and not parallel_hits:
        return 0
    log_fire(GATE, f"hubs={hubs} docs={[d for d, _ in doc_hits[:2]]} "
             f"imp={len(implicit_hits)} par={len(parallel_hits)}", cwd, tpath or "")
    parts = []
    if doc_hits:
        segs = []
        for d, m in doc_hits[:2]:
            tag = " (templates and config are deliverables too and must be updated)" if not d.endswith(".md") else ""
            segs.append(f"{d} covers the same topic but lacks {'/'.join(m)}{tag}")
        parts.append("; ".join(segs) + ".")
    if parallel_hits:
        parts.append("Parallel copies not updated: " + "; ".join(
            f"{f}:{ln} ({kind})" for f, ln, kind in parallel_hits[:3])
            + " (they hold the same old values but not the new one; check templates, maps and hard-coded lists).")
    if implicit_hits:
        parts.append("Positional references not checked: " + "; ".join(
            f"{f}:{ln} {expr}" for f, ln, expr in implicit_hits[:3])
            + " (after a list changes, position-based meaning can shift quietly; check each one).")
    sys.stderr.write(
        f"[{SIGNATURE}] You changed a widely used definition file ({', '.join(hubs)}). "
        + " ".join(parts)
        + " Sweep every consumer (imports, docs, positional references, data files), then update them or say why not.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
