#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""delivery_inventory (Stop hook): "everything is done" with no list of what was done.

Fires when all of these hold:
  1. The session wrote at least 4 distinct files.
  2. The final reply makes a real completion statement about the whole job: "all 12
     modules are updated", "everything is done", "I have updated every call", or a
     bare "Done." Words that only mention completion do not count: questions,
     conditions ("when all of it is done"), negations ("not all done"), plans
     ("will be done"), progress ("the first step is done, the rest is running"),
     quoted text, code, and inline code.
  3. The reply has no hedge or concession ("remaining", "skipped", "failed", ...), and
     it does not list every written file with a pass note on the same line.
Result: block once and ask for a three-way inventory (done and checked, degraded or
substituted, not done). Loop guard: stop_hook_active.

If the Stop payload carries last_assistant_message, that text is used instead of the
transcript, so a transcript that is not flushed yet cannot supply an old reply.
"""
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from firelog import log_fire  # noqa: E402
from _shared import WRITE_TOOLS, QUOTED, INLINE_CODE  # noqa: E402

GATE = "delivery_inventory"

DONE_WORD = r"(?:done|complete|completed|finished|updated|migrated|replaced|converted|renamed|ported|refactored|implemented|fixed|handled|wrapped up)"
ADV = r"(?:(?:now|all|fully|successfully|already|just)\s+){0,3}"
BE = r"(?:are|is|were|was|have been|has been|got|now)"
# Collective statements: "all 12 modules are updated", "12 modules have all been migrated".
ALL_DONE_EN = (
    r"\b(?:all|every|each|both|\d+)\b[^,.!?;\n]{0,40}?\b" + BE + r"\s+" + ADV + DONE_WORD + r"\b"
    r"|\b(?:i|we)(?:'ve| have)?\s+(?:now\s+)?" + DONE_WORD + r"\s+(?:all|every|each)\b"
    r"|\b(?:everything|all (?:tasks|items|work|steps))\s+(?:is|are)?\s*" + ADV + DONE_WORD + r"\b"
    r"|(?:^|[.!?;\n])\s*(?:all\s+)?(?:done|complete|finished)\s*(?=$|[!.])")
# Chinese counterparts (generic completion wording).
ZH_DONE = r"(?:完成|做完|处理完|改成|改完|替换|换成|迁移|更新|搞定|就绪)"
ZH_ACTION = r"\s*(?:(?:全部|悉数|按(?:照)?(?:要求|方案|计划)|逐项|逐一|顺利|成功|如期)\s*){0,2}" + ZH_DONE
ZH_STATUS = r"(?:(?:均|都)(?:已经|已)?|已经|已)"
ALL_DONE_ZH = (
    r"(?:全部|所有|悉数|各个|每个|\d+\s*(?:项|个))[^，,。！？!?；;\n]{0,40}?" + ZH_STATUS + ZH_ACTION + r"|"
    r"(?:已经|已)?(?:全部|全都|都|均|悉数)(?:已经|已)?" + ZH_ACTION + r"|"
    r"一次做完|(?:^|[。！？!?；;\n])\s*完成(?=$|[!！。])")
CONDITIONAL_ZH = r"如果|假如|假设|一旦|只要|除非|直到|等到|等待|待到|^\s*(?:若(?!干)|等(?!级|价|同|式)|待(?!办|遇))"
NON_ASSERTED_ZH = r"尚未|暂未|还未|并未|没有|不能|不可|并非|不是|未曾|不应|别|勿|准备|计划|打算|将会|将要|预计|是否|能否"
DEFERRED_ZH = r"^\s*(?:了)?(?:之后|以后|后|时|之前|前|才|再|就)"
HEDGE_ZH = (r"未完成|未能|没有完成|没(?:改|做|动|换)|待办(?!" + ZH_STATUS + ZH_ACTION + r")|待(?:处理|完成|确认)|"
            r"剩(?:余|下)|降级|替代(?:实现|方案)|除了|暂未|跳过|失败|做不了|权限不足|无法|部分完成|例外|豁免|保留原样|特殊处理")
ALL_DONE = re.compile(ALL_DONE_EN + "|" + ALL_DONE_ZH, re.I)

CONDITIONAL = re.compile(
    r"\b(?:if|when|once|unless|until|whenever|assuming|provided|as soon as|in case)\b"
    r"|^\s*(?:and |but )?(?:after|before)\b|" + CONDITIONAL_ZH, re.I)
NON_ASSERTED = re.compile(
    r"\b(?:not|never|no|yet|still|hardly|barely|cannot|can't|won't|will|would|should|could|may|might|"
    r"plan(?:s|ned|ning)?|goal|going to|about to|need(?:s)? to|to (?:get|have|make|be)|waiting|whether|"
    r"let me|let's)\b|n't\b|" + NON_ASSERTED_ZH, re.I)
DEFERRED = re.compile(r"^\s*(?:after|once|when|before|until|only after|pending|later|soon)\b|" + DEFERRED_ZH, re.I)
INVENTORY_LINE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)、]\s*|\|)")
VERIFIED = re.compile(r"\b(?:tests?|checks?|verification|build|lint)\s+(?:pass(?:ed|es)?|is green|are green|ok)\b|"
                      r"\b(?:PASS|passed|green)\b|(?<![不未没])(?:测试|自测|验证|检查)通过", re.I)
# A concession inside the reply: the claim is not "everything, as planned".
HEDGE = re.compile(
    r"\b(?:not (?:done|complete|completed|finished)|incomplete|unfinished|remain(?:s|ed|ing)?|"
    r"to-?do|left to|\d+\s+(?:more|to go)|\d+\s+(?:of|out of)\s+\d+|except|exceptions?|skipp(?:ed|ing)|"
    r"fail(?:ed|s|ing|ure)?|unable|could not|couldn't|blocked|partial(?:ly)?|degraded|fallback|workaround|"
    r"substitute[sd]?|replacement implementation|waived|left as is|special case|caveats?)\b|" + HEDGE_ZH, re.I)


def has_completion_claim(text):
    """True if the text contains a real, standalone completion statement."""
    for sentence in re.split(r"(?<=[.!?;。！？；\n])", text):
        sentence = sentence.strip()
        if not sentence or sentence.endswith(("?", "？")):
            continue
        conditional = False
        for clause in re.split(r"[,，]", sentence):
            conditional = conditional or bool(CONDITIONAL.search(clause))
            if conditional:
                continue
            for match in ALL_DONE.finditer(clause):
                if NON_ASSERTED.search(clause[:match.start()]):
                    continue
                if DEFERRED.search(clause[match.end():]):
                    continue
                return True
    return False


def has_file_inventory(text, files):
    """True if every written file appears on its own list line together with a pass note.

    This checks the shape of the list only. It does not prove the tests ran. Files that
    share a base name need their full path, so one `main.py` cannot stand for two files.
    """
    basenames = Counter(os.path.basename(path) for path in files)
    patterns = {}
    for path in files:
        names = [path]
        basename = os.path.basename(path)
        if basenames[basename] == 1:
            names.append(basename)
        patterns[path] = re.compile(r"(?<![\w.-])(?:" + "|".join(map(re.escape, names)) + r")(?![\w./-])")
    covered = set()
    for line in text.splitlines():
        if not INVENTORY_LINE.search(line) or not VERIFIED.search(line) or re.search(r"[?？]", line):
            continue
        matches = {path for path, pattern in patterns.items() if pattern.search(line)}
        if len(matches) == 1:
            covered.update(matches)
    return covered == files


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if payload.get("stop_hook_active"):
        return 0
    tpath = payload.get("transcript_path")
    if not tpath:
        return 0
    files = set()
    last_text = ""
    try:
        with open(tpath, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = row.get("message") or {}
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                texts = []
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("type") == "tool_use" and blk.get("name") in WRITE_TOOLS:
                        p = (blk.get("input") or {}).get("file_path") or (blk.get("input") or {}).get("notebook_path")
                        if p:
                            files.add(p)
                    elif blk.get("type") == "text":
                        texts.append(blk.get("text", ""))
                if (row.get("type") == "assistant" or msg.get("role") == "assistant") and texts:
                    last_text = "\n".join(texts)
    except OSError:
        return 0
    if isinstance(payload.get("last_assistant_message"), str):
        last_text = payload["last_assistant_message"]
    if len(files) < 4 or not last_text:
        return 0
    report_text = QUOTED.sub(" ", last_text)
    claim_text = INLINE_CODE.sub(" ", report_text)
    if (has_completion_claim(claim_text) and not HEDGE.search(claim_text)
            and not has_file_inventory(report_text, files)):
        log_fire(GATE, f"files={len(files)}", payload.get("cwd", ""), tpath or "")
        sys.stderr.write(
            f"[callbluff:{GATE}] The reply says the whole job is done, but there is no item-by-item "
            "inventory. Sort it into three groups before you hand over: (1) done as planned and checked; "
            "(2) degraded or substituted, with the difference; (3) not done, with the reason and what is "
            "needed. If everything really is done as planned, list each file with how you checked it.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
