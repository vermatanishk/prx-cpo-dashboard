"""Stage 14: EDD Funnel deep-dive on delayed_or_likely_to_be_delayed / shipment_status_query_(no_delay).

Per-contact classification against Original EDD (earliest known promise) and Final EDD (latest
known promise, i.e. the order's own EDD-revision history's last new_edd) -- a codified, date-based
rule, not text judgment.

DATA SOURCE: prx_prod_db.marketplace_edd_revisions (order_id, old_edd, new_edd, reason_for_change,
created_at) -- pulled via 19 order_id-range ClickHouse queries (1.0M-3.6M, 100K bands, aggregated
server-side with argMinIf/argMaxIf per order_id to keep each query's result size manageable through
the MCP tool) and cached at raw/orders/edd_revisions_agg.parquet. Re-pull by re-running those range
queries if this cache goes stale.

KNOWN SIMPLIFICATION (flagged, not silently applied): "Updated EDD" here is the order's FINAL/latest
ever-recorded new_edd, not a strict point-in-time reconstruction of "the EDD as known at the exact
moment of this contact." A true point-in-time join (merge_asof per order against the contact
timestamp) would require the full per-revision row history for all 76,972 target orders, which was
not pulled (only per-order aggregates: earliest old_edd, latest new_edd, revision count) -- pulling
full history hit ClickHouse MCP response-size limits at the volumes involved here. This means a
contact classified "within" the Final EDD might actually have occurred before a LATER revision that
pushed the EDD even further out -- the funnel below is still fully honest about this via the
vs_original x vs_final matrix (see the large "after original / within final" cell), just not a
strict per-second timeline.

KEY FINDING requiring a decision (see chat): the vs_original x vs_final matrix has a large cell
(48,094 of 119,861 raw contacts, 40%) where contact happened AFTER the original EDD but still WITHIN
the (already-revised) final EDD -- i.e. the order was already late per its original promise, EDD got
pushed back, and the contact landed in the gap. This doesn't cleanly fit "purely anxiety-driven"
(original promise already missed) nor "post-EDD comms failure" (current promise not yet missed).
Reported as its own explicit bucket below rather than force-fit into one of the three.

Reads:  intermediate/.../dedupable_raw.parquet
        raw/orders/edd_revisions_agg.parquet
Writes: output/PRX_EDD_Funnel_DelayedShipmentQuery_2026-05-01_to_2026-08-22.xlsx
"""
import pandas as pd
from openpyxl.utils import get_column_letter

from pipeline_utils import load_config, intermediate_dir, PIPELINE_ROOT

TARGET_SUBDISPS = ["delayed_or_likely_to_be_delayed", "shipment_status_query_(no_delay)"]


def autosize_and_freeze(writer, sheet_name, df):
    ws = writer.sheets[sheet_name]
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for i, col in enumerate(df.columns, start=1):
        width = min(60, max(10, int(df[col].astype(str).map(len).clip(upper=60).max() if len(df) else 10) + 2))
        ws.column_dimensions[get_column_letter(i)].width = width


def position(contact_date, edd_date):
    if pd.isna(edd_date):
        return None
    if contact_date < edd_date:
        return "within"
    if contact_date == edd_date:
        return "on"
    return "after"


def bucket_4way(vs_original, vs_final):
    if vs_final is None:
        return "EDD unknown"
    if vs_final == "on":
        return "Delivery-day / transparency"
    if vs_final == "after":
        return "Failure of proactive comms (post-EDD)"
    # vs_final == "within"
    if vs_original == "after":
        return "Already-delayed, EDD revised (contact before new date)"
    return "Purely anxiety-driven (within all promised EDDs)"


