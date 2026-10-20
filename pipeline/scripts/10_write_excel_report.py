"""Stage 10: write the final .xlsx report per the agreed spec.

Tab 1: Summary metrics + channel-volume tables (before/after dedup), by the 7-channel list.
Tab 2: De-duplicated issue breakdown by sub-disposition.
Tab 3: Resolution histogram.
Plus Run_Metadata and Raw_Audit sheets for traceability.
"""
import json
import pandas as pd
from openpyxl.utils import get_column_letter
from pipeline_utils import load_config, intermediate_dir, PIPELINE_ROOT

CHANNEL_ORDER = [
    "Ringg", "Agent IB", "Agent OB WH (connected)", "LimeChat Bot",
    "LimeChat Bot + Agent", "Email", "Agent OB WH (completed)",
]
BOT_CHANNELS = {"Ringg", "LimeChat Bot"}


def autosize_and_freeze(writer, sheet_name, df):
    ws = writer.sheets[sheet_name]
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for i, col in enumerate(df.columns, start=1):
        width = min(60, max(10, int(df[col].astype(str).map(len).clip(upper=60).max() if len(df) else 10) + 2))
        ws.column_dimensions[get_column_letter(i)].width = width


def main():
    cfg = load_config()
    out_dir = intermediate_dir(cfg)
    ref_dir = PIPELINE_ROOT / "reference"

    support = pd.read_parquet(out_dir / "support_contacts.parquet")
    primaries = pd.read_parquet(out_dir / "deduped_primary_contacts.parquet")
    not_dedupable = pd.read_parquet(out_dir / "not_dedupable_raw.parquet")
    orders_denom = pd.read_parquet(out_dir / "orders_denominator.parquet")
    fallback = pd.read_parquet(out_dir / "fallback_orders.parquet")
    hist = pd.read_parquet(out_dir / "resolution_histogram.parquet")
    resolution_by_order = pd.read_parquet(out_dir / "resolution_by_order.parquet")

    start = pd.Timestamp(cfg["run"]["start_date"])
    end = pd.Timestamp(cfg["run"]["end_date"])

    # Bot-resolved status: sourced from Stage 09's resolution_by_order.parquet, which is the
    # correct, already-existing 48-hour-aware, whole-order-journey definition (a contact only
    # counts as bot-resolved if EVERY touch on that order in that month was Ringg/LimeChat Bot AND
    # there was no further contact within 48h of the last one). Previously Stage 08 computed a much
    # cruder per-(order, sub-disposition) "all rows in this narrow group are bot-channel" flag with
    # no 48h concept -- that column has been retired; join the correct one in here instead.
    primaries["month"] = pd.to_datetime(primaries["created_at"]).dt.to_period("M").astype(str)
    bot_orders = resolution_by_order.loc[
        resolution_by_order["bot_contained_whole_journey"], ["final_order_id", "month"]
    ].drop_duplicates()
    bot_orders["bot_contained_whole_journey"] = True
    primaries = primaries.merge(bot_orders, on=["final_order_id", "month"], how="left")
    primaries["bot_contained_whole_journey"] = primaries["bot_contained_whole_journey"].fillna(False)

    # Post-dedup universe = Primary contacts (order-ID-bearing groups collapsed to one) plus every
    # contact that couldn't be deduped at all (no usable Order ID) -- each of those still counts as
    # its own contact since nothing merges it with anything else.
    not_dedupable["is_actionable"] = False
    # No-Order-ID rows have no order to look up in resolution_by_order -- approximate with a per-row
    # channel check (not 48h-aware) since there's no order-level journey to evaluate for them. Only
    # affects Tab 2's bot%/human% visibility for this passthrough population, not the CPO-scoped
    # Bot-Resolved Contacts / CPO Bot metrics below, which use `primaries` only.
    not_dedupable["bot_contained_whole_journey"] = not_dedupable["sub_channel"].isin(BOT_CHANNELS)
    not_dedupable["raw_contacts_in_group"] = 1
    post_dedup_cols = ["sub_channel", "final_order_id", "normalized_disposition",
                        "normalized_sub_disposition", "is_actionable", "bot_contained_whole_journey",
                        "raw_contacts_in_group", "created_at"]
    for c in post_dedup_cols:
        if c not in primaries.columns:
            primaries[c] = None
        if c not in not_dedupable.columns:
            not_dedupable[c] = None
    post_dedup = pd.concat([primaries[post_dedup_cols], not_dedupable[post_dedup_cols]], ignore_index=True)

    # ---- Summary metrics ----
    orders_in_range = orders_denom[orders_denom["order_date"].between(start, end - pd.Timedelta(days=1))]
    total_orders = int(orders_in_range["total_orders_processed"].sum())

    fb_in_range = fallback[fallback["created_at"].between(start, end)]
    total_unique_users = int(fb_in_range["user_id"].nunique())

    order_to_user = fallback.drop_duplicates("order_id").set_index("order_id")["user_id"]

    orders_with_contact = support["final_order_id"].dropna().unique()
    orders_with_contact_actionable = primaries.loc[primaries["is_actionable"], "final_order_id"].dropna().unique()

    users_with_contact = pd.Series(orders_with_contact).map(order_to_user).dropna().unique()

    # CPO is contacts-PER-ORDER -- a contact that never resolves to an Order ID (even after the
    # 20-day fallback) can't logically contribute to that ratio. Confirmed empirically: including
    # these passthrough contacts gave CPO ~2x the prior analysis's figures; excluding them from the
    # CPO-specific metrics (but NOT from raw/disposition reporting, where they stay fully visible)
    # reproduced the prior analysis's CPO within ~1pp. See plan doc "Amendment: CPO numerator scope
    # fix" for the full investigation.
    total_contacts_raw = len(support)
    total_contacts_after_dedup = len(primaries)
    bot_resolved_after_dedup = int(primaries["bot_contained_whole_journey"].fillna(False).sum())
    excluded_no_orderid = len(not_dedupable)

    summary_rows = [
        ("Total Orders", total_orders),
        ("Total Orders w/ Support Contact", len(orders_with_contact)),
        ("Total Orders w/ Support Contact - Actionable", len(orders_with_contact_actionable)),
        ("% Support-Driven Orders", len(orders_with_contact) / total_orders if total_orders else 0),
        ("Total Unique Users (all orders)", total_unique_users),
        ("Total Unique Users w/ Support Contact", len(users_with_contact)),
        ("% Users Who Contact for Support at Least Once",
         len(users_with_contact) / total_unique_users if total_unique_users else 0),
        ("Total Contacts Made (raw, support-only, sales stripped)", total_contacts_raw),
        ("Total Contacts After De-dup (new logic, Order-ID-bearing only -- see Run_Metadata)",
         total_contacts_after_dedup),
        ("Bot-Resolved Contacts After De-dup", bot_resolved_after_dedup),
        ("CPO Overall", total_contacts_after_dedup / total_orders if total_orders else 0),
        ("CPO Bot", bot_resolved_after_dedup / total_orders if total_orders else 0),
        ("  (memo) Raw contacts excluded from CPO -- no Order ID even after 20-day fallback",
         excluded_no_orderid),
    ]
    summary_df = pd.DataFrame(summary_rows, columns=["Metric", "Value"])

    # ---- Channel volume tables ----
    # "After Dedup" here uses Primary contacts ONLY (Order-ID-bearing) -- same CPO-scope reasoning
    # as the summary metrics above. Tab 2 (Disposition_Breakdown) still uses the full post_dedup
    # universe (Primaries + no-Order-ID passthrough) so those issues stay visible there.
    before_counts = support["sub_channel"].value_counts()
    after_counts = primaries["sub_channel"].value_counts()
    channel_table = pd.DataFrame({"Channel": CHANNEL_ORDER})
    channel_table["Before Dedup (raw, support-only)"] = channel_table["Channel"].map(before_counts).fillna(0).astype(int)
    channel_table["After Dedup (new logic, Order-ID-bearing only)"] = channel_table["Channel"].map(after_counts).fillna(0).astype(int)

    other_before = before_counts.get("Agent OB WH (other)", 0)
    other_after = after_counts.get("Agent OB WH (other)", 0)

    # ---- Tab 2: disposition breakdown ----
    actionability = pd.read_csv(ref_dir / "disposition_actionability.csv")
    disp_lookup = actionability.drop_duplicates("sub_disposition_normalized").set_index(
        "sub_disposition_normalized")[["Disposition (Parent Category)", "Actionable Classification"]]

    grp = post_dedup.groupby("normalized_sub_disposition")
    disp_breakdown = grp.agg(
        count=("normalized_sub_disposition", "size"),
        bot_resolved=("bot_contained_whole_journey", lambda s: s.fillna(False).sum()),
    ).reset_index()
    disp_breakdown["share"] = disp_breakdown["count"] / disp_breakdown["count"].sum()
    disp_breakdown["bot_pct"] = disp_breakdown["bot_resolved"] / disp_breakdown["count"]
    disp_breakdown["human_pct"] = 1 - disp_breakdown["bot_pct"]
    disp_breakdown = disp_breakdown.merge(
        disp_lookup, left_on="normalized_sub_disposition", right_index=True, how="left"
    )
    # channel split (pivot of counts per channel per sub-disposition)
    channel_pivot = post_dedup.pivot_table(
        index="normalized_sub_disposition", columns="sub_channel", values="final_order_id",
        aggfunc="size", fill_value=0,
    )
    disp_breakdown = disp_breakdown.merge(channel_pivot, left_on="normalized_sub_disposition",
                                           right_index=True, how="left")
    disp_breakdown = disp_breakdown.sort_values("count", ascending=False)
    disp_breakdown = disp_breakdown.rename(columns={
        "normalized_sub_disposition": "Sub-Disposition",
        "Disposition (Parent Category)": "Disposition (Parent)",
        "Actionable Classification": "Actionable",
        "count": "Count", "share": "Share of Total", "bot_pct": "Bot %", "human_pct": "Human %",
    }).drop(columns=["bot_resolved"])

    # ---- Tab 3: resolution histogram ----
    hist = hist.sort_values("touches_to_resolution")
    total_by_order = hist["count_by_order"].sum()
    total_by_issue = hist["count_by_issue"].sum()
    hist["pct_by_order"] = hist["count_by_order"] / total_by_order if total_by_order else 0
    hist["pct_by_issue"] = hist["count_by_issue"] / total_by_issue if total_by_issue else 0
    hist["cum_pct_by_order"] = hist["pct_by_order"].cumsum()
    hist["cum_pct_by_issue"] = hist["pct_by_issue"].cumsum()
    hist = hist.rename(columns={
        "touches_to_resolution": "Touches to Resolution", "count_by_order": "# Order IDs",
        "count_by_issue": "# Issues (Order ID x Sub-disp)", "pct_by_order": "% of Orders",
        "pct_by_issue": "% of Issues", "cum_pct_by_order": "Cumulative % (Orders)",
        "cum_pct_by_issue": "Cumulative % (Issues)",
    })

    # ---- Run metadata ----
    limechat_coverage = (out_dir / "_coverage_limechat.txt").read_text() if \
        (out_dir / "_coverage_limechat.txt").exists() else "n/a"
    meta_rows = [
        ("Run date range", f"{cfg['run']['start_date']} to {cfg['run']['end_date']} (exclusive)"),
        ("Version tag", cfg["run"]["version_tag"]),
        ("LimeChat coverage", limechat_coverage.replace("\n", " | ")),
        ("July file used", cfg["limechat"]["july_file_to_use"]),
        ("Zoho coverage", "Combined from two exports (90 Days Dump + L120D dump), deduped by "
                           "Ticket Id keeping the more-recently-generated row on overlap, scoped to "
                           "the run range -- full May 1 to Aug 21 coverage confirmed, zero missing days"),
        ("Total Support-only raw contacts", len(support)),
        ("Total Primary (deduped, Order-ID-bearing) contacts", len(primaries)),
        ("Total not-dedupable (no Order ID even after fallback) contacts",
         f"{len(not_dedupable)} -- EXCLUDED from CPO metrics, still visible in Disposition_Breakdown"),
        ("Agent OB WH (other) raw/post-dedup rows (excluded from the 7-channel tables)",
         f"{other_before} / {other_after}"),
        ("OPEN ITEMS", "See plan doc: LimeChat bot-only System Tags allowlist is a first-pass/"
                        "unapproved list; Disposition_Actionability.csv 52% legacy/unmapped; "
                        "dedup-key rule pending stakeholder re-confirmation; Agent-Outbound "
                        "timezone assumption unconfirmed; Agent OB WH (connected) vs (completed) "
                        "magnitudes look swapped vs. the prior analysis (bucketing-priority "
                        "artifact -- needs a source-definition check, not yet fixed)."),
    ]
    meta_df = pd.DataFrame(meta_rows, columns=["Item", "Value"])

    version_tag = cfg["run"]["version_tag"]
    out_path = PIPELINE_ROOT / "output" / (
        f"PRX_Aggregated_Contacts_{cfg['run']['start_date']}_to_{cfg['run']['end_date']}_{version_tag}.xlsx"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="Summary", index=False, startrow=0)
        channel_table.to_excel(writer, sheet_name="Summary", index=False, startrow=len(summary_df) + 3)
        autosize_and_freeze(writer, "Summary", summary_df)

        disp_breakdown.to_excel(writer, sheet_name="Disposition_Breakdown", index=False)
        autosize_and_freeze(writer, "Disposition_Breakdown", disp_breakdown)

        hist.to_excel(writer, sheet_name="Resolution_Histogram", index=False)
        autosize_and_freeze(writer, "Resolution_Histogram", hist)

        meta_df.to_excel(writer, sheet_name="Run_Metadata", index=False)
        autosize_and_freeze(writer, "Run_Metadata", meta_df)

        # Raw audit -- keep it light (key columns only) to keep file size reasonable.
        audit_cols = ["channel", "sub_channel", "created_at", "phone_normalized", "final_order_id",
                      "sales_or_support", "normalized_disposition", "normalized_sub_disposition",
                      "attribution_source", "attribution_confidence", "unmapped_normalized_value"]
        combined = pd.read_parquet(out_dir / "combined_contacts.parquet", columns=audit_cols)
        combined.to_excel(writer, sheet_name="Raw_Audit", index=False)
        autosize_and_freeze(writer, "Raw_Audit", combined)

    print(f"[10] wrote report -> {out_path}")
    print(f"[10] Summary metrics:\n{summary_df.to_string(index=False)}")
    print(f"\n[10] Channel table:\n{channel_table.to_string(index=False)}")
    print(f"\n[10] Agent OB WH (other) excluded rows: before={other_before}, after={other_after}")


if __name__ == "__main__":
    main()
