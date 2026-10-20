"""Stage 00: validate the ClickHouse extracts that were pulled for this run.

IMPORTANT: ClickHouse access in this project is READ-ONLY via an MCP tool inside a Claude session
(no standalone DB credentials were issued). That means the "pull fresh from ClickHouse every run"
step is NOT a cron-able script — it's a set of SELECT queries run by Claude via MCP each time the
pipeline needs refreshing, saved as flat CSVs into raw/calls/ and raw/orders/. This script's job is
only to validate that those extracts exist, cover the configured run range, and look sane — the
actual extraction queries are documented in pipeline/README_clickhouse_queries.md so they can be
re-run identically next time.

Reads:  raw/orders/orders_denominator.csv
        raw/orders/fallback_orders.csv
        raw/calls/ringg_support_calls.csv
        raw/calls/agent_inbound_calls.csv
        raw/calls/agent_outbound_calls.csv
Writes: intermediate/run_.../ (copies as parquet for the rest of the pipeline to consume)
"""
import sys
import pandas as pd
from pipeline_utils import load_config, intermediate_dir, PIPELINE_ROOT


def main():
    cfg = load_config()
    raw_orders = PIPELINE_ROOT / "raw" / "orders"
    raw_calls = PIPELINE_ROOT / "raw" / "calls"
    out_dir = intermediate_dir(cfg)

    start = pd.Timestamp(cfg["run"]["start_date"])
    end = pd.Timestamp(cfg["run"]["end_date"])

    orders_denom = pd.read_csv(raw_orders / "orders_denominator.csv", parse_dates=["order_date"])
    covered = orders_denom["order_date"].between(start, end - pd.Timedelta(days=1))
    print(f"[00] orders_denominator.csv: {len(orders_denom)} rows, "
          f"{orders_denom['order_date'].min().date()} to {orders_denom['order_date'].max().date()}, "
          f"{covered.sum()} rows within configured run range")
    if orders_denom["order_date"].min() > start or orders_denom["order_date"].max() < (end - pd.Timedelta(days=1)):
        print(f"[00] WARNING: orders_denominator.csv does not fully cover the configured run range "
              f"{start.date()}..{(end - pd.Timedelta(days=1)).date()}")
    orders_denom.to_parquet(out_dir / "orders_denominator.parquet", index=False)

    fallback = pd.read_csv(raw_orders / "fallback_orders.csv", dtype={"order_id": "Int64", "user_id": "Int64"})
    fallback["created_at"] = pd.to_datetime(fallback["created_at"], errors="coerce")
    dupe_orders = fallback["order_id"].duplicated().sum()
    print(f"[00] fallback_orders.csv: {len(fallback)} rows, {fallback['order_id'].nunique()} distinct order_id "
          f"({dupe_orders} duplicate order_id rows, dropped), "
          f"{fallback['created_at'].min()} to {fallback['created_at'].max()}")
    fallback = fallback.drop_duplicates(subset="order_id", keep="first")
    fallback.to_parquet(out_dir / "fallback_orders.parquet", index=False)

    for name, fname in [
        ("ringg", "ringg_support_calls.csv"),
        ("agent_inbound", "agent_inbound_calls.csv"),
        ("agent_outbound", "agent_outbound_calls.csv"),
    ]:
        path = raw_calls / fname
        if not path.exists():
            sys.exit(f"[00] FATAL: missing Calls extract {path} — re-pull via ClickHouse MCP first")
        df = pd.read_csv(path)
        # Object-dtype columns of numeric-looking strings (e.g. customer_phone_number) can trip
        # pyarrow's schema inference when a multi-chunk-appended CSV mixes representations across
        # ingest batches -- confirmed 2026-09-29 (September append): to_parquet raised
        # "Could not convert '7889369226' with type str: tried to convert to int64" even though
        # every value was already a Python str. Force object columns to pandas' nullable string
        # dtype so pyarrow never tries to guess int64 for them; harmless here since every
        # downstream consumer of these columns (e.g. normalize_phone) does str(value) anyway.
        for col in df.select_dtypes(include="object").columns:
            df[col] = df[col].astype("string")
        print(f"[00] {fname}: {len(df)} rows")
        df.to_parquet(out_dir / f"calls_{name}_raw.parquet", index=False)

    # Agent Inbound dispositions (joined call_id -> crm_task_call_logs -> task_id ->
    # crm_task_dispositions; see README amendment) -- optional file, only exists once pulled.
    ib_disp_path = raw_calls / "agent_inbound_dispositions.csv"
    if ib_disp_path.exists():
        df = pd.read_csv(ib_disp_path)
        df = df.rename(columns={"l.call_id": "call_id", "tcl.task_id": "task_id"})
        print(f"[00] agent_inbound_dispositions.csv: {len(df)} rows, "
              f"{df['call_log_id'].nunique()} distinct calls")
        df.to_parquet(out_dir / "calls_agent_inbound_dispositions.parquet", index=False)
    else:
        print(f"[00] NOTE: {ib_disp_path} not found -- Agent Inbound will have no real disposition "
              f"data this run (falls back to 'not_disposed' for the whole leg).")

    # Agent Outbound companion task dispositions -- when a Missed Call Support task connects, the
    # agent's real disposition often lands on a separate task created at that moment, not on the
    # original task's own id. CONFIRMED (2026-09-03 fix): PlatinumRx's CRM uses TWO valid names for
    # that companion task, "In Call" AND "In Call Support" -- the extraction now searches both (see
    # README's "Agent Outbound Companion Task Recovery" section). Missing the second name previously
    # left ~19,359 real dispositions unrecoverable, misreported as "not disposed."
    ob_companion_path = raw_calls / "agent_outbound_companion_dispositions.csv"
    if ob_companion_path.exists():
        df = pd.read_csv(ob_companion_path)
        # ClickHouse returns '' (not NULL) for a non-nullable String column with no real value --
        # confirmed via a companion task that has a disposition ROW but an empty disposition TEXT.
        # Must be cleaned to NaN explicitly here, not left to .notna() alone, or this exact class of
        # "looks recovered but isn't" bug can silently reappear on a future extract.
        for col in ["companion_disposition", "companion_sub_disposition", "companion_meta_order_id"]:
            df[col] = df[col].replace("", pd.NA)
        before = len(df)
        # ClickHouse's row_number()-based nearest-match can still fan out to >1 identical row per
        # task_id -- collapse before merging so Stage 05's per-task merge doesn't inflate the Agent
        # Outbound row count.
        df = df.drop_duplicates(subset="task_id", keep="first")
        print(f"[00] agent_outbound_companion_dispositions.csv: {before} rows -> {len(df)} "
              f"distinct task_id ({df['companion_disposition'].notna().sum()} with a real disposition)")
        df.to_parquet(out_dir / "calls_agent_outbound_companion.parquet", index=False)
    else:
        print(f"[00] NOTE: {ob_companion_path} not found -- Agent Outbound companion-task "
              f"disposition recovery skipped this run.")

    # Agent Outbound task status (crm_tasks.status) -- used by 05_load_calls.py to identify
    # auto-closed "ghost" Missed Call Support tasks (status != 20) whose companion-recovered
    # disposition just mirrors a Ringg call (PROJECT_METHODOLOGY_AND_LEARNINGS.md 6c). crm_tasks can
    # return the same task id more than once, so collapse to one row per task_id.
    ob_status_path = raw_calls / "agent_outbound_task_status.csv"
    if ob_status_path.exists():
        df = pd.read_csv(ob_status_path).drop_duplicates(subset="task_id", keep="first")
        print(f"[00] agent_outbound_task_status.csv: {len(df)} distinct task_id; "
              f"status counts {df['task_status'].value_counts().to_dict()}")
        df.to_parquet(out_dir / "calls_agent_outbound_task_status.parquet", index=False)
    else:
        print(f"[00] NOTE: {ob_status_path} not found -- ghost-task (status!=20) filtering in Stage 05 "
              f"will be skipped this run.")

    print(f"[00] all ClickHouse extracts validated and staged in {out_dir}")


if __name__ == "__main__":
    main()
