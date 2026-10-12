---
description: callbluff report - which gates fired, and what they said
---

Run `python3 "${CLAUDE_PLUGIN_ROOT}/tools/report.py"` (add `--days=N` if the user names a number of days, or `--all` for the whole history). Show the output as it is and do not rewrite the numbers. If it reports zero fires, say so plainly: the gates are on and nothing tripped.

The fire log is a local file, `~/.callbluff/firelog.jsonl`. Each line has a path to the transcript so a fire can be judged later. A fire is not proof of a mistake; many fires are false alarms.
