"""Stage 02: load + merge raw LimeChat monthly exports.

Reads:  raw/limechat/*.csv  (one file per month, symlinked from Data dumps/Limechat)
Writes: intermediate/run_.../limechat_raw.parquet
"""
import sys
import pandas as pd
from pathlib import Path
from pipeline_utils import load_config, intermediate_dir, PIPELINE_ROOT

EXPECTED_MONTH_ROWCOUNTS = {
    "2026-05": 110255,
    "2026-06": 121111,
    "2026-07": 182344,
    "2026-08": 191752,
    "2026-09": 134948,
}


def month_of(path: Path) -> str:
    return path.stem[:7]  # "2026-05_may.csv" -> "2026-05"


def main():
    cfg = load_config()
    raw_dir = PIPELINE_ROOT / cfg["limechat"]["raw_dir"]
    july_file = cfg["limechat"]["july_file_to_use"]

    files = sorted(raw_dir.glob("*.csv"))
    if not files:
        sys.exit(f"[02] FATAL: no LimeChat CSVs found in {raw_dir}")

    # Guard: if a duplicate July export sneaks into the folder alongside the configured one, abort
    # rather than silently double-counting July's ~182k tickets.
    july_candidates = [f for f in files if month_of(f) == "2026-07"]
    if len(july_candidates) > 1:
        chosen = [f for f in july_candidates if f.name == july_file]
        if not chosen:
            sys.exit(f"[02] FATAL: multiple July files present {july_candidates} but none match "
                      f"configured july_file_to_use={july_file!r}")
        files = [f for f in files if month_of(f) != "2026-07"] + chosen
        print(f"[02] Multiple July files detected — using configured {july_file} only, "
              f"dropping {[f.name for f in july_candidates if f.name != july_file]}")

    frames = []
    months_loaded = set()
    for f in files:
        df = pd.read_csv(f, dtype=str, keep_default_na=False, low_memory=False)
        df["_source_file"] = f.name
        m = month_of(f)
        months_loaded.add(m)
        n = len(df)
        expected = EXPECTED_MONTH_ROWCOUNTS.get(m)
        flag = "OK" if expected is None or n == expected else f"MISMATCH (expected {expected})"
        print(f"[02] {f.name}: {n} rows, month={m} [{flag}]")
        frames.append(df)

    limechat = pd.concat(frames, ignore_index=True)

    # Coverage check against the configured run range.
    start = pd.Timestamp(cfg["run"]["start_date"])
    end = pd.Timestamp(cfg["run"]["end_date"])
    required_months = set(pd.period_range(start, end - pd.Timedelta(days=1), freq="M").astype(str))
    missing_months = sorted(required_months - months_loaded)

    if missing_months:
        msg = (f"[02] COVERAGE GAP: run range {cfg['run']['start_date']}..{cfg['run']['end_date']} "
               f"requires LimeChat months {sorted(required_months)}, but only {sorted(months_loaded)} "
               f"are present in {raw_dir}. Missing: {missing_months}.")
        if cfg["run"].get("allow_partial_coverage"):
            print(f"[02] WARNING (allow_partial_coverage=true, continuing with partial data): {msg}")
        else:
            sys.exit(f"[02] FATAL (allow_partial_coverage=false): {msg}")

    out_dir = intermediate_dir(cfg)
    limechat.to_parquet(out_dir / "limechat_raw.parquet", index=False)
    print(f"[02] wrote {len(limechat)} total LimeChat rows -> {out_dir / 'limechat_raw.parquet'}")
    print(f"[02] months present: {sorted(months_loaded)}; missing: {missing_months or 'none'}")

    # Stash coverage info for the Run_Metadata sheet later.
    (out_dir / "_coverage_limechat.txt").write_text(
        f"months_present={sorted(months_loaded)}\nmonths_missing={missing_months}\n"
        f"july_file_used={july_file}\n"
    )


if __name__ == "__main__":
    main()
