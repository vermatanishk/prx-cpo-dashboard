# ClickHouse extraction queries (re-run these via MCP each refresh)

ClickHouse access in this project is **read-only**, via an MCP tool inside a Claude session — there
are no standalone DB credentials. So "Stage 00" isn't a script you run from a terminal; it's these
queries, run through Claude's ClickHouse MCP tool, with results saved to the paths below. Re-run all
of them (full date range, not incremental) every time the pipeline needs refreshing, then re-run the
numbered scripts starting from `01_prep_reference_tables.py`.

Service: `PRx Nucleus` (serviceId `790ddad5-a6ff-4b03-b135-9292547ed6b6`), database `prx_prod_db`.

Each query below is large enough that the MCP tool will return an error saying the result exceeds
the token limit — **that's expected and fine**: the full result is still saved to a local file on
disk (path given in the error message). Convert/append each saved file into the target CSV with:

```bash
python3 pipeline/scripts/_ingest_mcp_result.py <saved_result_path> <target_csv_path>
```

Split each query into date-range chunks small enough to avoid a hard SSE-size error (roughly
8-10 days of data at current volumes for the per-row queries; the aggregated denominator query
fits in one call for the whole range).

## 1. Orders denominator -> `raw/orders/orders_denominator.csv`

```sql
select cast(o.created_at as date) as order_date, count(distinct o.order_id) as total_orders_processed,
       count(distinct o.user_id) as unique_users
from prx_prod_db.marketplace_orders o
join prx_prod_db.auth_users u on o.user_id = u.user_id
where o.status_id not in (0,81,2,3,4,82)
  and u.is_test_user = false
  and o.user_id not in (67815,1680,63368,1939,30405,7276,17634,13516,2702,2695,2697,2703,2700,2698,2699,10411,5360449)
  and o.created_at >= '<start>' and o.created_at < '<end>'
group by 1 order by 1
```

## 2. Fallback orders (20-day phone/email join target) -> `raw/orders/fallback_orders.csv`

Pull from `(start_date - 20 days)` through `end_date`, in chunks, GROUP BY order_id to collapse
line-item duplicates server-side (marketplace_orders has multiple rows per order_id):

```sql
select o.order_id, any(o.user_id) as user_id, min(o.created_at) as created_at,
       any(u.phone_number) as phone_number, any(u.email) as email
from prx_prod_db.marketplace_orders o
join prx_prod_db.auth_users u on o.user_id = u.user_id
where o.status_id not in (0,81,2,3,4,82)
  and u.is_test_user = false
  and o.user_id not in (67815,1680,63368,1939,30405,7276,17634,13516,2702,2695,2697,2703,2700,2698,2699,10411,5360449)
  and o.created_at >= '<chunk_start>' and o.created_at < '<chunk_end>'
group by o.order_id
```

## 3. Ringg (bot) support calls -> `raw/calls/ringg_support_calls.csv`

Ringg's disposition/order fields are nested under `analysis_data` in the meta JSON (confirmed by
sampling real rows — the flat `orderId`/`disposition` keys assumed in an earlier draft of this query
don't exist and silently returned empty strings for every row; caught via the Stage 03/05 validation
print showing 0% order-ID fill and fixed before the final run):

```sql
select id, internal_user_id, call_status, customer_phone_number, call_id, created_at,
  JSONExtractString(meta,'analysis_data','disposition') as meta_disposition,
  JSONExtractString(meta,'analysis_data','sub_disposition') as meta_sub_disposition,
  JSONExtractString(meta,'analysis_data','unresolved_order_number') as meta_order_id
from prx_prod_db.auth_internal_user_call_logs
where lower(operator)='ringg' and lower(department_name)='support'
  and created_at >= '<start>' and created_at < '<end>'
```

## 4. Agent Inbound (connected, support menu path) -> `raw/calls/agent_inbound_calls.csv`

