"""Stage 05: Calls leg classification, disposition extraction, order-ID attribution.

This is the piece that had NO prior working code (per the project brief) — newly built here.
Confirmed live against ClickHouse (see README_clickhouse_queries.md): Ringg leg = lower(operator)=
'ringg' and lower(department_name)='support'; Agent Inbound = menu_extension in ('1,2','2,2');
Agent Outbound = crm_tasks.name='Missed Call Support', joined via call_id.

Reads:  intermediate/.../calls_ringg_raw.parquet
        intermediate/.../calls_agent_inbound_raw.parquet
        intermediate/.../calls_agent_outbound_raw.parquet
        intermediate/.../fallback_orders.parquet
Writes: intermediate/.../calls_classified.parquet
"""
import pandas as pd
from pipeline_utils import (
    load_config, intermediate_dir, normalize_disposition_value, normalize_phone,
    extract_single_numeric_id, print_join_match_rate, SALES_DISPOSITION_KEYS,
)


def fallback_join(df, fallback, window_days, ts_col):
    """20-day-lookback phone-based fallback order match: fallback order's created_at must be
    strictly earlier than the contact timestamp, most recent eligible order wins.
    Vectorized via merge_asof (grouped by phone) instead of a per-row python loop -- the naive
    loop took >10 minutes on ~250k rows against a 920k-row fallback table and was killed.
    """
    df = df.copy()
    df["_row_id"] = range(len(df))
    valid = df.dropna(subset=["phone_normalized", ts_col]).copy()
    if valid.empty:
        df["order_id_fallback"] = None
        df["order_created_at_fallback"] = None
        return df.drop(columns=["_row_id"])

    fb = fallback.dropna(subset=["phone_normalized"])[["phone_normalized", "order_id", "created_at"]].copy()
    fb = fb.rename(columns={"order_id": "fb_order_id", "created_at": "fb_created_at"})
    fb = fb.sort_values("fb_created_at")
    valid_sorted = valid[["_row_id", "phone_normalized", ts_col]].sort_values(ts_col)

    merged = pd.merge_asof(
        valid_sorted, fb, left_on=ts_col, right_on="fb_created_at", by="phone_normalized",
        direction="backward", allow_exact_matches=False,
    )
    within_window = (merged[ts_col] - merged["fb_created_at"]) <= pd.Timedelta(days=window_days)
    merged["order_id_fallback"] = merged["fb_order_id"].where(within_window)
    merged["order_created_at_fallback"] = merged["fb_created_at"].where(within_window)

    result = df.merge(
        merged[["_row_id", "order_id_fallback", "order_created_at_fallback"]], on="_row_id", how="left",
    )
    return result.drop(columns=["_row_id"])


