"""Stage 33: Export the single-tab dashboard's data as tidy long-format CSVs (no PII, aggregates only).

The Google Sheet "single tab" dashboard is built by an Apps Script (apps_script/Code.gs) that pulls
these files from the GitHub repo. This stage only emits BASE counts / non-additive stats; every
derived ratio (IPO, IPI, CPO, % rows, FCR/BFCR/HFCR, share, IPO/IPI by sub-disposition) is a live
spreadsheet formula over the lettered rows, so the sheet shows its own arithmetic.

Numbers reuse Stage 17 verbatim (build_summary_table / contacts_to_resolution), so the sheet ties to
the slim dashboard xlsx by construction.

Reads:  same inputs as Stage 17
Writes: export/dashboard_long.csv   one row per (section, key, month)
        export/meta.json            run range, version, generated_at, caveats
"""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from pipeline_utils import load_config, PIPELINE_ROOT

_spec = importlib.util.spec_from_file_location("slim_dashboard", Path(__file__).parent / "17_slim_dashboard.py")
slim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(slim)

EXPORT_DIR = PIPELINE_ROOT / "export"
BUCKETS = ["1", "2", "3", "4+"]
STAT_KEYS = ["Total keys", "Mean", "Median (P50)", "P75", "P90", "P95", "P99", "P99.9", "Max (tail)"]


def per_key_events(df, window_hours, group_cols, bot_channels=("Ringg", "LimeChat Bot")):
    """One row per key: touches in its FIRST resolution episode + whether the whole journey was bot-only.
    Identical logic to slim._resolution_cut_by_key, but keeps the month so we can aggregate per month."""
    rows = []
    for key, g in df.groupby(group_cols):
        touches = slim.contacts_to_resolution(g, window_hours)[0]
        bot_only = bool(g["sub_channel"].isin(list(bot_channels)).all())
        rows.append((key[-1], touches, bot_only))      # month is always the last group col
    return pd.DataFrame(rows, columns=["month", "touches", "bot_only"])


def resolution_long(events, section):
    out = []
    for month, g in list(events.groupby("month")) + [("Total", events)]:
        b = g["touches"].clip(upper=4).astype(int).astype(str).where(g["touches"] < 4, "4+")
        for bucket in BUCKETS:
            sel = g[b == bucket]
            out.append((section, f"b{bucket}_total", month, len(sel)))
            out.append((section, f"b{bucket}_bot", month, int(sel["bot_only"].sum())))
        t = g["touches"]
        stats = {"Total keys": len(g), "Mean": round(float(t.mean()), 4), "Median (P50)": float(t.median()),
                 "P75": float(t.quantile(.75)), "P90": float(t.quantile(.90)), "P95": float(t.quantile(.95)),
                 "P99": float(t.quantile(.99)), "P99.9": float(t.quantile(.999)), "Max (tail)": float(t.max())}
        for k, v in stats.items():
            out.append((section, f"stat_{k}", month, v))
    return out


