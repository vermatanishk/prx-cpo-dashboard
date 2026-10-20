"""Stage 24: Deep-dive report for 'missing_or_less_quantity_received' (Return & Pickup) -- size,
primary channel, FCR, contacts-to-resolution percentile distribution, and a full MECE root-cause
classification of every multi-touch (>1 contact) resolution episode. Same base method as Stage 21/22/
23, extended with a new priority-ordered root-cause classifier (Section 2 of the approved plan) since
no prior deepdive builds one programmatically -- 21-23 only do this qualitatively by hand in the .md
report; this stage covers the FULL multi-touch population, not a sample.

Reads:  intermediate/run_2026-05-01_2026-09-01/dedupable_raw.parquet
        intermediate/run_2026-05-01_2026-09-01/quantity_status_log.parquet
        intermediate/run_2026-05-01_2026-09-01/calls_ringg_raw.parquet (contact_id -> Ringg call_id, if present)
Writes: output/PRX_MissingOrLessQuantityReceived_DeepDive_2026-05-01_to_2026-09-01.xlsx
"""
import pandas as pd
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

from pipeline_utils import PIPELINE_ROOT

TARGET = "missing_or_less_quantity_received"
WINDOW_DIR = PIPELINE_ROOT / "intermediate" / "run_2026-05-01_2026-09-01"
WINDOW_HOURS = 48
HEADER_FILL = "632423"
DUPLICATE_LEG_SECONDS = 15  # starting assumption -- confirmed real pairs this session were 0-2s apart

MECE_ORDER = [
    "backend_cancel_reopen",
    "duplicate_leg_artifact",
    "channel_handoff",
    "multi_day_followup",
    "same_day_multi_call",
    "other_unclassified",
]


def autosize_and_freeze(writer, sheet_name, df):
    ws = writer.sheets[sheet_name]
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for i, col in enumerate(df.columns, start=1):
        width = min(60, max(10, int(df[col].astype(str).map(len).clip(upper=60).max() if len(df) else 10) + 2))
        ws.column_dimensions[get_column_letter(i)].width = width


def style_header(writer, sheet_name, n_cols):
    ws = writer.sheets[sheet_name]
    for c in range(1, n_cols + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=HEADER_FILL)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def build_episodes(order_df):
    """Same 48h-gap segmentation as scripts 21-23, extended to also carry the contact-level slice
    for each episode (needed by the MECE classifier -- the template's original version only kept
    aggregated fields)."""
    g = order_df.sort_values("created_at").reset_index(drop=True)
    times = g["created_at"].tolist()
    channels = g["channel"].tolist()
    sub_channels = g["sub_channel"].tolist()
    window = pd.Timedelta(hours=WINDOW_HOURS)
    episodes, start = [], 0
    for i in range(1, len(times)):
        if times[i] - times[i - 1] > window:
            episodes.append((start, i - 1))
            start = i
    episodes.append((start, len(times) - 1))
    rows = []
    for s, e in episodes:
        rows.append({
            "touches": e - s + 1,
            "elapsed_hours": (times[e] - times[s]).total_seconds() / 3600,
            "first_touch_channel": channels[s],
            "first_touch_sub_channel": sub_channels[s],
            "last_touch_channel": channels[e],
            "last_touch_sub_channel": sub_channels[e],
            "episode_start": times[s],
            "episode_end": times[e],
            "n_distinct_calendar_days": g.loc[s:e, "created_at"].dt.date.nunique(),
            "contacts_df": g.loc[s:e].copy(),
        })
    return rows


def has_cancel_reopen_loop(order_id, status_map):
    rows = status_map.get(order_id)
    if rows is None or len(rows) == 0:
        return False
    rows = rows.sort_values("created_at")
    cancel_times = rows.loc[rows["current_status_id"] == 280, "created_at"].tolist()
    reopen_times = rows.loc[rows["current_status_id"].isin([200, 210]), "created_at"].tolist()
    return any(rt > ct for ct in cancel_times for rt in reopen_times)


def has_duplicate_leg_pair(contacts_df):
    ringg_times = contacts_df.loc[contacts_df["sub_channel"] == "Ringg", "created_at"].tolist()
    obwh_times = contacts_df.loc[
        contacts_df["sub_channel"].isin(["Agent OB WH (completed)", "Agent OB WH (connected)"]),
        "created_at",
    ].tolist()
    if not ringg_times or not obwh_times:
        return False
    window = pd.Timedelta(seconds=DUPLICATE_LEG_SECONDS)
    return any(abs((rt - ot).total_seconds()) <= window.total_seconds() for rt in ringg_times for ot in obwh_times)


def is_channel_handoff(contacts_df):
    channels = contacts_df["channel"].tolist()
    first = channels[0]
    if first != "LimeChat":
        return False
    return any(c in ("Calls", "Email") for c in channels[1:])