def main():
    cfg = load_config()
    out_dir = intermediate_dir(cfg)
    window_days = cfg["fallback_join"]["window_days"]

    fallback = pd.read_parquet(out_dir / "fallback_orders.parquet")
    fallback["phone_normalized"] = fallback["phone_number"].map(normalize_phone)

    # --- Ringg (bot) ---
    ringg = pd.read_parquet(out_dir / "calls_ringg_raw.parquet")
    ringg["created_at_dt"] = pd.to_datetime(ringg["created_at"], errors="coerce")
    ringg["phone_normalized"] = ringg["customer_phone_number"].map(normalize_phone)
    ringg["order_id_direct"] = ringg["meta_order_id"].map(extract_single_numeric_id)
    ringg["normalized_disposition"] = ringg["meta_disposition"].map(normalize_disposition_value)
    ringg["normalized_sub_disposition"] = ringg["meta_sub_disposition"].map(normalize_disposition_value)
    ringg["leg"] = "Ringg"
    ringg["sub_channel"] = "Ringg"
    # department_name='Support' was already filtered at extraction, but the bot's actual
    # conversation can still land on a Sales-category disposition (confirmed: 94 rows) -- classify
    # by the real disposition rather than trusting the extraction-time department filter alone.
    ringg["sales_or_support"] = ringg["normalized_disposition"].map(
        lambda d: "Sales" if d in SALES_DISPOSITION_KEYS else "Support"
    )

    # --- Agent Inbound ---
    inbound = pd.read_parquet(out_dir / "calls_agent_inbound_raw.parquet")
    inbound["created_at_dt"] = pd.to_datetime(inbound["created_at"], errors="coerce")
    inbound["phone_normalized"] = inbound["customer_phone_number"].map(normalize_phone)
    inbound["leg"] = "Agent IB"
    inbound["sub_channel"] = "Agent IB"

    # Real dispositions, joined call_id -> crm_task_call_logs -> task_id -> crm_task_dispositions
    # (previously this leg was hardcoded to "not_disposed" entirely -- a real gap, not a genuine
    # business signal, that was found to be dragging the "actionable orders" metric down since
    # Agent IB is ~35% of all deduped contacts). Real dispositions only exist from 2026-05-12
    # onward -- looks like a system rollout date, not a query bug (confirmed via a live spot-check).
    ib_disp_path = out_dir / "calls_agent_inbound_dispositions.parquet"
    if ib_disp_path.exists():
        disp = pd.read_parquet(ib_disp_path)
        # One call can link to several CRM tasks ("sister tasks"): 53k of 216k calls in the 2026-10-06
        # run. The first task is usually a generic hand-off marker ("Sales Related - Created a New
        # Task" is the first task on 75% of them) and a LATER task carries the real outcome. The
        # previous version sorted by call_created_at, but that is the CALL's timestamp, identical for
        # every task of one call -- so the pick was arbitrary (and shifted run to run whenever rows
        # were appended). Deterministic rule: prefer a task carrying a real disposition, then the
        # latest-created task (highest task_id).
        before = len(disp)
        disp["_has_disp"] = disp["disposition"].replace("", pd.NA).notna()
        disp = (disp.sort_values(["call_log_id", "_has_disp", "task_id"])
                    .drop_duplicates(subset="call_log_id", keep="last"))
        print(f"[05] agent_inbound_dispositions: {before} rows -> {len(disp)} unique calls "
              f"(sister tasks resolved: prefer real disposition, then latest task_id)")
        disp = disp.rename(columns={
            "disposition": "ib_disposition", "sub_disposition": "ib_sub_disposition",
            "meta_order_id": "ib_meta_order_id",
        })
        inbound = inbound.merge(
            disp[["call_log_id", "ib_disposition", "ib_sub_disposition", "ib_meta_order_id"]],
            left_on="id", right_on="call_log_id", how="left",
        )
        match_rate = inbound["ib_disposition"].notna().mean()
        print(f"[05] Agent IB disposition join match rate: {match_rate:.1%} "
              f"(rows before 2026-05-12 have no disposition by design -- system rollout date)")
        inbound["order_id_direct"] = inbound["ib_meta_order_id"].map(extract_single_numeric_id)
        inbound["normalized_disposition"] = inbound["ib_disposition"].map(normalize_disposition_value)
        inbound["normalized_sub_disposition"] = inbound["ib_sub_disposition"].map(normalize_disposition_value)
    else:
        print("[05] WARNING: no Agent Inbound disposition file found -- leg will be 100% not_disposed")
        inbound["order_id_direct"] = None
        inbound["normalized_disposition"] = "not_disposed"
        inbound["normalized_sub_disposition"] = "not_disposed"

    # Agent IB's menu_extension filter is meant to isolate the Support menu path, but a caller can
    # still steer the conversation to a Sales topic -- classify by the real disposition, not the
    # menu-routing assumption (previously ALL Agent IB rows were hardcoded "Support", contaminating
    # the Support universe with genuine Sales-category dispositions).
    inbound["sales_or_support"] = inbound["normalized_disposition"].map(
        lambda d: "Sales" if d in SALES_DISPOSITION_KEYS else "Support"
    )

    # --- Agent Outbound ---
    outbound = pd.read_parquet(out_dir / "calls_agent_outbound_raw.parquet")
    # Blank-timestamp fallback per the brief: use task-level created_at, not a call-log timestamp.
    outbound["created_at_dt"] = pd.to_datetime(outbound["task_created_at"], errors="coerce")

    # Companion "In Call" task disposition recovery: when a Missed Call Support task connects, the
    # agent's real disposition often lands on a SEPARATE "In Call" task created at that moment, not
    # the original task's own id. Prefer the task's own disposition; fall back to the companion's.
    # Remember which tasks carry a disposition of their OWN (before companion recovery overwrites
    # it), and attach crm_tasks.status -- both needed to spot auto-closed "ghost" tasks below.
    outbound["_own_disp"] = outbound["disposition"].notna()
    ob_status_path = out_dir / "calls_agent_outbound_task_status.parquet"
    if ob_status_path.exists():
        outbound = outbound.merge(
            pd.read_parquet(ob_status_path)[["task_id", "task_status"]], on="task_id", how="left")
    else:
        outbound["task_status"] = pd.NA
    ob_companion_path = out_dir / "calls_agent_outbound_companion.parquet"
    if ob_companion_path.exists():
        companion = pd.read_parquet(ob_companion_path)
        before_disp = outbound["disposition"].notna().sum()
        outbound = outbound.merge(companion, on="task_id", how="left")
        outbound["disposition"] = outbound["disposition"].combine_first(outbound["companion_disposition"])
        outbound["sub_disposition"] = outbound["sub_disposition"].combine_first(outbound["companion_sub_disposition"])
        outbound["meta_order_id"] = outbound["meta_order_id"].combine_first(outbound["companion_meta_order_id"])
        after_disp = outbound["disposition"].notna().sum()
        print(f"[05] Agent Outbound companion-task recovery: disposition coverage {before_disp} -> "
              f"{after_disp} rows (+{after_disp - before_disp} recovered)")
    else:
        print("[05] WARNING: no Agent Outbound companion-disposition file found -- skipping recovery")

    outbound["order_id_direct"] = outbound["meta_order_id"].map(extract_single_numeric_id)
    outbound["normalized_disposition"] = outbound["disposition"].map(normalize_disposition_value)
    outbound["normalized_sub_disposition"] = outbound["sub_disposition"].map(normalize_disposition_value)
    # Missed Call Support tasks (and their recovered companion dispositions) can still turn out to
    # be Sales conversations -- classify by the real disposition, not a blanket "Support" label
    # (previously ALL Agent Outbound rows were hardcoded "Support").
    outbound["sales_or_support"] = outbound["normalized_disposition"].map(
        lambda d: "Sales" if d in SALES_DISPOSITION_KEYS else "Support"
    )
    outbound["phone_normalized"] = None  # no phone column on crm_tasks; fallback done via user_id below
    # Office-hours window filter (see run_config.yaml's timezone caveat -- OPEN ITEM, applied as-is).
    hh_start = cfg["calls"]["agent_outbound_office_hours_start"]
    hh_end = cfg["calls"]["agent_outbound_office_hours_end"]
    t = outbound["created_at_dt"].dt.time
    in_hours = (t >= pd.Timestamp(hh_start).time()) & (t <= pd.Timestamp(hh_end).time())
    outbound["in_office_hours"] = in_hours
    # connected vs completed are OVERLAPPING subsets per the brief -- kept as independent boolean
    # flags (used by the Tab-1 channel report) rather than an exclusive bucket. "completed" now also
    # counts a recovered companion-task disposition as evidence the call was actually worked.
    outbound["is_connected"] = outbound["call_id"].notna()
    outbound["is_completed"] = outbound["disposition_status"].notna() | outbound["disposition"].notna()
    # For the single-leg-per-row combine/dedup table only, pick one exclusive tag by priority
    # (connected > completed > other) so each contact still lands in exactly one dedup bucket.
    outbound["leg"] = outbound.apply(
        lambda r: "Agent OB WH (connected)" if r["is_connected"] else "Agent OB WH (completed)"
        if r["is_completed"] else "Agent OB WH (other)", axis=1,
    )
    outbound["sub_channel"] = outbound["leg"]

    # Fallback order-ID join for legs with a usable phone number.
    ringg = fallback_join(ringg, fallback, window_days, "created_at_dt")
    inbound = fallback_join(inbound, fallback, window_days, "created_at_dt")

    # Agent Outbound has no phone column at all (crm_tasks only has user_id) -- fall back to a
    # user_id-based 20-day join against the same fallback_orders table instead of phone.
    fb_by_user = fallback.dropna(subset=["user_id"])[["user_id", "order_id", "created_at"]].rename(
        columns={"order_id": "fb_order_id", "created_at": "fb_created_at"}
    ).sort_values("fb_created_at")
    outbound_valid = outbound.dropna(subset=["user_id", "created_at_dt"]).copy()
    outbound_valid["user_id"] = outbound_valid["user_id"].astype("Int64")
    outbound_valid["_row_id"] = outbound_valid.index
    outbound_sorted = outbound_valid[["_row_id", "user_id", "created_at_dt"]].sort_values("created_at_dt")
    ob_merged = pd.merge_asof(
        outbound_sorted, fb_by_user, left_on="created_at_dt", right_on="fb_created_at", by="user_id",
        direction="backward", allow_exact_matches=False,
    )
    ob_within = (ob_merged["created_at_dt"] - ob_merged["fb_created_at"]) <= pd.Timedelta(days=window_days)
    ob_merged["order_id_fallback"] = ob_merged["fb_order_id"].where(ob_within)
    outbound = outbound.merge(
        ob_merged[["_row_id", "order_id_fallback"]].rename(columns={"_row_id": "_idx"}),
        left_index=True, right_on="_idx", how="left",
    ).drop(columns=["_idx"])

    common_cols = ["leg", "sub_channel", "sales_or_support", "created_at_dt", "phone_normalized",
                   "order_id_direct", "normalized_disposition", "normalized_sub_disposition"]
    for df, name in [(ringg, "ringg"), (inbound, "agent_inbound")]:
        df["final_order_id"] = df["order_id_direct"].combine_first(df.get("order_id_fallback"))
        direct_fill = df["order_id_direct"].notna().mean() if len(df) else 0
        final_fill = df["final_order_id"].notna().mean() if len(df) else 0
        print(f"[05] {name}: {len(df)} rows, direct order-id fill={direct_fill:.1%}, "
              f"after 20-day fallback={final_fill:.1%}")

    outbound["final_order_id"] = outbound["order_id_direct"].combine_first(outbound["order_id_fallback"])
    direct_fill = outbound["order_id_direct"].notna().mean() if len(outbound) else 0
    final_fill = outbound["final_order_id"].notna().mean() if len(outbound) else 0
    print(f"[05] agent_outbound: {len(outbound)} rows, direct order-id fill={direct_fill:.1%}, "
          f"after user_id-based 20-day fallback={final_fill:.1%}")
    # --- Ghost "sister" tasks (PROJECT_METHODOLOGY_AND_LEARNINGS.md 6c) -----------------------------
    # crm_tasks.status=20 ("Connected") tasks are genuinely agent-worked: 100% carry their own
    # disposition. status=60/50 tasks are auto-closed placeholders created within seconds of a Ringg
    # call (median ~37s to close, ~1% carry a disposition of their own). The companion-recovery
    # merge above still finds a disposition for most of them, which mirrors the Ringg call -- so one
    # Ringg call showed up twice (Ringg + an "Agent OB WH (completed)" row), inflating raw contacts
    # and IPI. Drop a non-status-20 task with no disposition of its own when a Ringg contact exists
    # on the same Final Order ID within GHOST_PAIR_WINDOW. Unpaired non-status-20 tasks are left
    # exactly as before (not enough evidence they duplicate anything).
    outbound = outbound.reset_index(drop=True)  # unique positional index so drops below are exact
    if outbound["task_status"].notna().any():
        cand = (outbound["task_status"].notna() & (outbound["task_status"] != 20)
                & ~outbound["_own_disp"] & outbound["final_order_id"].notna())
        ob_c = outbound.loc[cand, ["final_order_id", "created_at_dt"]].copy()
        ob_c["_k"] = ob_c.index
        ob_c["final_order_id"] = ob_c["final_order_id"].astype("float64")
        rg = ringg.loc[ringg["final_order_id"].notna(), ["final_order_id", "created_at_dt"]].copy()
        rg["final_order_id"] = rg["final_order_id"].astype("float64")
        rg["_ringg_t"] = rg["created_at_dt"]
        near = pd.merge_asof(
            ob_c.sort_values("created_at_dt"), rg.sort_values("created_at_dt"),
            on="created_at_dt", by="final_order_id", direction="nearest",
            tolerance=pd.Timedelta(hours=24),
        )
        near["gap_s"] = (near["created_at_dt"] - near["_ringg_t"]).abs().dt.total_seconds()
        gaps = near["gap_s"].dropna()
        print(f"[05] ghost-task pairing: {int(cand.sum())} candidate Agent OB tasks "
              f"(status!=20, no own disposition, has Order ID); nearest same-order Ringg gap: "
              f"<=1min {int((gaps<=60).sum())}, <=5min {int((gaps<=300).sum())}, "
              f"<=15min {int((gaps<=900).sum())}, <=60min {int((gaps<=3600).sum())}, "
              f"<=24h {len(gaps)}")
        GHOST_PAIR_WINDOW_S = 900  # 15 minutes
        drop_idx = near.loc[near["gap_s"] <= GHOST_PAIR_WINDOW_S, "_k"]
        n_before = len(outbound)
        was_completed = outbound.loc[drop_idx, "is_completed"].sum()
        outbound = outbound.drop(index=drop_idx)
        print(f"[05] dropped {len(drop_idx)} ghost Agent OB tasks (paired to a Ringg contact within "
              f"15min on the same order; {int(was_completed)} of them had been counted as "
              f"'completed' via companion recovery): {n_before} -> {len(outbound)} Agent OB rows")
    else:
        print("[05] WARNING: no task_status available -- ghost-task filtering skipped")

    print(f"[05] agent_outbound leg split (connected/completed overlap, not summed):\n"
          f"{outbound['leg'].value_counts()}")
    print(f"[05] agent_outbound true overlap counts: is_connected={outbound['is_connected'].sum()}, "
          f"is_completed={outbound['is_completed'].sum()}, in_office_hours={outbound['in_office_hours'].sum()}")

    for df in (ringg, inbound):
        df["is_connected"] = pd.NA
        df["is_completed"] = pd.NA

    keep_cols = ["leg", "sub_channel", "sales_or_support", "created_at_dt", "phone_normalized",
                 "order_id_direct", "final_order_id", "normalized_disposition", "normalized_sub_disposition",
                 "is_connected", "is_completed"]
    combined = pd.concat([
        ringg[keep_cols], inbound[keep_cols], outbound[keep_cols],
    ], ignore_index=True)
    combined["channel"] = "Calls"

    combined.to_parquet(out_dir / "calls_classified.parquet", index=False)
    print(f"[05] wrote {len(combined)} total Calls rows -> {out_dir / 'calls_classified.parquet'}")
    print(f"[05] leg distribution:\n{combined['leg'].value_counts()}")


if __name__ == "__main__":
    main()
