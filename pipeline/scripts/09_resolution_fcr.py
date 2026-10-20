"""Stage 09: Resolution / FCR.

Resolved = no further contact for the same key within 48 hours. Reported at two grains (per the
brief, explicitly non-additive):
  - Order ID level, keyed by (Order ID, month)
  - Order ID x Actionable-Sub-disposition ("issue") level, keyed by (Order ID, Sub-disposition, month)

Reads:  intermediate/.../dedupable_raw.parquet   (all raw Support contacts WITH a usable Order ID)
Writes: intermediate/.../resolution_by_order.parquet
        intermediate/.../resolution_by_issue.parquet
"""
import pandas as pd
from pipeline_utils import load_config, intermediate_dir


def contacts_to_resolution(group_df, window_hours):
    """Given all contacts for one key sorted by time, count touches until a 48h gap (resolution)."""
    times = group_df["created_at"].sort_values().tolist()
    window = pd.Timedelta(hours=window_hours)
    touches_per_resolution = []
    count = 1
    for i in range(1, len(times)):
        if times[i] - times[i - 1] > window:
            touches_per_resolution.append(count)
            count = 1
        else:
            count += 1
    touches_per_resolution.append(count)
    return touches_per_resolution


def main():
    cfg = load_config()
    out_dir = intermediate_dir(cfg)
    window_hours = cfg["resolution"]["window_hours"]

    dedupable = pd.read_parquet(out_dir / "dedupable_raw.parquet")
    dedupable["month"] = dedupable["created_at"].dt.to_period("M").astype(str)

    # --- Order ID level ---
    order_results = []
    for (order_id, month), g in dedupable.groupby(["final_order_id", "month"]):
        touches = contacts_to_resolution(g, window_hours)
        bot_only = g["sub_channel"].isin(["Ringg", "LimeChat Bot"]).all()
        order_results.append({
            "final_order_id": order_id, "month": month, "n_resolutions": len(touches),
            "touches_per_resolution": touches, "max_touches": max(touches),
            "bot_contained_whole_journey": bot_only,
        })
    order_df = pd.DataFrame(order_results)
    order_df.to_parquet(out_dir / "resolution_by_order.parquet", index=False)
    print(f"[09] Order-ID-level resolution: {len(order_df)} (order_id, month) keys")

    # --- Order ID x Sub-disposition ("issue") level ---
    issue_results = []
    for (order_id, subdisp, month), g in dedupable.groupby(["final_order_id", "normalized_sub_disposition", "month"]):
        touches = contacts_to_resolution(g, window_hours)
        bot_only = g["sub_channel"].isin(["Ringg", "LimeChat Bot"]).all()
        issue_results.append({
            "final_order_id": order_id, "sub_disposition": subdisp, "month": month,
            "n_resolutions": len(touches), "touches_per_resolution": touches,
            "max_touches": max(touches), "bot_contained_whole_journey": bot_only,
        })
    issue_df = pd.DataFrame(issue_results)
    issue_df.to_parquet(out_dir / "resolution_by_issue.parquet", index=False)
    print(f"[09] Order-ID x Sub-disposition-level resolution: {len(issue_df)} keys "
          f"(intentionally does not sum to the Order-ID-level count above)")

    # Flatten touches-per-resolution into a histogram for Tab 3 of the report.
    all_touches_order = [t for row in order_df["touches_per_resolution"] for t in row]
    all_touches_issue = [t for row in issue_df["touches_per_resolution"] for t in row]
    hist_df = pd.DataFrame({
        "touches": pd.Series(all_touches_order, dtype="Int64").value_counts().sort_index().index,
    })
    hist_by_order = pd.Series(all_touches_order).value_counts().sort_index()
    hist_by_issue = pd.Series(all_touches_issue).value_counts().sort_index()
    hist_out = pd.DataFrame({
        "touches_to_resolution": sorted(set(hist_by_order.index) | set(hist_by_issue.index)),
    })
    hist_out["count_by_order"] = hist_out["touches_to_resolution"].map(hist_by_order).fillna(0).astype(int)
    hist_out["count_by_issue"] = hist_out["touches_to_resolution"].map(hist_by_issue).fillna(0).astype(int)
    hist_out.to_parquet(out_dir / "resolution_histogram.parquet", index=False)
    print(f"[09] resolution histogram written; median touches-to-resolution "
          f"(by order)={pd.Series(all_touches_order).median()}, "
          f"(by issue)={pd.Series(all_touches_issue).median()}")


if __name__ == "__main__":
    main()