def main():
    cfg = load_config()
    out_dir = intermediate_dir(cfg)

    dedupable = pd.read_parquet(out_dir / "dedupable_raw.parquet")
    sub = dedupable[dedupable["normalized_sub_disposition"].isin(TARGET_SUBDISPS)].copy()
    print(f"[14] contacts in scope (raw, 2 sub-dispositions): {len(sub)}")

    edd = pd.read_parquet(PIPELINE_ROOT / "raw" / "orders" / "edd_revisions_agg.parquet")
    edd["original_edd"] = pd.to_datetime(edd["original_edd"], format="mixed")
    edd["final_edd"] = pd.to_datetime(edd["final_edd"], format="mixed")

    sub = sub.merge(edd[["order_id", "original_edd", "final_edd", "revision_count"]],
                     left_on="final_order_id", right_on="order_id", how="left")
    match_rate = sub["original_edd"].notna().mean()
    print(f"[14] EDD match rate (revision history only): {match_rate:.1%}")

    # FALLBACK: marketplace_edd_revisions only has a row when an EDD was REVISED, so orders whose
    # EDD was set once at creation and never revised show up as "no match" above even though a
    # real EDD exists. Fill from marketplace_order_analytics.on_created_edd/edd (verified: 99.85%
    # of no-revision orders have a valid value there) -- see script 15's docstring for the full
    # investigation. Only orders missing EDD in BOTH sources stay "EDD unknown".
    fallback = pd.read_parquet(PIPELINE_ROOT / "raw" / "orders" / "order_analytics_edd_fallback.parquet")
    fallback_map = fallback.set_index("order_id")[["original_edd", "final_edd_ever"]]
    no_match = sub["original_edd"].isna()
    sub.loc[no_match, "original_edd"] = sub.loc[no_match, "final_order_id"].map(fallback_map["original_edd"])
    sub.loc[no_match, "final_edd"] = sub.loc[no_match, "final_order_id"].map(fallback_map["final_edd_ever"])
    match_rate_after = sub["original_edd"].notna().mean()
    print(f"[14] EDD match rate (with order_analytics fallback): {match_rate_after:.1%}")

    sub["contact_date"] = pd.to_datetime(sub["created_at"]).dt.normalize()
    sub["orig_edd_date"] = sub["original_edd"].dt.normalize()
    sub["final_edd_date"] = sub["final_edd"].dt.normalize()
    sub["vs_original"] = [position(c, e) for c, e in zip(sub["contact_date"], sub["orig_edd_date"])]
    sub["vs_final"] = [position(c, e) for c, e in zip(sub["contact_date"], sub["final_edd_date"])]
    sub["bucket"] = [bucket_4way(o, f) for o, f in zip(sub["vs_original"], sub["vs_final"])]

    funnel_matrix = pd.crosstab(sub["vs_original"].fillna("unknown"), sub["vs_final"].fillna("unknown"), margins=True)
    bucket_counts = sub["bucket"].value_counts().rename_axis("Bucket").reset_index(name="Count")
    bucket_counts["% of total"] = bucket_counts["Count"] / len(sub)

    by_channel = sub.groupby(["channel", "bucket"]).size().unstack(fill_value=0)
    by_subdisp = sub.groupby(["normalized_sub_disposition", "bucket"]).size().unstack(fill_value=0)

    start, end = cfg["run"]["start_date"], cfg["run"]["end_date"]
    out_path = PIPELINE_ROOT / "output" / f"PRX_EDD_Funnel_DelayedShipmentQuery_{start}_to_{end}.xlsx"
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        bucket_counts.to_excel(writer, sheet_name="Headline Buckets", index=False)
        autosize_and_freeze(writer, "Headline Buckets", bucket_counts)

        funnel_matrix.to_excel(writer, sheet_name="3x3 Funnel Matrix")

        by_channel.reset_index().to_excel(writer, sheet_name="By Channel", index=False)
        by_subdisp.reset_index().to_excel(writer, sheet_name="By Sub-Disposition", index=False)

        notes = pd.DataFrame({"Note": [
            f"Scope: {len(sub)} raw contacts (delayed_or_likely_to_be_delayed + "
            "shipment_status_query_(no_delay)), from dedupable_raw.parquet (has Order ID, Sales stripped).",
            f"EDD match rate: {match_rate:.1%} against prx_prod_db.marketplace_edd_revisions "
            "(order_id range 1.0M-3.6M pulled; a small number of orders outside that range or with "
            "zero revision rows are 'EDD unknown').",
            "'Final EDD' = the order's latest-ever recorded new_edd, NOT a strict point-in-time "
            "reconstruction of the EDD as known exactly at each contact's timestamp -- see script "
            "docstring for why (ClickHouse MCP response-size limits at this volume).",
            "The 'Already-delayed, EDD revised (contact before new date)' bucket (48,094 rows, 40% "
            "of scope) is a 4th category not in the original 3-bucket ask -- it's the largest single "
            "cell in the funnel matrix and doesn't cleanly fit 'purely anxiety' (original promise "
            "already missed) or 'post-EDD failure' (current promise not yet missed). Flagged for a "
            "decision on how to fold it into the 3 headline buckets, not silently assigned.",
        ]})
        notes.to_excel(writer, sheet_name="Notes", index=False)
        autosize_and_freeze(writer, "Notes", notes)

    print(bucket_counts)
    print(f"[14] wrote {out_path}")


if __name__ == "__main__":
    main()
