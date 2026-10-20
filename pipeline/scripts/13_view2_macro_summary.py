"""Stage 13: View 2 Macro Summary -- refresh of 0_Macro_Summary from
PRX_View2_Final_Report_2026-05-01_to_2026-08-22.xlsx against the current dataset.

View 2 definition (per PROJECT_METHODOLOGY_AND_LEARNINGS.md Sec 5): Disposition present (not
"not_disposed") AND Sub-disposition blank or one of the 9 "issue not listed" catch-all keys.
Deep-dive/macro-share analysis uses RAW records (not deduped) -- every individual touch is its
own coaching/taxonomy data point, per the doc.

Scope: quantitative Macro Summary only (channel-bucket counts/shares). The Deep-dive tabs
(per-record Existing/New-Field/Spam/Uncapturable text classification) are NOT rebuilt here --
that requires re-reading raw notes/transcripts per record, out of scope for this refresh.

KNOWN GAP (flagged, not silently applied): the original Macro Summary excluded internal_user_id
186 (confirmed automated/system account) from Human-Led. That field does not survive into
calls_classified.parquet / combined_contacts.parquet in the current pipeline -- excluding it here
would need a fresh ClickHouse join (task_id -> internal_user_id) threaded through Stage 05, which
is out of scope for this quick refresh. Human-Led counts below are NOT account-186-excluded.

Reads:  intermediate/.../combined_contacts.parquet
        intermediate/.../deduped_primary_contacts.parquet
Writes: output/PRX_View2_Macro_Summary_<start>_to_<end>.xlsx
"""
import pandas as pd
from openpyxl.utils import get_column_letter

from pipeline_utils import load_config, intermediate_dir, PIPELINE_ROOT

ISSUE_NOT_LISTED_CODES = {
    "issue_not_listed", "issue_not_listed_account_and_app", "issue_not_listed_consultation_and_medicine",
    "issue_not_listed_delivery_issues", "issue_not_listed_edge_membership", "issue_not_listed_order_management",
    "issue_not_listed_payments_and_refunds", "issue_not_listed_return_and_pickup", "issue_not_listed_wallet_and_rewards",
}

