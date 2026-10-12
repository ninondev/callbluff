---
description: callbluff audit - scan the current repository for syntax errors and undefined names
---

Run `python3 "${CLAUDE_PLUGIN_ROOT}/tools/audit.py"` (pass a directory or `--max=N` if the user gives one) and show the output as it is. If it finds something, remind the user that the check is static and conservative: names injected at runtime can be reported by mistake, so confirm each one before changing code.
