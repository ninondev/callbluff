# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ninon
"""Q2, step 2: turn a detector rate into an estimated true rate.

The detector flags a share of code claims (flagged / code_claims). A rater
hand-checks a random sample of the flagged claims and confirms some of them.
The estimate is

    flagged_share x confirmed / checked

and the 95% interval is the Wilson interval of confirmed / checked, scaled
by flagged_share. The flagged share itself is treated as fixed (it is a full
count, not a sample).

Either give the counts directly, or point at the labeled sample CSV written
by q2_done_claims.py and say which hand labels count as confirmed:

  python3 -I q2_adjust.py --code-claims 873 --flagged 402 --checked 40 --confirmed 1
  python3 -I q2_adjust.py --code-claims 873 --flagged 402 --labels sample.csv \
      --detector-class UNVERIFIED --confirm CORRECT,RELAY,RECAP
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import wilson  # noqa: E402


def adjust(code_claims, flagged, checked, confirmed):
    share = flagged / code_claims
    lo, hi = wilson(confirmed, checked)
    prec = confirmed / checked
    return {"flagged_share": share, "precision": prec, "precision_low": lo, "precision_high": hi,
            "estimate": share * prec, "low": share * lo, "high": share * hi}


def count_labels(path, detector_class, confirm):
    ok = {x.strip().upper() for x in confirm.split(",") if x.strip()}
    checked = confirmed = 0
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            if detector_class and r.get("detector_class") != detector_class:
                continue
            lab = (r.get("hand_label") or "").strip()
            if not lab:
                continue
            checked += 1
            base = lab.split("(")[0].split(":")[0].strip().upper()
            if base in ok:
                confirmed += 1
    return checked, confirmed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--code-claims", type=int, required=True)
    ap.add_argument("--flagged", type=int, required=True)
    ap.add_argument("--checked", type=int)
    ap.add_argument("--confirmed", type=int)
    ap.add_argument("--labels", help="labeled sample CSV (columns detector_class, hand_label)")
    ap.add_argument("--detector-class", default="")
    ap.add_argument("--confirm", default="CORRECT", help="comma list of hand labels that confirm the flag")
    a = ap.parse_args(argv)
    if a.labels:
        checked, confirmed = count_labels(a.labels, a.detector_class, a.confirm)
    else:
        checked, confirmed = a.checked, a.confirmed
    if not checked or confirmed is None or not a.code_claims:
        print("need --checked and --confirmed, or a --labels file with hand labels")
        return 1
    if not 0 <= confirmed <= checked or not 0 <= a.flagged <= a.code_claims:
        # Out-of-range counts otherwise crash inside the Wilson interval or
        # print impossible percentages.
        print("need 0 <= confirmed <= checked and 0 <= flagged <= code-claims")
        return 1
    r = adjust(a.code_claims, a.flagged, checked, confirmed)
    print(f"flagged {a.flagged}/{a.code_claims} = {100*r['flagged_share']:.1f}% of code claims")
    print(f"hand-check: {confirmed}/{checked} confirmed = {100*r['precision']:.1f}%"
          f" (wilson95 {100*r['precision_low']:.1f}-{100*r['precision_high']:.1f}%)")
    print(f"estimated true rate: {100*r['estimate']:.1f}% (95% {100*r['low']:.1f}-{100*r['high']:.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
