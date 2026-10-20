"""Stage 12: MoM Summary / Sub-Disposition Before-After Dedup / Contacts-to-Resolution report.

Consolidated Support DB = Order ID x Sub-disposition, per user direction: a contact counts only if
it has a usable Order ID (direct or the 20-day fallback join) and is not Sales-classified. There is
no separate "no Order ID" category in this report -- not_dedupable_raw.parquet is not read here.

  Before Dedup = dedupable_raw.parquet   (329,061 rows: Support, has Order ID, pre-dedup)
  After Dedup  = deduped_primary_contacts.parquet   (238,927 rows: one Primary per Order ID x
                 Sub-disposition group) -- this IS the consolidated Support DB.

Reads:  intermediate/.../dedupable_raw.parquet
        intermediate/.../deduped_primary_contacts.parquet
        intermediate/.../resolution_by_order.parquet
        intermediate/.../orders_denominator.parquet
Writes: output/PRX_CPO_Analysis_v4_<start>_to_<end>.xlsx
"""
import importlib.util
from pathlib import Path

import pandas as pd
from openpyxl.utils import get_column_letter

from pipeline_utils import load_config, intermediate_dir, PIPELINE_ROOT

# Reuse Stage 09's exact 48h-window touches-to-resolution function rather than re-deriving it.
_spec = importlib.util.spec_from_file_location(
    "resolution_fcr", Path(__file__).parent / "09_resolution_fcr.py"
)
_resolution_fcr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_resolution_fcr)
contacts_to_resolution = _resolution_fcr.contacts_to_resolution


def autosize_and_freeze(writer, sheet_name, df):
    ws = writer.sheets[sheet_name]
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for i, col in enumerate(df.columns, start=1):
        width = min(60, max(10, int(df[col].astype(str).map(len).clip(upper=60).max() if len(df) else 10) + 2))
        ws.column_dimensions[get_column_letter(i)].width = width


def build_mom_summary(primaries: pd.DataFrame, orders_denom: pd.DataFrame) -> pd.DataFrame:
    primaries = primaries.copy()
    primaries["month"] = pd.to_datetime(primaries["created_at"]).dt.to_period("M").astype(str)

    orders_denom = orders_denom.copy()
    orders_denom["month"] = pd.to_datetime(orders_denom["order_date"]).dt.to_period("M").astype(str)
    orders_by_month = orders_denom.groupby("month")["total_orders_processed"].sum()

    orders_w_contact = primaries.groupby("month")["final_order_id"].nunique()
    orders_w_contact_actionable = (
        primaries[primaries["is_actionable"]].groupby("month")["final_order_id"].nunique()
    )

    months = sorted(orders_by_month.index)
    rows = {
        "Total Orders": [orders_by_month.get(m, 0) for m in months],
        "Total Orders w Support Contact": [orders_w_contact.get(m, 0) for m in months],
        "Total Orders w Support Contact - actionable": [orders_w_contact_actionable.get(m, 0) for m in months],
    }
    df = pd.DataFrame(rows, index=months).T
    df.index.name = "Metric"
    pct = df.loc["Total Orders w Support Contact"] / df.loc["Total Orders"]
    df.loc["% support driven orders"] = pct
    return df.reset_index()


def build_subdisp_before_after(dedupable: pd.DataFrame, primaries: pd.DataFrame) -> pd.DataFrame:
    before = dedupable["normalized_sub_disposition"].value_counts()
    after = primaries["normalized_sub_disposition"].value_counts()
    out = pd.DataFrame({
        "Before Dedup - Count": before,
        "After Dedup - Count": after,
    }).fillna(0).astype(int)
    out["Before Dedup - Share%"] = out["Before Dedup - Count"] / out["Before Dedup - Count"].sum()
    out["After Dedup - Share%"] = out["After Dedup - Count"] / out["After Dedup - Count"].sum()
    out.index.name = "Sub-Disposition"
    out = out.sort_values("Before Dedup - Count", ascending=False).reset_index()
    return out[["Sub-Disposition", "Before Dedup - Count", "Before Dedup - Share%",
                "After Dedup - Count", "After Dedup - Share%"]]


