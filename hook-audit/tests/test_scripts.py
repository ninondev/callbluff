# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ninon
"""Run every script on the synthetic fixture. Never touches a real ~/.claude folder."""
import csv
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SCRIPTS = os.path.join(REPO, "scripts")
FIX = os.path.join(HERE, "fixtures")
PROJECTS = os.path.join(FIX, "projects")
SENTINEL = "SENTINEL-PRIVATE-TEXT"

sys.dont_write_bytecode = True
sys.path.insert(0, SCRIPTS)
import common  # noqa: E402
import q2_done_claims  # noqa: E402


def run(script, *args):
    p = subprocess.run([sys.executable, "-I", "-B", os.path.join(SCRIPTS, script), *args],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise AssertionError(f"{script} exited {p.returncode}: {p.stderr}")
    return p.stdout


def table(out, header):
    """Rows of the tab-separated block that starts with the given header word."""
    lines = out.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(header + "\t"))
    res = []
    for ln in lines[start + 1:]:
        if "\t" not in ln:
            break
        res.append(ln.split("\t"))
    return res


class Wilson(unittest.TestCase):
    def test_published_intervals(self):
        lo, hi = common.wilson(5, 59)
        self.assertEqual((round(lo, 3), round(hi, 3)), (0.037, 0.184))
        lo, hi = common.wilson(1, 40)
        self.assertEqual((round(lo, 3), round(hi, 3)), (0.004, 0.129))

    def test_edges(self):
        self.assertEqual(common.wilson(0, 0), (0.0, 0.0))
        lo, hi = common.wilson(0, 10)
        self.assertEqual(lo, 0.0)
        self.assertGreater(hi, 0.0)


class ClaimPhrases(unittest.TestCase):
    def test_negation_filtered(self):
        self.assertEqual(q2_done_claims.claims("It is not done yet."), [])
        self.assertEqual(q2_done_claims.claims("This is not verified."), [])

    def test_hits(self):
        self.assertIn("done", q2_done_claims.claims("Done, all good."))
        self.assertTrue(q2_done_claims.claims("已完成"))

    def test_is_check(self):
        self.assertTrue(q2_done_claims.is_check("cd x && nice -n 10 python3 -m unittest"))
        self.assertTrue(q2_done_claims.is_check("cargo test --release"))
        self.assertFalse(q2_done_claims.is_check("cat test_report.txt"))
        self.assertFalse(q2_done_claims.is_check("git commit -m 'fix tests'"))


class Q2(unittest.TestCase):
    def setUp(self):
        self.out = run("q2_done_claims.py", PROJECTS)

    def test_counts(self):
        self.assertIn("main-session transcripts: 2", self.out)
        self.assertIn("claims: 14 raw, 7 unique, 6 about code", self.out)
        t = {r[0]: r[1:] for r in table(self.out, "class")}
        self.assertEqual(t["VERIFIED_PASS"][0], "2")
        for c in ("UNVERIFIED", "CLAIM_WITH_FAILING_CHECK", "FAILING_CHECK_DISCLOSED",
                  "CHECK_OUTCOME_UNKNOWN", "NOT_APPLICABLE"):
            self.assertEqual(t[c][0], "1", c)

    def test_no_text_in_output(self):
        self.assertNotIn(SENTINEL, self.out)
        self.assertNotIn("rename", self.out)

    def test_sample_has_locators_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.csv")
            out = run("q2_done_claims.py", PROJECTS, "--per-class", "5", "--sample-out", path)
            self.assertIn("sample: 2 rows written", out)
            body = open(path).read()
            self.assertNotIn(SENTINEL, body)
            rows = list(csv.DictReader(open(path)))
            self.assertEqual({r["detector_class"] for r in rows}, {"UNVERIFIED", "CLAIM_WITH_FAILING_CHECK"})


class Q1(unittest.TestCase):
    def test_transcript_fires(self):
        out = run("q1_hook_fires.py", PROJECTS)
        self.assertIn("fires: 3 in 2 strata", out)
        t = table(out, "hook")
        self.assertEqual(t, [["guard_tests.py", "PreToolUse", "2"], ["guard_done.py", "Stop", "1"]])
        self.assertNotIn(SENTINEL, out)

    def test_sample_is_seeded(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "a.csv"), os.path.join(d, "b.csv")
            run("q1_hook_fires.py", PROJECTS, "--per-hook", "1", "--sample-out", a)
            run("q1_hook_fires.py", PROJECTS, "--per-hook", "1", "--sample-out", b)
            self.assertEqual(open(a).read(), open(b).read())
            self.assertEqual(len(list(csv.DictReader(open(a)))), 2)

    def test_alloc(self):
        with tempfile.TemporaryDirectory() as d:
            a = os.path.join(d, "a.csv")
            run("q1_hook_fires.py", PROJECTS, "--alloc", "guard_tests.py|PreToolUse=2", "--sample-out", a)
            rows = list(csv.DictReader(open(a)))
            self.assertEqual([r["stratum"] for r in rows], ["guard_tests.py|PreToolUse"] * 2)

    def test_firelog_skips_judged(self):
        out = run("q1_hook_fires.py", "--firelog", os.path.join(FIX, "firelog.jsonl"),
                  "--judged", os.path.join(FIX, "judged.jsonl"))
        self.assertIn("fires: 4 in 3 strata", out)
        t = table(out, "hook")
        self.assertIn(["guard_tests", "shadow", "2"], t)
        self.assertNotIn("2026-09-04", out)

    def test_stats(self):
        out = run("q1_stats.py", os.path.join(FIX, "q1_labeled.csv"), "--prior", "0,3,1")
        self.assertIn("ALL\t8\t1\t6\t1\t30\t0.125\t0.022\t0.471", out)
        self.assertIn("population-weighted TP rate: 0.083", out)
        self.assertIn("all judged (sample + prior): n=12 TP=1", out)
        self.assertIn("behavior after blocking fires (n=4)", out)


class Adjust(unittest.TestCase):
    def test_reproduces_report(self):
        out = run("q2_adjust.py", "--code-claims", "873", "--flagged", "402", "--checked", "40", "--confirmed", "1")
        self.assertIn("estimated true rate: 1.2% (95% 0.2-5.9%)", out)
        out = run("q2_adjust.py", "--code-claims", "873", "--flagged", "402", "--checked", "40", "--confirmed", "8")
        self.assertIn("estimated true rate: 9.2% (95% 4.8-16.0%)", out)

    def test_labels_file(self):
        out = run("q2_adjust.py", "--code-claims", "100", "--flagged", "40", "--labels",
                  os.path.join(FIX, "q2_labeled.csv"), "--detector-class", "UNVERIFIED",
                  "--confirm", "CORRECT,RELAY,RECAP")
        self.assertIn("hand-check: 3/10 confirmed", out)

    def test_out_of_range_counts_are_rejected(self):
        for args in (("--code-claims", "100", "--flagged", "50", "--checked", "5", "--confirmed", "7"),
                     ("--code-claims", "100", "--flagged", "50", "--checked", "-3", "--confirmed", "1"),
                     ("--code-claims", "100", "--flagged", "150", "--checked", "10", "--confirmed", "1")):
            with self.subTest(args=args):
                p = subprocess.run([sys.executable, "-I", "-B", os.path.join(SCRIPTS, "q2_adjust.py"), *args],
                                   capture_output=True, text=True, timeout=60)
                self.assertEqual(p.returncode, 1, p.stderr)
                self.assertIn("0 <= confirmed <= checked", p.stdout)
                self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
