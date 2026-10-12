#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""execution_check (Stop hook): did this session leave broken code behind?

For every file the session itself wrote (Edit, Write, MultiEdit, NotebookEdit):
  1. Python files: syntax check, then a small AST pass that finds names that are
     used but never bound anywhere in the module (an import was changed but a
     call site was not).
  2. JS, shell, Ruby, PHP and JSON files: syntax check with the tool on PATH,
     skipped when the tool is missing.
A hit exits with code 2 and names the file and the name.

Only files written by this session are checked. A file edited by another session,
or by hand, is not this session's problem.

Python 3.12+ syntax is understood: PEP 695 type parameters (`class C[T]`,
`def f[T](x: T)`, `type X = ...`) bind their names, and `match` patterns bind
their capture names. When the interpreter running the hook is older than 3.12 and
the file uses PEP 695 syntax, the file is skipped instead of reported.

Loop guard: stop_hook_active lets the stop through. Any failure of the gate itself
(no transcript, unreadable file) lets the stop through too.
"""
import ast
import builtins
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from firelog import log_fire  # noqa: E402
import _shared  # noqa: E402

GATE = "execution_check"
BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__", "__loader__", "__package__"}

# Signs of syntax that only Python 3.12+ can parse (PEP 695).
PEP695 = re.compile(r"^\s*(?:class|def|async\s+def)\s+\w+\s*\[|^\s*type\s+\w+(?:\[[^\]]*\])?\s*=", re.M)


def uses_new_syntax(source):
    """True when the source text uses PEP 695 generics or the `type` statement."""
    if isinstance(source, bytes):
        source = source.decode("utf-8", "replace")
    return bool(PEP695.search(source))


def _bind_pattern_names(node, bound):
    """Names bound by `match` patterns (Python 3.10+; the classes do not exist before)."""
    as_cls, star_cls, map_cls = (getattr(ast, n, None) for n in ("MatchAs", "MatchStar", "MatchMapping"))
    if as_cls and isinstance(node, as_cls) and node.name:
        bound.add(node.name)
    elif star_cls and isinstance(node, star_cls) and node.name:
        bound.add(node.name)
    elif map_cls and isinstance(node, map_cls) and node.rest:
        bound.add(node.rest)


def undefined_names(path):
    """Return [(name, lineno)] for names that are loaded but bound nowhere in the module.

    Conservative on purpose: a name bound anywhere in the file, in any scope, is
    treated as defined, so scoping rules cannot cause a false alarm.
    """
    try:
        with open(path, "rb") as f:
            tree = ast.parse(f.read())  # bytes: ast honours a PEP 263 encoding line
    except Exception:
        return []  # syntax errors are reported by check_python; anything else must not crash the gate
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                bound.add(a.asname or a.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Global) or isinstance(node, ast.Nonlocal):
            bound.update(node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        # PEP 695 type parameters (Python 3.12+); PEP 696 defaults ride on the same nodes.
        for tp in ("TypeVar", "ParamSpec", "TypeVarTuple"):
            cls = getattr(ast, tp, None)
            if cls is not None and isinstance(node, cls):
                bound.add(node.name)
        _bind_pattern_names(node, bound)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            ar = node.args
            for a in (ar.args + ar.posonlyargs + ar.kwonlyargs):
                bound.add(a.arg)
            if ar.vararg:
                bound.add(ar.vararg.arg)
            if ar.kwarg:
                bound.add(ar.kwarg.arg)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in bound and node.id not in BUILTINS:
                bad.append((node.id, node.lineno))
    return bad


def check_python(path, rel, version=None):
    """Return a list of problem strings for one Python file.

    `version` is the interpreter version tuple; it is a parameter so tests can
    pretend to be an older Python.
    """
    version = version or sys.version_info[:2]
    try:
        with open(path, "rb") as f:
            src = f.read()
        compile(src, path, "exec", dont_inherit=True)  # no .pyc is written, unlike py_compile
    except SyntaxError as e:
        if tuple(version) < (3, 12) and uses_new_syntax(src):
            return []  # this Python cannot judge 3.12+ syntax, so it must not call it an error
        return [f"{rel}: syntax error ({str(e.msg)[:80]}, line {e.lineno})"]
    except Exception:
        return []
    return [f"{rel}:{ln} uses undefined name `{name}` (an import or definition changed but a call site did not?)"
            for name, ln in undefined_names(path)[:3]]


def _has(tool):
    from shutil import which
    return which(tool) is not None


def check_other_lang(path, rel):
    """Syntax check for non-Python files with the tool on PATH. Returns a problem string or None."""
    ext = os.path.splitext(path)[1]
    cmd = None
    if ext in (".js", ".mjs", ".cjs") and _has("node"):
        cmd = ["node", "--check", path]
    elif ext == ".sh" and _has("bash"):
        cmd = ["bash", "-n", path]
    elif ext == ".rb" and _has("ruby"):
        cmd = ["ruby", "-c", path]
    elif ext == ".php" and _has("php"):
        cmd = ["php", "-l", path]
    elif ext == ".json":
        try:
            with open(path, encoding="utf-8") as f:
                json.load(f)
            return None
        except Exception as e:
            return f"{rel}: invalid JSON ({str(e)[:60]})"
    if not cmd:
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            msg = (r.stderr or r.stdout).strip().splitlines()
            return f"{rel}: syntax check failed ({msg[0][:80] if msg else 'non-zero exit'})"
    except Exception:
        return None
    return None


def session_files(payload, cwd):
    """Files this session wrote that still exist, as (absolute, display) pairs."""
    tpath = payload.get("transcript_path")
    if not tpath:
        return []
    out = []
    for p in _shared.session_written_files(tpath, cwd):
        if os.path.isfile(p):
            rel = os.path.relpath(p, cwd) if p.startswith(os.path.abspath(cwd) + os.sep) else p
            out.append((p, rel))
    return out


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if payload.get("stop_hook_active"):
        return 0
    cwd = payload.get("cwd") or os.getcwd()
    problems = []
    for p, rel in session_files(payload, cwd):
        if rel.endswith(".py"):
            problems.extend(check_python(p, rel))
        else:
            prob = check_other_lang(p, rel)
            if prob:
                problems.append(prob)
    if problems:
        log_fire(GATE, "; ".join(problems[:2]), cwd, payload.get("transcript_path") or "")
        sys.stderr.write(
            f"[callbluff:{GATE}] Code written in this session looks broken. Fix it before you hand over:\n- "
            + "\n- ".join(problems[:6])
            + "\nFix each one and run it. If one is a false alarm (for example a name injected at runtime), "
              "say so by name in your report.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
