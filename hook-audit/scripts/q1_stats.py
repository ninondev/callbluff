# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ninon
"""Q1, step 2: precision of guard-hook fires from a labeled sample.

Reads the CSV written by q1_hook_fires.py after a rater has filled in:
  label           TP (real problem), FP (false alarm) or UNCLEAR
  behavior_after  what the agent did next; "n/a..." for shadow-mode fires

Prints, per stratum and overall: counts, the TP rate with a 95% Wilson
interval, a TP+UNCLEAR upper reading, a population-weighted TP rate (each
stratum's rate times its unjudged population), and the behavior counts after
blocking fires. Only aggregates are printed.

--prior TP,FP,UNCLEAR adds verdicts judged before this sample to an extra
"all judged" line.

Usage: python3 -I q1_stats.py LABELED.csv [--prior 0,20,5]
"""
import argparse
import collections
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import wilson  # noqa: E402

LABELS = ("TP", "FP", "UNCLEAR")


def summarize(rows):
    by = collections.defaultdict(collections.Counter)
    pop = {}
    for r in rows:
        lab = (r.get("label") or "").strip().upper()
        if lab not in LABELS:
            continue
        k = r.get("stratum") or r.get("hook") or "unknown"
        by[k][lab] += 1
        try:
            pop[k] = int(r.get("population") or 0)
        except ValueError:
            pop[k] = 0
    return by, pop


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("labels")
    ap.add_argument("--prior", help="TP,FP,UNCLEAR counts judged earlier, outside this sample")
    a = ap.parse_args(argv)
    with open(a.labels, newline="") as fh:
        rows = list(csv.DictReader(fh))
    by, pop = summarize(rows)
    if not by:
        print("no labeled rows")
        return 1

    print("stratum\tn\tTP\tFP\tUNCLEAR\tpopulation\tTP_rate\twilson95_low\twilson95_high")
    tot = collections.Counter()
    wtp = wpop = 0.0
    for k in sorted(by):
        c = by[k]
        n = sum(c.values())
        lo, hi = wilson(c["TP"], n)
        print(f"{k}\t{n}\t{c['TP']}\t{c['FP']}\t{c['UNCLEAR']}\t{pop.get(k, 0)}\t{c['TP']/n:.3f}\t{lo:.3f}\t{hi:.3f}")
        tot.update(c)
        if pop.get(k):
            wtp += pop[k] * c["TP"] / n
            wpop += pop[k]
    n = sum(tot.values())
    lo, hi = wilson(tot["TP"], n)
    print(f"ALL\t{n}\t{tot['TP']}\t{tot['FP']}\t{tot['UNCLEAR']}\t{int(wpop)}\t{tot['TP']/n:.3f}\t{lo:.3f}\t{hi:.3f}")
    k2 = tot["TP"] + tot["UNCLEAR"]
    lo2, hi2 = wilson(k2, n)
    print(f"TP+UNCLEAR upper reading: {k2}/{n} = {k2/n:.3f} (wilson95 {lo2:.3f}-{hi2:.3f})")
    if wpop:
        print(f"population-weighted TP rate: {wtp/wpop:.3f} (about {wtp:.1f} of {int(wpop)} fires)")
    if a.prior:
        p = [int(x) for x in a.prior.split(",")]
        allc = collections.Counter(TP=tot["TP"] + p[0], FP=tot["FP"] + p[1], UNCLEAR=tot["UNCLEAR"] + p[2])
        m = sum(allc.values())
        lo3, hi3 = wilson(allc["TP"], m)
        print(f"all judged (sample + prior): n={m} TP={allc['TP']} FP={allc['FP']} UNCLEAR={allc['UNCLEAR']}"
              f" TP_rate={allc['TP']/m:.3f} wilson95 {lo3:.3f}-{hi3:.3f}")
    beh = collections.Counter((r.get("behavior_after") or "").strip() for r in rows
                              if (r.get("label") or "").strip().upper() in LABELS
                              and not (r.get("behavior_after") or "").strip().lower().startswith("n/a"))
    if beh:
        print(f"behavior after blocking fires (n={sum(beh.values())}):")
        for b, c in beh.most_common():
            print(f"  {b or '(blank)'}\t{c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
