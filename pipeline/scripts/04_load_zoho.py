"""Stage 04: load + classify Zoho email export.

Reads:  raw/zoho/*.csv  (Zoho ticket export; 4-line preamble then a header row)
Writes: intermediate/run_.../zoho_classified.parquet
"""
import sys
import pandas as pd
from pipeline_utils import (
    load_config, intermediate_dir, PIPELINE_ROOT, normalize_disposition_value,
    normalize_phone, normalize_email, extract_single_numeric_id, fallback_order_join,
    fallback_order_join_by_email,
)

EXCLUDED_CHANNELS = {"instagram", "forums", "facebook", "platinum rx insta", "twitter",
                      "contact us", "web", "google my business"}


def read_zoho_csv(path):
    """Zoho exports carry a 4-line report-metadata preamble before the real header row.
    Returns (dataframe, generated_at) -- generated_at is parsed from the 'Generated On' line
    so overlapping exports can be prioritized by recency during dedup.
    """
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        lines = f.readlines()
    header_idx = None
    for i, line in enumerate(lines[:10]):
        if line.startswith("Ticket Id,"):
            header_idx = i
            break
    if header_idx is None:
        sys.exit(f"[04] FATAL: could not find 'Ticket Id,' header row in {path} within first 10 lines")

    generated_at = None
    for line in lines[:5]:
        if line.startswith("Generated On"):
            raw = line.split(",")[1].strip().strip('"')  # only the 2nd CSV field -- the row has
                                                            # many trailing empty columns after it
            for fmt in ("%d %b %Y %I:%M %p", "%d %b %Y %H:%M"):
                try:
                    generated_at = pd.to_datetime(raw, format=fmt)
                    break
                except ValueError:
                    continue
            break

    from io import StringIO
    df = pd.read_csv(StringIO("".join(lines[header_idx:])), dtype=str, keep_default_na=False)
    return df, generated_at


