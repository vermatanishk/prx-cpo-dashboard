"""Stage 15: Row-level EDD/order-stage detail per contact, for delayed_or_likely_to_be_delayed
and shipment_status_query_(no_delay) -- one row per contact (raw, not deduped), per the user's
explicit request for a granular view (the Stage 14 aggregate funnel was "too superficial").

Columns: Order ID, Original EDD (first created), Final EDD (last changed, ever), EDD Change Count,
Contact Made (date), Total Contacts for this Order, Shipment Status / Delivery Delay (sub-disposition),
Channel, Sub-Channel, Order Stage at Contact (id + internal name + customer-facing name),
Active EDD at Time of Contact, Within/On/Post vs Original EDD, Within/On/Post vs the EDD active
at the moment of contact.

DATA SOURCES:
- raw/orders/edd_revisions_full_with_status.parquet -- FULL (non-aggregated) per-revision history
  (order_id, old_edd, new_edd, order_status_id, created_at) for order_id 1.0M-3.6M, pulled via 22
  ClickHouse range-band queries (see chat for the exact bands -- density required splitting the
  3.2M-3.5M range into 25-50K sub-bands to stay under the MCP tool's response-size limit). 83.3%
  of the 76,972 target orders have at least one revision row.
- reference/order_status_master.csv -- prx_prod_db.marketplace_status_master (status_id ->
  status_name / customer_status), 83 rows, pulled once (small, static lookup).

METHODOLOGY:
- Original EDD = earliest revision's old_edd for that order (fallback to that revision's new_edd
  if old_edd is null on the very first row).
- Active EDD at Time of Contact = TRUE point-in-time lookup via merge_asof (direction=backward):
  the new_edd of the most recent revision at or before the contact's timestamp. If the contact
  predates every revision (or the order has none), falls back to Original EDD. This is the exact
  point-in-time reconstruction that Stage 14's aggregate "Final EDD" simplification could not do at
  full population scale -- 70.0% of contacts land after at least one real revision; the rest use
  the Original EDD fallback (no revision had happened yet by contact time).
- Order Stage at Contact = order_status_id carried on that SAME matched revision row (embedded in
  marketplace_edd_revisions itself, not a separate marketplace_order_status_log pull -- that table
  is ~10x denser and pulling full history for all target orders was not tractable through this
  tool). This means order stage is only as fresh as the last EDD-triggering event, not every status
  transition in between -- flagged as an approximation, not a full status-transition timeline.
- Within/On/Post: contact's calendar date vs. the relevant EDD's calendar date.

Reads:  intermediate/.../dedupable_raw.parquet
        raw/orders/edd_revisions_full_with_status.parquet
        reference/order_status_master.csv
Writes: output/PRX_EDD_ContactLevel_Detail_2026-05-01_to_2026-08-22.xlsx
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
        return "Within"
    if contact_date == edd_date:
        return "On"
    return "Post"


def main():
    cfg = load_config()
    out_dir = intermediate_dir(cfg)

    dedupable = pd.read_parquet(out_dir / "dedupable_raw.parquet")
    sub = dedupable[dedupable["normalized_sub_disposition"].isin(TARGET_SUBDISPS)].copy()
    sub = sub.rename(columns={"final_order_id": "order_id"})
    sub["order_id"] = sub["order_id"].astype("int64")
    sub["created_at"] = pd.to_datetime(sub["created_at"])
    print(f"[15] contacts in scope (raw, 2 sub-dispositions): {len(sub)}")

    rev = pd.read_parquet(PIPELINE_ROOT / "raw" / "orders" / "edd_revisions_full_with_status.parquet")
    rev["order_id"] = rev["order_id"].astype("int64")
    rev["old_edd"] = pd.to_datetime(rev["old_edd"], format="mixed", errors="coerce")
    rev["new_edd"] = pd.to_datetime(rev["new_edd"], format="mixed", errors="coerce")
    rev["created_at"] = pd.to_datetime(rev["created_at"])
    rev = rev.dropna(subset=["order_id", "created_at"]).sort_values(["order_id", "created_at"])
    print(f"[15] revision history rows: {len(rev)}")

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

    # FALLBACK (fixes the S0/M0 "No EDD data" misclassification): marketplace_edd_revisions is a
    # change-log -- it only has a row when an EDD was REVISED. Orders whose EDD was set once at
    # creation and never revised have zero rows there, so orig_map/final_map above come back null
    # for them even though a real EDD exists. Verified via prx_prod_db.marketplace_order_analytics:
    # of orders with zero revision rows, 99.85% have a valid on_created_edd/edd there (see chat).
    # Cross-checked the revision-based original_edd against on_created_edd for orders that have
    # BOTH (99.2% exact match, rest off by 1-2 days) -- the revision-based logic itself is sound,
    # this fallback only fills the true gap, it doesn't replace working values.
    fallback = pd.read_parquet(PIPELINE_ROOT / "raw" / "orders" / "order_analytics_edd_fallback.parquet")
    fallback_map = fallback.set_index("order_id")[["original_edd", "final_edd_ever"]]
    no_revision = sub["original_edd"].isna()
    fb_orig = sub.loc[no_revision, "order_id"].map(fallback_map["original_edd"])
    fb_final = sub.loc[no_revision, "order_id"].map(fallback_map["final_edd_ever"])
    sub.loc[no_revision, "original_edd"] = fb_orig
    sub.loc[no_revision, "final_edd_ever"] = fb_final
    fallback_filled = no_revision & sub["original_edd"].notna()
    print(f"[15] EDD fallback (order_analytics.on_created_edd/edd, no revision history): "
          f"filled {fallback_filled.sum()} of {no_revision.sum()} previously-blank rows "
          f"({sub['original_edd'].isna().sum()} contacts remain genuinely EDD-unknown)")

    rev_sorted = rev.sort_values("created_at")
    sub_sorted = sub.sort_values("created_at")
    merged = pd.merge_asof(
        sub_sorted, rev_sorted[["order_id", "created_at", "new_edd", "order_status_id"]],
        on="created_at", by="order_id", direction="backward", allow_exact_matches=True,
    )
    merged["active_edd_at_contact"] = merged["new_edd"].combine_first(merged["original_edd"])
    match_rate = merged["new_edd"].notna().mean()
    print(f"[15] point-in-time revision match rate: {match_rate:.1%} "
          f"(remainder falls back to Original EDD -- no revision had happened by contact time)")

    status_master = pd.read_csv(PIPELINE_ROOT / "reference" / "order_status_master.csv")
    status_master = status_master.drop_duplicates(subset="status_id", keep="first")
    status_map = status_master.set_index("status_id")[["status_name", "customer_status"]]
    merged = merged.merge(status_map, left_on="order_status_id", right_index=True, how="left")

    merged["contact_date"] = merged["created_at"].dt.normalize()
    merged["original_edd_date"] = merged["original_edd"].dt.normalize()
    merged["active_edd_date"] = merged["active_edd_at_contact"].dt.normalize()
    merged["vs_original"] = [position(c, e) for c, e in zip(merged["contact_date"], merged["original_edd_date"])]
    merged["vs_active_at_contact"] = [position(c, e) for c, e in zip(merged["contact_date"], merged["active_edd_date"])]

    order_contact_counts = merged.groupby("order_id").size().rename("total_contacts_for_order")
    merged = merged.merge(order_contact_counts, on="order_id", how="left")

    final = merged[[
        "order_id", "original_edd", "final_edd_ever", "edd_change_count",
        "contact_date", "total_contacts_for_order",
        "normalized_sub_disposition", "channel", "sub_channel",
        "order_status_id", "status_name", "customer_status",
        "active_edd_at_contact", "vs_original", "vs_active_at_contact",
    ]].rename(columns={
        "order_id": "Order ID",
        "original_edd": "Original EDD (first created)",
        "final_edd_ever": "Final EDD (last changed, ever)",
        "edd_change_count": "EDD Change Count",
        "contact_date": "Contact Made (date)",
        "total_contacts_for_order": "Total Contacts for this Order",
        "normalized_sub_disposition": "Shipment Status / Delivery Delay",
        "channel": "Channel",
        "sub_channel": "Sub-Channel",
        "order_status_id": "Order Stage ID at Contact",
        "status_name": "Order Stage (internal)",
        "customer_status": "Order Stage (customer-facing)",
        "active_edd_at_contact": "Active EDD at Time of Contact",
        "vs_original": "Within/On/Post (vs Original EDD)",
        "vs_active_at_contact": "Within/On/Post (vs EDD at Time of Contact)",
    }).sort_values(["Order ID", "Contact Made (date)"])

    notes = pd.DataFrame({"Note": [
        f"Scope: {len(final)} raw contacts, one row each (delayed_or_likely_to_be_delayed + "
        "shipment_status_query_(no_delay)), from dedupable_raw.parquet.",
        f"{match_rate:.1%} of rows have a true point-in-time EDD match (a real revision existed at "
        "or before the contact); the rest fall back to Original EDD because no revision had "
        "happened yet by contact time -- this is a real state, not a data gap.",
        "'Order Stage at Contact' is read off the SAME EDD-revision event as the point-in-time EDD "
        "match, not a separate full status-transition history (marketplace_order_status_log is "
        "~10x denser and wasn't pulled in full) -- it reflects the stage as of the last "
        "EDD-triggering event before contact, which may lag the true real-time stage by hours.",
        "Rows with no EDD-revision history for their order now fall back to "
        "marketplace_order_analytics.on_created_edd/edd (the EDD set at order creation, present "
        "whether or not it was ever later revised) -- fixes the earlier misclassification of "
        "these as 'No EDD data'. Only orders with no EDD in EITHER source remain genuinely blank.",
    ]})

    start, end = cfg["run"]["start_date"], cfg["run"]["end_date"]
    out_path = PIPELINE_ROOT / "output" / f"PRX_EDD_ContactLevel_Detail_{start}_to_{end}.xlsx"
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        final.to_excel(writer, sheet_name="Contact-Level Detail", index=False)
        autosize_and_freeze(writer, "Contact-Level Detail", final)
        notes.to_excel(writer, sheet_name="Notes", index=False)
        autosize_and_freeze(writer, "Notes", notes)

    print(f"[15] wrote {out_path} ({len(final)} rows)")


if __name__ == "__main__":
    main()