def classify_episode(order_id, ep, status_map):
    """Priority-ordered, mutually-exclusive cascade -- first true wins. See plan Section 2 for the
    rationale behind this exact ordering (backend cause first, data-artifact second, handoff third,
    then day-count splits, residual last)."""
    contacts_df = ep["contacts_df"]
    cancel_reopen = has_cancel_reopen_loop(order_id, status_map)
    dup_leg = has_duplicate_leg_pair(contacts_df)
    handoff = is_channel_handoff(contacts_df)

    if cancel_reopen:
        category = "backend_cancel_reopen"
    elif dup_leg:
        category = "duplicate_leg_artifact"
    elif handoff:
        category = "channel_handoff"
    elif ep["n_distinct_calendar_days"] > 1:
        category = "multi_day_followup"
    elif ep["touches"] > 1:
        category = "same_day_multi_call"
    else:
        category = "other_unclassified"

    return category, cancel_reopen, dup_leg


def main():
    dedupable = pd.read_parquet(WINDOW_DIR / "dedupable_raw.parquet")
    dedupable["created_at"] = pd.to_datetime(dedupable["created_at"])
    total_contacts_all = len(dedupable)

    sub = dedupable[dedupable["normalized_sub_disposition"] == TARGET].copy()
    sub = sub.dropna(subset=["final_order_id"])
    sub["final_order_id"] = sub["final_order_id"].astype("int64")
    total_contacts = len(sub)
    total_orders = sub["final_order_id"].nunique()
    ipi = total_contacts / total_orders
    share = total_contacts / total_contacts_all
    print(f"[24] {TARGET}: contacts={total_contacts} orders={total_orders} IPI={ipi:.3f} share={share:.4%}")

    first_touch = sub.sort_values("created_at").groupby("final_order_id").first()
    primary_channel_counts = first_touch["channel"].value_counts()
    primary_subchannel_counts = first_touch["sub_channel"].value_counts()

    status_log = pd.read_parquet(WINDOW_DIR / "quantity_status_log.parquet")
    status_map = {oid: g for oid, g in status_log.groupby("order_id")}
    print(f"[24] loaded status log for {len(status_map)} orders")

    all_episodes = []
    for order_id, g in sub.groupby("final_order_id"):
        for ep in build_episodes(g):
            ep["order_id"] = order_id
            all_episodes.append(ep)
    ep_df = pd.DataFrame(all_episodes)
    n_episodes = len(ep_df)
    fcr = (ep_df["touches"] == 1).mean()
    touch_percentiles = ep_df["touches"].quantile([0.5, 0.75, 0.85, 0.90, 0.95, 0.99, 1.0])
    mean_touches = ep_df["touches"].mean()
    median_all = ep_df["elapsed_hours"].median()
    multi = ep_df[ep_df["touches"] > 1].copy()
    median_multi = multi["elapsed_hours"].median() if len(multi) else float("nan")

    print(f"[24] resolution episodes: {n_episodes}, FCR: {fcr:.2%}")
    print(f"[24] touch percentiles p50/p75/p85/p90/p95/p99/p100: {touch_percentiles.values}")
    print(f"[24] mean touches: {mean_touches:.3f}")
    print(f"[24] median resolution hours ALL={median_all:.1f} MULTI={median_multi:.1f} "
          f"({len(multi)}/{n_episodes} needed 2+ touches)")

    # --- MECE classification of every multi-touch episode ---
    mece_rows = []
    for _, ep in multi.iterrows():
        category, cancel_reopen, dup_leg = classify_episode(ep["order_id"], ep, status_map)
        contacts_df = ep["contacts_df"]
        mece_rows.append({
            "final_order_id": ep["order_id"],
            "episode_start": ep["episode_start"],
            "episode_end": ep["episode_end"],
            "touches": ep["touches"],
            "elapsed_hours": round(ep["elapsed_hours"], 2),
            "n_distinct_calendar_days": ep["n_distinct_calendar_days"],
            "first_touch_channel": ep["first_touch_channel"],
            "first_touch_sub_channel": ep["first_touch_sub_channel"],
            "last_touch_channel": ep["last_touch_channel"],
            "last_touch_sub_channel": ep["last_touch_sub_channel"],
            "mece_category": category,
            "has_cancel_reopen": cancel_reopen,
            "has_duplicate_leg_pair": dup_leg,
            "duplicate_leg_contact_ids": (
                ",".join(contacts_df["contact_id"].dropna().astype(str)) if dup_leg else ""
            ),
        })
    mece_df = pd.DataFrame(mece_rows)

    assert mece_df["mece_category"].notna().all(), "[24] FATAL: every multi-touch episode must get a category"
    assert len(mece_df) == len(multi), "[24] FATAL: MECE table row count must equal multi-touch episode count"
    assert set(mece_df["mece_category"]).issubset(set(MECE_ORDER)), "[24] FATAL: unknown category emitted"

    mece_counts = mece_df["mece_category"].value_counts().reindex(MECE_ORDER).fillna(0).astype(int)
    mece_summary = pd.DataFrame({
        "mece_category": mece_counts.index,
        "n_episodes": mece_counts.values,
        "share_of_multitouch": (mece_counts / len(mece_df)).values if len(mece_df) else 0,
        "share_of_all_episodes": (mece_counts / n_episodes).values,
    })
    print(f"[24] MECE breakdown of {len(mece_df)} multi-touch episodes:\n{mece_summary}")

    monthly = sub.groupby(sub["created_at"].dt.to_period("M")).size()
    print(f"[24] monthly volume:\n{monthly}")

    out_path = PIPELINE_ROOT / "output" / "PRX_MissingOrLessQuantityReceived_DeepDive_2026-05-01_to_2026-09-01.xlsx"
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        summary = pd.DataFrame([
            {"Metric": "Total contacts, ALL dispositions (window)", "Value": total_contacts_all},
            {"Metric": f"Total contacts, {TARGET}", "Value": total_contacts},
            {"Metric": "Share of all contacts", "Value": f"{share:.4%}"},
            {"Metric": "Total distinct orders", "Value": total_orders},
            {"Metric": "IPI (contacts/orders)", "Value": round(ipi, 3)},
            {"Metric": "Primary (first-touch) channel", "Value": primary_channel_counts.idxmax()},
            {"Metric": "Primary channel share of orders", "Value": f"{primary_channel_counts.max()/total_orders:.1%}"},
            {"Metric": "Resolution episodes (n)", "Value": n_episodes},
            {"Metric": "Mean touches", "Value": round(mean_touches, 3)},
            {"Metric": "FCR (order level, 1-touch)", "Value": f"{fcr:.2%}"},
            {"Metric": "p50 touches", "Value": touch_percentiles[0.5]},
            {"Metric": "p75 touches", "Value": touch_percentiles[0.75]},
            {"Metric": "p85 touches", "Value": touch_percentiles[0.85]},
            {"Metric": "p90 touches", "Value": touch_percentiles[0.90]},
            {"Metric": "p95 touches", "Value": touch_percentiles[0.95]},
            {"Metric": "p99 touches", "Value": touch_percentiles[0.99]},
            {"Metric": "p100 (max) touches", "Value": touch_percentiles[1.0]},
            {"Metric": "Median resolution hours, ALL episodes", "Value": round(median_all, 1)},
            {"Metric": "Median resolution hours, MULTI-touch only", "Value": round(median_multi, 1)},
            {"Metric": "Share of episodes needing 2+ touches", "Value": f"{len(multi)/n_episodes:.2%}"},
        ])
        summary.to_excel(writer, sheet_name="Summary", index=False)
        autosize_and_freeze(writer, "Summary", summary)
        style_header(writer, "Summary", len(summary.columns))

        chan = pd.DataFrame({
            "Channel": primary_channel_counts.index,
            "Orders (first touch)": primary_channel_counts.values,
            "Share": (primary_channel_counts / total_orders).values,
        })
        chan.to_excel(writer, sheet_name="Primary Channel", index=False)
        ws = writer.sheets["Primary Channel"]
        for r in range(2, len(chan) + 2):
            ws.cell(row=r, column=3).number_format = "0.00%"
        autosize_and_freeze(writer, "Primary Channel", chan)
        style_header(writer, "Primary Channel", len(chan.columns))

        monthly_df = monthly.reset_index()
        monthly_df.columns = ["Month", "Contacts"]
        monthly_df["Month"] = monthly_df["Month"].astype(str)
        monthly_df.to_excel(writer, sheet_name="Monthly Trend", index=False)
        autosize_and_freeze(writer, "Monthly Trend", monthly_df)
        style_header(writer, "Monthly Trend", len(monthly_df.columns))

        ep_out = ep_df.sort_values("touches", ascending=False)[
            ["order_id", "touches", "elapsed_hours", "first_touch_channel",
             "first_touch_sub_channel", "episode_start", "episode_end"]
        ]
        ep_out.to_excel(writer, sheet_name="Resolution Episodes", index=False)
        autosize_and_freeze(writer, "Resolution Episodes", ep_out)
        style_header(writer, "Resolution Episodes", len(ep_out.columns))

        mece_out = mece_df.sort_values("touches", ascending=False)
        mece_out.to_excel(writer, sheet_name="MECE Classification", index=False)
        autosize_and_freeze(writer, "MECE Classification", mece_out)
        style_header(writer, "MECE Classification", len(mece_out.columns))

        mece_summary_fmt = mece_summary.copy()
        mece_summary_fmt["share_of_multitouch"] = mece_summary_fmt["share_of_multitouch"].map(lambda x: f"{x:.2%}")
        mece_summary_fmt["share_of_all_episodes"] = mece_summary_fmt["share_of_all_episodes"].map(lambda x: f"{x:.2%}")
        mece_summary_fmt.to_excel(writer, sheet_name="MECE Summary", index=False)
        autosize_and_freeze(writer, "MECE Summary", mece_summary_fmt)
        style_header(writer, "MECE Summary", len(mece_summary_fmt.columns))

    print(f"[24] wrote {out_path}")


if __name__ == "__main__":
    main()