def main():
    cfg = load_config()
    raw_dir = PIPELINE_ROOT / cfg["zoho"]["raw_dir"]
    files = sorted(raw_dir.glob("*.csv"))
    if not files:
        print(f"[04] WARNING: no Zoho CSVs found in {raw_dir} — Zoho channel will be empty this run.")
        out_dir = intermediate_dir(cfg)
        pd.DataFrame().to_parquet(out_dir / "zoho_classified.parquet", index=False)
        return

    frames = []
    for f in files:
        df, generated_at = read_zoho_csv(f)
        df["_source_file"] = f.name
        df["_generated_at"] = generated_at
        frames.append(df)
        print(f"[04] {f.name}: {len(df)} rows, generated_at={generated_at}")
    zoho = pd.concat(frames, ignore_index=True)

    # Different Zoho exports use different timestamp formats in the data rows themselves (12h with
    # AM/PM vs 24h) -- a single fixed format silently NaT's out an entire file. Try both.
    created_raw = zoho["Created Time (Ticket)"]
    parsed_12h = pd.to_datetime(created_raw, errors="coerce", format="%d %b %Y %I:%M %p")
    parsed_24h = pd.to_datetime(created_raw, errors="coerce", format="%d %b %Y %H:%M")
    zoho["created_at_dt"] = parsed_12h.combine_first(parsed_24h)
    unparsed = zoho["created_at_dt"].isna() & created_raw.str.strip().ne("-") & created_raw.str.strip().ne("")
    if unparsed.any():
        print(f"[04] WARNING: {unparsed.sum()} rows' Created Time (Ticket) matched neither known "
              f"format, e.g.: {created_raw[unparsed].head(3).tolist()}")
    if zoho["created_at_dt"].notna().any():
        print(f"[04] Combined raw coverage (all files, pre-dedup/pre-range-filter): "
              f"{zoho['created_at_dt'].min()} to {zoho['created_at_dt'].max()} ({len(zoho)} rows total)")

    # Multiple overlapping exports (e.g. a 90-day dump + an older 120-day dump) can carry the same
    # Ticket Id -- keep the row from whichever file was generated most recently, since a ticket's
    # disposition/status may have changed between exports.
    before_dedup = len(zoho)
    zoho = zoho.sort_values("_generated_at", ascending=False).drop_duplicates(
        subset="Ticket Id", keep="first"
    )
    print(f"[04] Cross-file Ticket Id dedup: {before_dedup} -> {len(zoho)} rows "
          f"({before_dedup - len(zoho)} duplicate tickets resolved by most-recent export)")

    # Scope to the configured run range, same as every other channel (LimeChat/Calls are already
    # pulled/filtered to this window -- Zoho's multi-month exports would otherwise leak extra
    # months into raw totals).
    start = pd.Timestamp(cfg["run"]["start_date"])
    end = pd.Timestamp(cfg["run"]["end_date"])
    in_range = zoho["created_at_dt"].between(start, end - pd.Timedelta(seconds=1))
    out_of_range = (~in_range).sum()
    zoho = zoho[in_range].copy()
    print(f"[04] Scoped to run range {cfg['run']['start_date']}..{cfg['run']['end_date']}: "
          f"{len(zoho)} rows in range, {out_of_range} rows outside range dropped")

    # BRD: filter to Channel == 'Email' ONLY -- excludes Instagram/Forums/Facebook/Twitter/
    # Contact Us/Web (Web = L1 escalation of tickets already counted elsewhere -> double count risk).
    channel_norm = zoho["Channel"].str.strip().str.lower()
    zoho["is_email_channel"] = channel_norm.eq("email")
    excluded_seen = channel_norm[~zoho["is_email_channel"]].value_counts()
    print(f"[04] Channel distribution (all rows):\n{zoho['Channel'].value_counts()}")
    print(f"[04] Rows excluded as non-Email channel: {(~zoho['is_email_channel']).sum()} "
          f"({(~zoho['is_email_channel']).mean():.1%})")

    email_only = zoho[zoho["is_email_channel"]].copy()

    # Final Disposition = Dispositions + ' | ' + Sub-disposition, falling back to Issue Related to.
    def final_disposition(row):
        d, s = row.get("Dispositions", "").strip(), row.get("Sub-disposition", "").strip()
        if d or s:
            return f"{d} | {s}".strip(" |")
        return row.get("Issue Related to", "").strip()

    email_only["final_disposition_raw"] = email_only.apply(final_disposition, axis=1)
    email_only["normalized_disposition"] = email_only["Dispositions"].map(normalize_disposition_value)
    email_only["normalized_sub_disposition"] = email_only["Sub-disposition"].map(normalize_disposition_value)
    # If both blank, fall back to normalizing 'Issue Related to' so nothing silently drops to
    # not_disposed when a real (if unstructured) signal exists.
    both_blank = email_only["Dispositions"].str.strip().eq("") & email_only["Sub-disposition"].str.strip().eq("")
    email_only.loc[both_blank, "normalized_sub_disposition"] = (
        email_only.loc[both_blank, "Issue Related to"].map(normalize_disposition_value)
    )

    email_only["order_id_direct"] = email_only["Order ID"].map(extract_single_numeric_id)
    email_only["phone_normalized"] = email_only["Phone Number"].map(normalize_phone)
    email_only["email_normalized"] = email_only["Email (Ticket)"].map(normalize_email)
    email_only["sub_channel"] = "Email"

    order_fill = email_only["order_id_direct"].notna().mean() if len(email_only) else 0.0
    print(f"[04] Email-channel rows: {len(email_only)}; direct Order-ID fill rate: {order_fill:.1%} "
          f"(BRD's known rate ~50.1%)")

    out_dir = intermediate_dir(cfg)

    # 20-day phone fallback for rows with no direct Order ID (standardized helper shared with
    # LimeChat/Calls -- fallback order's created_at must be strictly earlier than the ticket).
    # 96.8% of Email tickets carry no phone number at all, so the phone pass alone leaves Email's
    # fallback match rate near-zero (0.1%, confirmed) -- not because phone-matching is broken, but
    # because there's almost nothing to match on. Add an email-keyed second pass (same 20-day/
    # strictly-earlier semantics) for whatever the phone pass didn't fill; the fallback table's own
    # email field matches ~41% of the time when tried, which is real recoverable signal.
    fallback_path = out_dir / "fallback_orders.parquet"
    if fallback_path.exists() and len(email_only):
        fallback = pd.read_parquet(fallback_path)
        email_only = fallback_order_join(email_only, fallback, cfg["fallback_join"]["window_days"], "created_at_dt")
        phone_fallback_fill = email_only["order_id_fallback"].notna().mean()
        email_only = fallback_order_join_by_email(email_only, fallback, cfg["fallback_join"]["window_days"], "created_at_dt")
        email_fallback_fill = email_only["order_id_fallback"].notna().mean()
        print(f"[04] order_id_fallback fill: phone-pass {phone_fallback_fill:.1%} -> "
              f"phone+email-pass {email_fallback_fill:.1%}")
        email_only["final_order_id"] = email_only["order_id_direct"].combine_first(email_only["order_id_fallback"])
        final_fill = email_only["final_order_id"].notna().mean()
        print(f"[04] final_order_id fill after phone+email 20-day fallback: {final_fill:.1%}")
    else:
        email_only["final_order_id"] = email_only["order_id_direct"]
    email_only.to_parquet(out_dir / "zoho_classified.parquet", index=False)
    print(f"[04] wrote {len(email_only)} Zoho email rows -> {out_dir / 'zoho_classified.parquet'}")


if __name__ == "__main__":
    main()