Confirmed live: `menu_extension` values `'1,2'` and `'2,2'` correspond to the Support menu path
(the BRD's stated rule); `'2,1'`/`'1,1'` are Sales paths. Pull in date chunks:

```sql
select id, customer_phone_number, call_id, created_at,
  JSONExtractString(meta,'menu_extension') as menu_extension
from prx_prod_db.auth_internal_user_call_logs
where call_type='INCOMING' and call_status='CONNECTED' and operator='knowlarity'
  and JSONExtractString(meta,'menu_extension') in ('1,2','2,2')
  and created_at >= '<chunk_start>' and created_at < '<chunk_end>'
```

## 5. Agent Outbound (Missed Call Support tasks) -> `raw/calls/agent_outbound_calls.csv`

Join key confirmed live: `crm_task_call_logs.call_id` == `auth_internal_user_call_logs.call_id`
(both hold the Knowlarity call UUID) — this resolves the earlier "unconfirmed join key" open item.
LEFT JOIN (not filtering the joined tables by date) so a disposition/call-log created after the
chunk boundary is still captured:

```sql
select t.id as task_id, t.user_id, t.created_at as task_created_at, t.completed_at, t.activated_at,
       tcl.call_id, tcl.dialer_type, tcl.created_at as call_log_created_at,
       td.disposition, td.sub_disposition, td.status as disposition_status, td.created_at as disposition_created_at,
       JSONExtractString(td.meta,'orderId') as meta_order_id
from prx_prod_db.crm_tasks t
left join prx_prod_db.crm_task_call_logs tcl on tcl.task_id = t.id
left join prx_prod_db.crm_task_dispositions td on td.task_id = t.id
where t.name='Missed Call Support' and t.created_at >= '<chunk_start>' and t.created_at < '<chunk_end>'
```

Note: `crm_tasks.created_at` here doubles as the Agent-Outbound blank-timestamp fallback field the
brief calls for — every row already has it, so no separate handling is needed downstream.

## 5b. Agent Outbound Companion Task Recovery -> `raw/calls/agent_outbound_companion_dispositions.csv`

**Fixed 2026-09-03.** When a Missed Call Support task connects, the agent's real disposition often
lands on a *separate* task created at that moment, not on the original task's own id. This
companion task is created under **two different, equally common names** —  `In Call` and
`In Call Support` — confirmed live by `select name, count() from crm_tasks group by name`
(394,684 vs 40,873 occurrences respectively over the full history). The query that had been in use
before this fix only ever searched for `In Call`; it was never written down anywhere reproducible
(this section didn't exist), which is partly how the missing name went unnoticed. This one covers
both, and ranks candidates to prefer one that actually carries a real disposition (see the
empty-string gotcha below) over a merely-earlier one that doesn't:

```sql
with candidates as (
  select o.id as orig_task_id, o.user_id, o.completed_at
  from prx_prod_db.crm_tasks o
  where o.name = 'Missed Call Support'
    and o.created_at >= '<chunk_start>' and o.created_at < '<chunk_end>'
    and o.id not in (select task_id from prx_prod_db.crm_task_call_logs)
    and o.id not in (select task_id from prx_prod_db.crm_task_dispositions)
    and o.completed_at is not null
),
matches as (
  select c.orig_task_id, r.id as companion_task_id, r.created_at as companion_created_at,
         td.disposition as td_disposition
  from candidates c
  inner join prx_prod_db.crm_tasks r
    on r.user_id = c.user_id
    and r.name in ('In Call', 'In Call Support')
    and r.created_at >= c.completed_at
    and r.created_at <= c.completed_at + interval 10 minute
  left join prx_prod_db.crm_task_dispositions td on td.task_id = r.id
),
ranked as (
  select *,
         row_number() over (
           partition by orig_task_id
           order by (td_disposition is not null and td_disposition != '') desc, companion_created_at asc
         ) as rn
  from matches
)
select r.orig_task_id as task_id, td.disposition as companion_disposition,
       td.sub_disposition as companion_sub_disposition,
       JSONExtractString(td.meta,'orderId') as companion_meta_order_id
from ranked r
left join prx_prod_db.crm_task_dispositions td on td.task_id = r.companion_task_id
where r.rn = 1
```

**Empty-string-vs-NULL gotcha (a second bug found while fixing the first):** `crm_task_dispositions.disposition`
is a non-nullable `String` column. A `LEFT JOIN` against a companion task that has a disposition
*row* but no real value returns `''`, not `NULL` — a plain `.notna()` check in pandas does **not**
catch this and will falsely count it as recovered. Explicitly filter/replace `''` as missing on
both the ranking condition above (already handled) and after loading the CSV (see
`00_load_clickhouse_extracts.py`).

**Dead end, deliberately not used:** `crm_tasks.meta` sometimes carries a `lastTaskDisposition` /
`lastTaskSubDisposition` field that looks like a third recovery source. It isn't one — verified by
cross-checking 25 already-disposed tasks, where `meta.lastTaskDisposition` consistently showed a
*different* category than the task's real, current disposition. It's a snapshot of the customer's
previous, unrelated interaction, shown to the agent as context at task-creation time — using it
would backfill wrong outcomes, not real ones.

**Open question for engineering, not resolved by this fix:** every companion disposition sampled
this session was entered by `internal_user_id = 186` (the confirmed automated/system account
documented in `PROJECT_METHODOLOGY_AND_LEARNINGS.md` §4) within 19–67 milliseconds of the original
task closing — too fast for a human. This looks like a system/webhook process writing the real
outcome to a new task record instead of the original one. Worth a CRM-side investigation into why,
independent of this pipeline's ability to go find it after the fact.

## 6. Return-flow status log for `missing_or_less_quantity_received` orders -> `raw/orders/quantity_status_log.csv`

Used by `24_missing_or_less_quantity_deepdive.py`'s MECE classifier to detect a backend
return-cancelled-then-reopened loop. Scoped to only the distinct `final_order_id`s already present in
that sub-disposition's `dedupable_raw.parquet` (~2,100 ids) rather than a full-table pull -- generate
the `IN (...)` list from that parquet each time this is refreshed:

```sql
select order_id, current_status_id, previous_status_id, remarks, summary, created_at,
       workflow_type, description, sub_remarks
from prx_prod_db.marketplace_order_status_log
where order_id in (<comma-separated distinct final_order_id list from dedupable_raw.parquet
                    for missing_or_less_quantity_received, ~2,100 ids>)
order by order_id, created_at
```

Relevant `current_status_id` values for the return/refund journey: `200`=RETURN_REQUEST_RECEIVED,
`210`=PARTIAL_RETURN_REQUEST_RECEIVED, `280`=RETURN_CANCELLED, `250`=ITEM_QUALITY_CHECK_AT_OUR_FACILITY,
`260`=REFUND_INITIATED, `265`=REFUND_PROCESSED. The cancel-then-reopen pattern is a `280` row followed
later by a `200`/`210` row for the same order (confirmed hand-example: order 3076348).

Result exceeds the MCP tool's inline token limit at this row count (~45k rows / 2,126 orders) -- saved
to disk and ingested the same way as every other query here (`_ingest_mcp_result.py`).

**v5 refresh (2026-10-07)**: `intermediate/run_2026-05-01_2026-10-01/quantity_status_log.parquet` covers the
2,817-order v5 cohort (60,593 rows, 4 columns: `order_id, current_status_id, previous_status_id, created_at`).
Built by re-using the cached v3 pull for the 2,109 orders already fetched and running the query above only for the
708 new orders. Return-state names/phases come from `reference/order_status_master.csv` (200/210 request,
220-234 approval/pickup, 236-250 warehouse/QC, 260-265 refund, 280 cancelled).

Cancel actor + timing (who cancels a return) comes from a second small pull saved as
`raw/orders/return_cancel_events_since_2026-05-01.csv` (5,122 rows):

```sql
select order_id, ops_user_name, workflow_type, created_at
from prx_prod_db.marketplace_order_status_log
where current_status_id = 280 and created_at >= '2026-05-01'
order by order_id, created_at
```

`ops_user_name = 'Backed System User'` is the automated actor; a NULL `ops_user_name` on a `return_status_update` row
is the system mirror of the same cancel. `remarks`/`sub_remarks` are empty on every cancel event, so no cancel reason
is recorded anywhere in this table.

## Confirmed live-schema notes (resolves plan's open items g/h)

- Ringg leg's real filter is `lower(operator)='ringg' and lower(department_name)='support'`
  (the BRD's `call_intent='Support'` doesn't exist as a literal field — `department_name` serves
  that role, values are `Support`/`Sales`/blank, case varies).
- Agent-Outbound timezone: `crm_tasks.created_at` in ClickHouse is stored as naive UTC-ish
  timestamps matching the rest of the schema; the 08:00-20:00 office-hours window in
  `run_config.yaml` is applied as-is against this column with no timezone conversion applied yet
  — **still needs sign-off that no conversion is needed** (see plan's open item h).