def _resolution_cut(dedupable: pd.DataFrame, window_hours: int, group_cols: list) -> tuple[pd.DataFrame, dict]:
    """Explode (touches, bot_contained_whole_journey) per resolution event for one grouping, then
    build the histogram + summary stats table matching v3_slim's CUT layout."""
    results = []
    for _, g in dedupable.groupby(group_cols):
        touches = contacts_to_resolution(g, window_hours)
        bot_only = g["sub_channel"].isin(["Ringg", "LimeChat Bot"]).all()
        for t in touches:
            results.append({"touches": t, "bot_contained_whole_journey": bot_only})
    events = pd.DataFrame(results)

    total_keys = dedupable.groupby(group_cols).ngroups
    bucketed = events["touches"].clip(upper=5).astype(str).where(events["touches"] < 5, "5+")
    hist = events.groupby(bucketed).agg(
        total=("touches", "size"),
        bot_resolved=("bot_contained_whole_journey", "sum"),
    )
    hist["human_resolved"] = hist["total"] - hist["bot_resolved"]
    hist["pct_of_total"] = hist["total"] / hist["total"].sum()
    order = [str(i) for i in range(1, 5)] + ["5+"]
    hist = hist.reindex(order).fillna(0)
    hist["cumulative_pct"] = hist["pct_of_total"].cumsum()
    hist["containment_rate"] = hist["bot_resolved"] / hist["total"].replace(0, pd.NA)
    hist = hist.reset_index().rename(columns={
        "index": "# contacts to resolution", "total": "Total OrderIDs",
        "pct_of_total": "% of Total", "cumulative_pct": "Cumulative %",
        "bot_resolved": "Bot Resolved", "human_resolved": "Human Resolved",
        "containment_rate": "Containment Rate",
    })

    stats = {
        "Total keys": total_keys,
        "Mean": round(events["touches"].mean(), 2),
        "Median (P50)": events["touches"].median(),
        "P75": events["touches"].quantile(0.75),
        "P90": events["touches"].quantile(0.90),
        "P95": events["touches"].quantile(0.95),
        "P99": events["touches"].quantile(0.99),
        "P99.9": events["touches"].quantile(0.999),
        "Max (tail)": events["touches"].max(),
    }
    return hist, stats


def build_contacts_to_resolution(dedupable: pd.DataFrame, window_hours: int) -> list:
    """Returns a list of (label, hist_df, stats_dict) for the 2 cuts, written as one sheet."""
    cut1_hist, cut1_stats = _resolution_cut(dedupable, window_hours, ["final_order_id", "month"])
    cut2_pop = dedupable[dedupable["is_actionable"]]
    cut2_hist, cut2_stats = _resolution_cut(
        cut2_pop, window_hours, ["final_order_id", "normalized_sub_disposition", "month"]
    )
    return [
        ("CUT 1 - Order ID level (all support-only contacts per order)", cut1_hist, cut1_stats),
        ("CUT 2 - Order ID x Sub-disposition level, Actionable only", cut2_hist, cut2_stats),
    ]


def write_resolution_sheet(writer, sheet_name, cuts):
    startrow = 0
    for label, hist, stats in cuts:
        pd.DataFrame({label: []}).to_excel(writer, sheet_name=sheet_name, startrow=startrow, index=False)
        startrow += 2
        hist.to_excel(writer, sheet_name=sheet_name, startrow=startrow, index=False)
        stats_df = pd.DataFrame({"Stat": list(stats.keys()), "Value": list(stats.values())})
        stats_df.to_excel(writer, sheet_name=sheet_name, startrow=startrow, startcol=8, index=False)
        startrow += len(hist) + 3


def main():
    cfg = load_config()
    out_dir = intermediate_dir(cfg)

    dedupable = pd.read_parquet(out_dir / "dedupable_raw.parquet")
    dedupable["month"] = dedupable["created_at"].dt.to_period("M").astype(str)
    primaries = pd.read_parquet(out_dir / "deduped_primary_contacts.parquet")
    orders_denom = pd.read_parquet(out_dir / "orders_denominator.parquet")

    print(f"[12] Before Dedup (dedupable_raw): {len(dedupable)} rows")
    print(f"[12] After Dedup (deduped_primary_contacts): {len(primaries)} rows")

    mom = build_mom_summary(primaries, orders_denom)
    subdisp = build_subdisp_before_after(dedupable, primaries)
    resolution_cuts = build_contacts_to_resolution(dedupable, cfg["resolution"]["window_hours"])

    start, end = cfg["run"]["start_date"], cfg["run"]["end_date"]
    out_path = PIPELINE_ROOT / "output" / f"PRX_CPO_Analysis_v4_{start}_to_{end}.xlsx"
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        mom.to_excel(writer, sheet_name="MoM Summary", index=False)
        autosize_and_freeze(writer, "MoM Summary", mom)

        subdisp.to_excel(writer, sheet_name="Sub-Disp Before-After Dedup", index=False)
        autosize_and_freeze(writer, "Sub-Disp Before-After Dedup", subdisp)

        write_resolution_sheet(writer, "Contacts-to-Resolution", resolution_cuts)

    print(f"[12] wrote {out_path}")
    print(f"[12] Sub-Disp Before-After totals: Before={subdisp['Before Dedup - Count'].sum()}, "
          f"After={subdisp['After Dedup - Count'].sum()}")


if __name__ == "__main__":
    main()