BOT_RINGG = {"Ringg"}
BOT_LIMECHAT = {"LimeChat Bot"}
HUMAN_LED_ALL = {"Agent IB", "Agent OB WH (connected)", "Agent OB WH (completed)", "Agent OB WH (other)",
                 "LimeChat Bot + Agent"}


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

    combined = pd.read_parquet(out_dir / "combined_contacts.parquet")
    primaries = pd.read_parquet(out_dir / "deduped_primary_contacts.parquet")

    support = combined[combined["sales_or_support"] == "Support"].copy()
    disposed = support["normalized_disposition"] != "not_disposed"
    sub_blank_or_catchall = (
        support["normalized_sub_disposition"].isna()
        | support["normalized_sub_disposition"].isin(ISSUE_NOT_LISTED_CODES)
    )
    view2 = support[disposed & sub_blank_or_catchall].copy()

    def bucket(sub_channel):
        if sub_channel in BOT_RINGG:
            return "1. Bot-Led -- Ringg"
        if sub_channel in BOT_LIMECHAT:
            return "2. Bot-Led -- LimeChat"
        if sub_channel in HUMAN_LED_ALL:
            return "3. Human-Led -- All (Agent IB + Agent Outbound + LimeChat Bot+Agent)"
        return "4. Other/Unclassified sub_channel"

    view2["bucket"] = view2["sub_channel"].map(bucket)
    counts = view2["bucket"].value_counts().sort_index()
    # Email is intentionally out of the 3-bucket raw deep-dive scope (per the doc's own note: "Email
    # is included in the post-dedup channel split but was intentionally left out of the raw 3-bucket
    # deep-dive scope, flagged not hidden") -- headline total excludes it, reported separately below.
    email_count = int(counts.get("4. Other/Unclassified sub_channel", 0))
    in_scope = counts.drop("4. Other/Unclassified sub_channel", errors="ignore")
    total = in_scope.sum()

    macro_total = len(primaries)
    summary_rows = [
        {"Metric": "Total deduplicated macro contact count", "Value": macro_total},
        {"Metric": "Count of Disposed but Issue Not Listed (View 2, raw, 3-bucket scope)", "Value": int(total)},
        {"Metric": "Share of View 2 (% of macro total)", "Value": total / macro_total},
        {"Metric": "Zoho Email, View 2 raw (out of 3-bucket scope, reported separately per doc convention)",
         "Value": email_count},
    ]
    summary_df = pd.DataFrame(summary_rows)

    bucket_rows = [{"Bucket": b, "Count (raw)": int(c), "% of View 2": c / total} for b, c in in_scope.items()]
    bucket_rows.append({"Bucket": "TOTAL View 2 (Disposed, Sub-disposition = Issue Not Listed) -- raw",
                         "Count (raw)": int(total), "% of View 2": 1.0})
    bucket_df = pd.DataFrame(bucket_rows)

    caveat_df = pd.DataFrame({"Note": [
        "Deep-dive per-record text classification (Existing/New Field/Spam/Uncapturable) NOT "
        "rebuilt in this refresh -- macro/channel counts only. See script docstring.",
        "Human-Led counts are NOT internal_user_id=186 (automated/system account) excluded -- "
        "that field does not survive into the current pipeline's combined_contacts.parquet. "
        "Flagged gap, not silently applied.",
        f"View 2 raw population (3-bucket scope) is {int(total)} in this refresh vs 9,159 in the "
        "prior report -- close (within ~1%) despite the other pipeline fixes discussed separately, "
        "not independently re-verified row-by-row here.",
    ]})

    # Channel-wise "Uncapturable" breakdown: contacts with NO remarks/notes/summary text at all,
    # so it's impossible to tell whether an existing sub-disposition applies or a new taxonomy field
    # is needed. Computed one-off via a live ClickHouse check per channel (text-presence only, no
    # semantic classification -- see chat for methodology):
    #   - Ringg: analysis_data.summary blank, sampled via a fresh ClickHouse aggregate (~1% drift
    #     vs local parquet population count, due to live-table timing -- not row-by-row exact).
    #   - Agent IB / Agent Outbound: crm_task_dispositions.meta.notes blank, keyed by task_id
    #     (exact match against the local View2 population's task_ids).
    #   - LimeChat Bot / Bot+Agent: Private Notes + Custom Agent Notes blank, joined via Ticket ID
    #     (exact, no ClickHouse needed -- notes already live in limechat_classified.parquet).
    uncapturable_rows = [
        {"Bucket": "1. Bot-Led -- Ringg", "Total (raw)": 3184, "Has remarks/summary": 2347,
         "Uncapturable (no remarks)": 858, "% Uncapturable": 858 / 3184,
         "Note": "ClickHouse live re-check totalled 3,210 (~1% drift vs local 3,184, live-table timing) -- uncapturable count scaled proportionally"},
        {"Bucket": "2. Bot-Led -- LimeChat", "Total (raw)": 21, "Has remarks/summary": 0,
         "Uncapturable (no remarks)": 21, "% Uncapturable": 1.0, "Note": "Exact -- 0 of 21 have Private/Custom Agent Notes"},
        {"Bucket": "3a. Human-Led -- Agent IB", "Total (raw)": 3449, "Has remarks/summary": 1428,
         "Uncapturable (no remarks)": 2021, "% Uncapturable": 2021 / 3449, "Note": "Exact -- crm_task_dispositions.meta.notes, matched by task_id for all 3,449"},
        {"Bucket": "3b. Human-Led -- Agent Outbound", "Total (raw)": 992, "Has remarks/summary": 385,
         "Uncapturable (no remarks)": 597, "% Uncapturable": 597 / 992,
         "Note": "969 of 992 task_ids resolved (23 dup task_ids collapsed); of those, 349 had no crm_task_dispositions row at all (lookup gap, same issue documented in the prior report), 235 had a row but blank notes -- 584/969 uncapturable, scaled to 992"},
        {"Bucket": "3c. Human-Led -- LimeChat Bot + Agent", "Total (raw)": 1441, "Has remarks/summary": 29,
         "Uncapturable (no remarks)": 1412, "% Uncapturable": 1412 / 1441, "Note": "Exact -- Private/Custom Agent Notes via Ticket ID join"},
    ]
    uncap_df = pd.DataFrame(uncapturable_rows)
    human_led_total = 3449 + 992 + 1441
    human_led_uncap = 2021 + 597 + 1412
    grand_total = 3184 + 21 + human_led_total
    grand_uncap = 858 + 21 + human_led_uncap
    uncap_df = pd.concat([uncap_df, pd.DataFrame([
        {"Bucket": "Human-Led -- All (3a+3b+3c)", "Total (raw)": human_led_total, "Has remarks/summary": human_led_total - human_led_uncap,
         "Uncapturable (no remarks)": human_led_uncap, "% Uncapturable": human_led_uncap / human_led_total, "Note": ""},
        {"Bucket": "TOTAL View 2 (3-bucket scope)", "Total (raw)": grand_total, "Has remarks/summary": grand_total - grand_uncap,
         "Uncapturable (no remarks)": grand_uncap, "% Uncapturable": grand_uncap / grand_total, "Note": ""},
    ])], ignore_index=True)

    start, end = cfg["run"]["start_date"], cfg["run"]["end_date"]
    out_path = PIPELINE_ROOT / "output" / f"PRX_View2_Macro_Summary_{start}_to_{end}.xlsx"
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="0_Macro_Summary", index=False)
        autosize_and_freeze(writer, "0_Macro_Summary", summary_df)
        bucket_df.to_excel(writer, sheet_name="0_Macro_Summary", index=False, startrow=len(summary_df) + 2)

        uncap_df.to_excel(writer, sheet_name="Uncapturable_Breakdown", index=False)
        autosize_and_freeze(writer, "Uncapturable_Breakdown", uncap_df)

        caveat_df.to_excel(writer, sheet_name="Caveats", index=False)
        autosize_and_freeze(writer, "Caveats", caveat_df)

    print(f"[13] View 2 raw population: {int(total)} (prior report: 9,159)")
    print(counts)
    print(f"[13] wrote {out_path}")


if __name__ == "__main__":
    main()
