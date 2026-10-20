"""Stage 17: Raw per-contact EDD export for delayed_or_likely_to_be_delayed and
shipment_status_query_(no_delay), scoped to the 2026-05-01..2026-08-22 window (the population the
user has been auditing as "119k contacts"). Same methodology as Stage 15 (incl. the
order_analytics EDD fallback fix), but with a fixed column order/schema per explicit request and
two new derived columns: EDD Change (direction) and EDD change days (signed).

NOTE ON ROW COUNT: dedupable_raw.parquet for this window was regenerated 2026-09-03 (after several
pipeline bug fixes landed post the original 2026-08-27 run that reported "119,861 raw contacts").
Re-filtering the current, corrected data for the same two sub-dispositions over the same date range
now yields a different count -- this script uses the current, correct data and reports the actual
count rather than forcing 119,861.

Reads:  intermediate/run_2026-05-01_2026-08-22/dedupable_raw.parquet
        raw/orders/edd_revisions_full_with_status.parquet
        raw/orders/order_analytics_edd_fallback.parquet
        reference/order_status_master.csv
Writes: output/PRX_EDD_Raw_Export_2026-05-01_to_2026-08-22.xlsx
"""
import pandas as pd
from openpyxl.utils import get_column_letter

from pipeline_utils import PIPELINE_ROOT

TARGET_SUBDISPS = ["delayed_or_likely_to_be_delayed", "shipment_status_query_(no_delay)"]
WINDOW_DIR = PIPELINE_ROOT / "intermediate" / "run_2026-05-01_2026-08-22"


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
        return "Within"
    if contact_date == edd_date:
        return "On"
    return "Post"


def edd_change_label(orig, final):
    if pd.isna(orig) or pd.isna(final):
        return None
    if final > orig:
        return "Postponed"
    if final < orig:
        return "Pulled Early"
    return "No Change"