def main():
    cfg = load_config()
    dedupable, primaries, resolution_order, fallback_orders, orders_denom = slim.load_data(cfg)
    window_hours = cfg.get("resolution", {}).get("window_hours", cfg["run"].get("window_hours", 48)) \
        if isinstance(cfg.get("resolution", {}), dict) else 48

    long = []   # (section, key, month, value)

    # ---- Summary base rows + channel volumes (reuse Stage 17's table so numbers are identical) ----
    summary, months_asc = slim.build_summary_table(dedupable, primaries, resolution_order, fallback_orders, orders_denom)
    label_to_month = {slim.month_label(m): m for m in months_asc}
    base_labels = {
        "Total Orders": "orders",
        "Total Orders w Support Contact": "orders_w_contact",
        "Total Orders w Support Contact - Actionable": "orders_w_contact_actionable",
        "Total Unique Users (all orders)": "users",
        "Total Unique Users w/ Support Contact": "users_w_contact",
        "Total Contacts Made (raw, support-only, sales stripped)": "contacts_raw",
        "Total Contacts After De-dup (new logic)": "contacts_dedup",
    }
    sections = {"base": 0, "before": 0, "after": 0}
    current = "base"
    for r in summary.itertuples(index=False):
        metric = r[0]
        if isinstance(metric, str) and metric.startswith("Contact Volume by Channel -- Before"):
            current = "before"; continue
        if isinstance(metric, str) and metric.startswith("Contact Volume by Channel -- After"):
            current = "after"; continue
        if current == "base" and metric in base_labels:
            for col, val in zip(summary.columns[1:], r[1:]):
                m = label_to_month.get(col, "Total")
                long.append(("summary", base_labels[metric], m, float(val)))
        elif current in ("before", "after") and isinstance(metric, str) and metric:
            for col, val in zip(summary.columns[1:], r[1:]):
                m = label_to_month.get(col, "Total")
                long.append((f"channel_{current}", metric, m, float(val)))

    # ---- Contacts to resolution: cut 1 (Order ID) and cut 2 (Order ID x Sub-disposition, Actionable) ----
    ev1 = per_key_events(dedupable, window_hours, ["final_order_id", "month"])
    long += resolution_long(ev1, "res_order")
    cut2 = dedupable[dedupable["is_actionable"]]
    ev2 = per_key_events(cut2, window_hours, ["final_order_id", "normalized_sub_disposition", "month"])
    long += resolution_long(ev2, "res_order_sub")

    # ---- Disposition / sub-disposition breakdown: contacts before dedup, issues after dedup ----
    for sec, frame in (("disp_before", dedupable), ("disp_after", primaries)):
        g = frame.groupby(["normalized_disposition", "normalized_sub_disposition", "month"]).size()
        for (d, s, m), v in g.items():
            long.append((sec, f"{d}|{s}", m, int(v)))
        tot = frame.groupby(["normalized_disposition", "normalized_sub_disposition"]).size()
        for (d, s), v in tot.items():
            long.append((sec, f"{d}|{s}", "Total", int(v)))

    df = pd.DataFrame(long, columns=["section", "key", "month", "value"])
    EXPORT_DIR.mkdir(exist_ok=True)
    df.to_csv(EXPORT_DIR / "dashboard_long.csv", index=False)

    # ---- labels (display names + sort order) so the Apps Script has no naming logic of its own ----
    from pipeline_utils import normalize_disposition_value as nv
    master = pd.read_csv(PIPELINE_ROOT / "reference" / "dispositions_master.csv")
    disp_label = {nv(d): d for d in master["disposition"].unique()}
    disp_label["not_disposed"] = "Not Disposed"
    sub_label = {(nv(r.disposition), r.sub_disposition_normalized): r.sub_disposition for r in master.itertuples()}
    tot = df[(df.section == "disp_before") & (df.month == "Total")].copy()
    tot[["disp_key", "sub_key"]] = tot["key"].str.split("|", expand=True)
    d_rank = tot.groupby("disp_key")["value"].sum().rank(ascending=False, method="first").astype(int)
    tot["disp_rank"] = tot["disp_key"].map(d_rank)
    tot = tot.sort_values(["disp_rank", "value"], ascending=[True, False])
    tot["disp_label"] = tot["disp_key"].map(lambda k: disp_label.get(k, k.replace("_", " ").title()))
    tot["sub_label"] = [sub_label.get((d, s), "(no sub-disposition)" if s == "not_disposed" and d != "not_disposed" else s.replace("_", " "))
                        for d, s in zip(tot["disp_key"], tot["sub_key"])]
    tot[["key", "disp_key", "disp_label", "sub_key", "sub_label"]].to_csv(EXPORT_DIR / "labels.csv", index=False)

    meta = {
        "version_tag": cfg["run"]["version_tag"],
        "range_start": str(cfg["run"]["start_date"]),
        "range_end_exclusive": str(cfg["run"]["end_date"]),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "months": months_asc,
        "channels": slim.CHANNEL_ORDER,
        "caveats": [
            "Latest month is right-censored: orders placed late in the month have not generated all their contacts yet, so its IPO/CPO read low.",
            "Zoho (Email) coverage ends 2026-08-25; Email is 0 afterwards.",
            "Only order-linked Support contacts are counted (contacts with no Order ID are excluded from CPO).",
        ],
    }
    (EXPORT_DIR / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"[33] wrote {len(df)} rows -> {EXPORT_DIR / 'dashboard_long.csv'}")
    print(df.groupby("section").size().to_string())
    # tie-out checks
    s = df[df.section == "summary"].pivot(index="key", columns="month", values="value")
    for sec in ("channel_before", "channel_after"):
        chan = df[df.section == sec].groupby("month")["value"].sum()
        ref = s.loc["contacts_raw" if sec == "channel_before" else "contacts_dedup"]
        print(f"[33] {sec} sum vs summary: max abs diff = {(chan - ref).abs().max():.0f}")
    t = df[(df.section == "res_order") & (df.month == "Total")].set_index("key")["value"]
    print("[33] res_order buckets sum", int(sum(t[f'b{b}_total'] for b in BUCKETS)), "vs Total keys", int(t["stat_Total keys"]),
          "| orders_w_contact total", int(s.loc["orders_w_contact", "Total"]))


if __name__ == "__main__":
    main()
