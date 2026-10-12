#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""callbluff audit: run the execution_check logic over a whole repository.

Scans every .py file and reports syntax errors and names that are used but never
defined. Read-only, standard library only. The check itself lives in
hooks/guard_execution_check.py.
Usage: audit.py [directory] [--max=50]
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "hooks"))
from guard_execution_check import check_python  # noqa: E402

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "site-packages",
             ".tox", "dist", "build", "out", ".mypy_cache", ".ruff_cache"}


def walk_py(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def main():
    root = "."
    cap = 50
    for a in sys.argv[1:]:
        if a.startswith("--max="):
            cap = int(a.split("=")[1])
        elif not a.startswith("-"):
            root = a
    n_files, findings = 0, []
    for p in walk_py(root):
        n_files += 1
        findings.extend(check_python(p, os.path.relpath(p, root)))
        if len(findings) >= cap:
            break
    suffix = " (display limit reached; raise it with --max=)" if len(findings) >= cap else ""
    print(f"callbluff audit: scanned {n_files} .py files, found {len(findings)} possible problems{suffix}\n")
    for f in findings:
        print("  -", f)
    if not findings:
        print("  Clean: no syntax errors and no undefined names.")
    else:
        print("\nNote: the undefined-name check is static and conservative. Names injected at runtime "
              "(globals(), exec) are reported by mistake. Confirm each one before you change code.")


if __name__ == "__main__":
    main()