def main():
    dedupable = pd.read_parquet(WINDOW_DIR / "dedupable_raw.parquet")
    sub = dedupable[dedupable["normalized_sub_disposition"].isin(TARGET_SUBDISPS)].copy()
    sub = sub.rename(columns={"final_order_id": "order_id"})
    sub["order_id"] = sub["order_id"].astype("int64")
    sub["created_at"] = pd.to_datetime(sub["created_at"])
    print(f"[17] contacts in scope (raw, 2 sub-dispositions, 2026-05-01..2026-08-22): {len(sub)}")

    rev = pd.read_parquet(PIPELINE_ROOT / "raw" / "orders" / "edd_revisions_full_with_status.parquet")
    rev["order_id"] = rev["order_id"].astype("int64")
    rev["old_edd"] = pd.to_datetime(rev["old_edd"], format="mixed", errors="coerce")
    rev["new_edd"] = pd.to_datetime(rev["new_edd"], format="mixed", errors="coerce")
    rev["created_at"] = pd.to_datetime(rev["created_at"])
    rev = rev.dropna(subset=["order_id", "created_at"]).sort_values(["order_id", "created_at"])

    first_rev = rev.groupby("order_id").first().reset_index()
    first_rev["original_edd"] = first_rev["old_edd"].combine_first(first_rev["new_edd"])
    orig_map = first_rev.set_index("order_id")["original_edd"]

    last_rev = rev.groupby("order_id").last().reset_index()
    final_map = last_rev.set_index("order_id")["new_edd"]

    rev_count = rev.groupby("order_id").size().rename("edd_change_count")

    sub["original_edd"] = sub["order_id"].map(orig_map)
    sub["final_edd_ever"] = sub["order_id"].map(final_map)
    sub = sub.merge(rev_count, on="order_id", how="left")
    sub["edd_change_count"] = sub["edd_change_count"].fillna(0).astype(int)

    # Fallback fix: orders with zero rows in marketplace_edd_revisions still have a real EDD set
    # at creation, stored in marketplace_order_analytics.on_created_edd/edd. See scripts 14/15.
    fallback = pd.read_parquet(PIPELINE_ROOT / "raw" / "orders" / "order_analytics_edd_fallback.parquet")
    fallback_map = fallback.set_index("order_id")[["original_edd", "final_edd_ever"]]
    no_revision = sub["original_edd"].isna()
    sub.loc[no_revision, "original_edd"] = sub.loc[no_revision, "order_id"].map(fallback_map["original_edd"])
    sub.loc[no_revision, "final_edd_ever"] = sub.loc[no_revision, "order_id"].map(fallback_map["final_edd_ever"])
    fallback_filled = no_revision & sub["original_edd"].notna()
    print(f"[17] EDD fallback: filled {fallback_filled.sum()} of {no_revision.sum()} previously-blank rows "
          f"({sub['original_edd'].isna().sum()} contacts remain genuinely EDD-unknown)")

    rev_sorted = rev.sort_values("created_at")
    sub_sorted = sub.sort_values("created_at")
    merged = pd.merge_asof(
        sub_sorted, rev_sorted[["order_id", "created_at", "new_edd", "order_status_id"]],
        on="created_at", by="order_id", direction="backward", allow_exact_matches=True,
    )
    merged["active_edd_at_contact"] = merged["new_edd"].combine_first(merged["original_edd"])

    status_master = pd.read_csv(PIPELINE_ROOT / "reference" / "order_status_master.csv")
    status_master = status_master.drop_duplicates(subset="status_id", keep="first")
    status_map = status_master.set_index("status_id")[["status_name", "customer_status"]]
    merged = merged.merge(status_map, left_on="order_status_id", right_index=True, how="left")

    merged["contact_date"] = merged["created_at"].dt.normalize()
    merged["original_edd_date"] = merged["original_edd"].dt.normalize()
    merged["active_edd_date"] = merged["active_edd_at_contact"].dt.normalize()
    merged["vs_original"] = [position(c, e) for c, e in zip(merged["contact_date"], merged["original_edd_date"])]
    merged["vs_active_at_contact"] = [position(c, e) for c, e in zip(merged["contact_date"], merged["active_edd_date"])]

    merged["edd_change_label"] = [edd_change_label(o, f) for o, f in
                                   zip(merged["original_edd"], merged["final_edd_ever"])]
    merged["edd_change_days"] = (merged["final_edd_ever"] - merged["original_edd"]).dt.days

    order_contact_counts = merged.groupby("order_id").size().rename("total_contacts_for_order")
    merged = merged.merge(order_contact_counts, on="order_id", how="left")

    final = merged[[
        "order_id", "original_edd", "final_edd_ever", "edd_change_label", "edd_change_days",
        "edd_change_count", "contact_date", "active_edd_at_contact", "total_contacts_for_order",
        "normalized_sub_disposition", "channel", "sub_channel",
        "order_status_id", "status_name", "customer_status",
        "vs_original", "vs_active_at_contact",
    ]].rename(columns={
        "order_id": "Order ID",
        "original_edd": "Original EDD (first created)",
        "final_edd_ever": "Final EDD (last changed, ever)",
        "edd_change_label": "EDD Change",
        "edd_change_days": "EDD change days",
        "edd_change_count": "EDD Change Count",
        "contact_date": "Contact Made (date)",
        "active_edd_at_contact": "Active EDD at Time of Contact",
        "total_contacts_for_order": "Total Contacts for this Order",
        "normalized_sub_disposition": "Shipment Status / Delivery Delay",
        "channel": "Channel",
        "sub_channel": "Sub-Channel",
        "order_status_id": "Order Stage ID at Contact",
        "status_name": "Order Stage (internal)",
        "customer_status": "Order Stage (customer-facing)",
        "vs_original": "Within/On/Post (vs Original EDD)",
        "vs_active_at_contact": "Within/On/Post (vs EDD at Time of Contact)",
    }).sort_values(["Order ID", "Contact Made (date)"])

    out_path = PIPELINE_ROOT / "output" / "PRX_EDD_Raw_Export_2026-05-01_to_2026-08-22.xlsx"
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        final.to_excel(writer, sheet_name="Raw Data", index=False)
        autosize_and_freeze(writer, "Raw Data", final)

    print(f"[17] wrote {out_path} ({len(final)} rows)")


if __name__ == "__main__":
    main()
