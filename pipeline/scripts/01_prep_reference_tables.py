"""Stage 01: parse/clean the two disposition reference CSVs into flat, pipeline-ready tables.

Reads:
  ../../Dispositions/Dispositions - New-Support-Dispositions.csv   (messy two-block layout)
  ../../Dispositions/Disposition_Actionability.csv
Writes:
  reference/dispositions_master.csv
  reference/disposition_actionability.csv   (copy + is_legacy_unmapped flag)
"""
import csv
import sys
import pandas as pd
from pipeline_utils import PIPELINE_ROOT, normalize_disposition_value

SRC_DISPOSITIONS = PIPELINE_ROOT.parent / "Dispositions" / "Dispositions - New-Support-Dispositions.csv"
SRC_ACTIONABILITY = PIPELINE_ROOT.parent / "Dispositions" / "Disposition_Actionability.csv"

EXPECTED_DISPOSITIONS_ROWS = 89   # tripwire: alert if the source file's shape changes silently
EXPECTED_ACTIONABILITY_ROWS = 174


def parse_dispositions_master() -> pd.DataFrame:
    with open(SRC_DISPOSITIONS, newline="", encoding="utf-8-sig", errors="replace") as f:
        rows = list(csv.reader(f))

    if len(rows) != EXPECTED_DISPOSITIONS_ROWS:
        print(f"[WARN] Dispositions master row count changed: expected {EXPECTED_DISPOSITIONS_ROWS}, "
              f"got {len(rows)}. Verify the source file wasn't edited unexpectedly.", file=sys.stderr)

    # Row 0 = title comments, row 1 = header, rows 2+ = data.
    # Block 1 = columns 1-4 (Status, Disposition, Sub Disposition, Order ID requirement), effective 2026-03-12
    # Block 2 = columns 6-9 (same shape), effective 2026-06-03
    block1, block2 = [], []
    for r in rows[2:]:
        if len(r) < 5:
            continue
        status, disp, subdisp, orderid = r[1].strip(), r[2].strip(), r[3].strip(), r[4].strip()
        if disp or subdisp:
            block1.append({
                "status": status, "disposition": disp, "sub_disposition": subdisp,
                "order_id_requirement": orderid, "effective_date": "2026-03-12",
            })
        if len(r) >= 10:
            status2, disp2, subdisp2, orderid2 = r[6].strip(), r[7].strip(), r[8].strip(), r[9].strip()
            if disp2 or subdisp2:
                block2.append({
                    "status": status2, "disposition": disp2, "sub_disposition": subdisp2,
                    "order_id_requirement": orderid2, "effective_date": "2026-06-03",
                })

    df = pd.DataFrame(block1 + block2)
    df["sub_disposition_normalized"] = df["sub_disposition"].map(normalize_disposition_value)
    return df


def prep_actionability() -> pd.DataFrame:
    df = pd.read_csv(SRC_ACTIONABILITY, dtype=str, keep_default_na=False)
    if len(df) + 1 != EXPECTED_ACTIONABILITY_ROWS:
        print(f"[WARN] Disposition_Actionability.csv row count changed: expected "
              f"{EXPECTED_ACTIONABILITY_ROWS - 1} data rows, got {len(df)}.", file=sys.stderr)

    notes_col = "Notes"
    df["is_legacy_unmapped"] = df[notes_col].str.contains("Legacy/unmapped", na=False)
    df["sub_disposition_normalized"] = df["Sub-Disposition (Raw Key)"].map(normalize_disposition_value)
    return df


def main():
    ref_dir = PIPELINE_ROOT / "reference"
    ref_dir.mkdir(parents=True, exist_ok=True)

    master = parse_dispositions_master()
    master.to_csv(ref_dir / "dispositions_master.csv", index=False)
    print(f"[01] dispositions_master.csv: {len(master)} rows "
          f"({(master['effective_date'] == '2026-03-12').sum()} from 12-Mar-2026 block, "
          f"{(master['effective_date'] == '2026-06-03').sum()} from 03-Jun-2026 block)")

    actionability = prep_actionability()
    actionability.to_csv(ref_dir / "disposition_actionability.csv", index=False)
    legacy_pct = actionability["is_legacy_unmapped"].mean()
    print(f"[01] disposition_actionability.csv: {len(actionability)} rows, "
          f"{legacy_pct:.1%} flagged legacy/unmapped (needs sign-off before treating as final)")


if __name__ == "__main__":
    main()
