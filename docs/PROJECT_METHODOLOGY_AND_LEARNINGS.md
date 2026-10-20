# PlatinumRx Contact Attribution Project — Methodology, Context & Learnings

Status snapshot as of this writing. This document exists because the working session that built this
pipeline ran long enough that early decisions and their rationale were at risk of being lost. Read this
before touching the pipeline again — it will save you from re-litigating things that were already decided,
and from re-introducing bugs that were already found and fixed.

## 1. Purpose

Build a single source of truth for every PlatinumRx (PRX) customer contact across three channels —
Calls (ClickHouse), LimeChat (chat, CSV exports), Zoho (email, CSV exports) — for two goals:
1. A dashboard/metrics tracking view (CPO, resolution, dedup volumes).
2. Root-cause analysis into *why* customers contact support, to drive product/comms fixes.

This replaced an earlier analyst's incomplete/abandoned attempt (`combined_calls_limechat_zoho_dedup_flagged 2.xlsx`),
which used different, less-validated logic and should not be treated as a reference for "correct" numbers.

## 2. Data Sources & Channels

- **ClickHouse** (`prx_prod_db`, read-only): `auth_internal_user_call_logs`, `crm_tasks`,
  `crm_task_call_logs`, `crm_task_dispositions`, `marketplace_orders`, `auth_users`.
- **LimeChat**: monthly CSV exports in `raw/limechat/` (May–Jul only as of this writing — **August LimeChat
  data is not yet available**, a real coverage gap, not a bug).
- **Zoho**: CSV exports in `raw/zoho/` (90-day + 120-day dumps, deduped by Ticket ID keeping the most
  recent export).

Three call "legs", each with its own extraction query (see `README_clickhouse_queries.md`):
- **Ringg** (AI voice bot): `operator='ringg' AND department_name='support'`.
- **Agent Inbound (Agent IB)**: `menu_extension IN ('1,2','2,2')` (Support menu path).
- **Agent Outbound**: `crm_tasks.name='Missed Call Support'`, with a "companion task" disposition-recovery
  step (a connected call's real disposition often lands on a separate "In Call" task, not the original).

LimeChat sub-channels: `LimeChat Bot` (pure bot, no handoff) and `LimeChat Bot + Agent` (human touched it
after handoff).

## 3. Pipeline Architecture (11 stages, `pipeline/scripts/`)

00 (ClickHouse extracts) → 01 (reference prep) → 02 (LimeChat load) → 03 (LimeChat classify) →
04 (Zoho load) → 05 (Calls classify) → 07 (combine channels) → 08 (dedup) → 09 (resolution/FCR) →
10 (Excel report). Stage 06 was folded into 07.

Shared helpers live in `pipeline_utils.py`: `normalize_phone`, `normalize_email`,
`normalize_disposition_value` (+ `EXACT_SYNONYM_MAP`), `fallback_order_join` (phone-keyed),
`fallback_order_join_by_email` (email-keyed, added this session — see §6), `SALES_DISPOSITION_KEYS`.

## 4. Core Methodology Decisions (already agreed — do not re-litigate without reason)

- **Order attribution rule**: a contact's order is its **direct Order ID** if present, else the most
  recent order for that customer's phone (or email, for Zoho) within a **20-day lookback window**,
  strictly before the contact timestamp (`fallback_join` config, `window_days: 20`).
- **Dedup key**: group by (Final Order ID × normalized Sub-disposition); Primary = first
  (chronologically earliest) row flagged Actionable in the group, else first row overall.
- **Post-dedup universe = `deduped_primary_contacts` + `not_dedupable_raw`** (contacts with no
  Order ID at all, direct or fallback). This is the pipeline's own documented spec (see
  `10_write_excel_report.py`'s comment): a contact with no Order ID still counts as its own contact
  since nothing merges it with anything else. **Do not narrow this to `deduped_primary_contacts` alone**
  — that was tried mid-session and reverted; it doesn't match the agreed spec.
- **Sales stripped before any Support metric**: `SALES_DISPOSITION_KEYS` = `{scope_for_sale,
  no_scope_for_sale, potential_sale, sales_related, sales_related_created_a_new_task}`. Classify by
  actual disposition value, never by channel/menu-path assumption (a menu path doesn't guarantee topic).
- **Bot-Resolved / CPO Bot**: whole-order-journey, 48-hour-aware. A key counts as Bot-Resolved only if
  every contact in it used a bot-only channel (Ringg or LimeChat Bot) **and** there was no further
  contact within 48h of the last one. This lives in Stage 09 (`resolution_by_order.parquet`) — Stage 08
  had a cruder, non-48h-aware version that has been retired; don't resurrect it.
- **Account `internal_user_id = 186`**: a confirmed automated/system account (~40ms task completion
  time, tens of thousands of dispositions) that contaminates the "human-led" population. Exclude it
  from any Human-Led headline count. This is a genuine data-quality finding, not a bug in this pipeline.
- **Agent IB real dispositions only exist from 2026-05-12 onward** — a system rollout date, confirmed
  live (not a query artifact). Before that date, Agent IB calls are structurally undisposed; this is a
  system gap, not a compliance failure, and matters when analyzing View 1 (Not Disposed) root causes.

## 5. View 1 / View 2 Framework

- **View 1 — Not Disposed At All**: both Disposition and Sub-disposition blank.
- **View 2 — Disposed, Sub-disposition Not Listed**: Disposition present, Sub-disposition blank or one
  of the 9 "issue not listed" catch-all keys (`issue_not_listed` + 8 category-specific variants:
  `..._account_and_app`, `..._consultation_and_medicine`, `..._delivery_issues`, `..._edge_membership`,
  `..._order_management`, `..._payments_and_refunds`, `..._return_and_pickup`, `..._wallet_and_rewards`).

Both views get reported as **3 channel buckets**: Bot-Led Ringg / Bot-Led LimeChat (pure bot only) /
Human-Led All (Agent IB + Agent Outbound + LimeChat Bot+Agent, account 186 excluded). Deep-dive analysis
uses **raw records**, not deduped ones — every individual touch is its own coaching/taxonomy data point.
Macro/headline share metrics use the **post-dedup** population (§4).

### View 2 deep-dive method
Every raw record in the population gets force-classified into exactly one of: an existing sub-disposition
it should have been (named), a genuinely new taxonomy gap (New Field), Spam/Unrecognisable, or
Uncapturable (no text exists to classify from). **Zero residual "Other" bucket is the acceptance bar.**
New Field proposals found so far (with per-channel counts): `mrp_or_labeled_price_mismatch`,
`cold_chain_temperature_complaint`, `order_not_found_for_customer_number`,
`escalation_immediate_human_agent_requested`, `order_swap_between_customers` (kept separate from
`wrong_item_delivered` per explicit direction — two customers' orders crossing is a distinct failure
mode from a single wrong item), `health_symptom_or_medical_guidance_query`,
`wants_doctor_consultation_request`, `specific_courier_partner_preference`,
`otp_not_received_for_delivery`, `batch_number_mismatch_or_query`, `patient_name_change_on_order`,
`hub_address_or_alt_contact_request`, `medicine_not_effective_or_no_relief`,
`duplicate_order_placed_unintentionally`, `product_composition_mismatch_vs_listing` (low-volume but
safety-relevant — flag to quality/compliance regardless of count).

### View 1 findings
34.4% of macro contacts are Not Disposed. Biggest bucket, Agent IB, splits roughly 47% system-cause
(pre-May-12 rollout gap + a recurring 1–2pm queue/telephony linkage failure — confirmed via stratified
ClickHouse sample, not a full census) vs 53% genuine process/compliance gap, spread broadly across many
agents, not concentrated in a few. LimeChat's Private Notes field (only ~0.6% fill rate) still yielded a
small, real, fully-classified signal — check it before assuming a channel has "no text."

## 6. Bugs Found & Fixed This Session

- **Ringg JSON field path**: fields live under `meta.analysis_data.*`, not flat `meta.*`.
- **Zoho "Generated On" parsing / dual timestamp formats**: fixed CSV-preamble parsing and added
  dual-format datetime parsing with `combine_first`.
- **Agent Outbound companion-task fan-out**: `row_number()` nearest-match created exact duplicate rows;
  fixed with `drop_duplicates(subset="task_id", keep="first")`.
- **Sales-leak**: Ringg/Agent IB/Agent Outbound/Zoho-Email were hardcoding `sales_or_support="Support"`
  regardless of actual disposition. Fixed to classify by disposition value against
  `SALES_DISPOSITION_KEYS`.
- **LimeChat Team-Name override bug**: once Team Name said "Support Team" (the most authoritative
  signal), lower-priority rules could still flip the verdict to Sales. Fixed to lock in Team Name's
  verdict across all downstream priority steps.
- **`order_id = 0` treated as valid**: caused ~452 unrelated Calls records to pool under a fake shared
  "order 0". Fixed `extract_single_numeric_id` to reject literal 0.
- **Bot-Resolved logic**: Stage 08's narrow, non-48h-aware version replaced by Stage 09's correct,
  already-existing whole-order-journey/48h definition (see §4).
- **Email fallback near-total failure (0.1% match rate)**: root cause was NOT a wrong join key (it was
  already phone-keyed, same shared function as every other channel) — it's that 96.8% of Zoho Email
  tickets simply carry no phone number, so the phone-fallback pass never even attempts a match for them.
  Fixed by adding `fallback_order_join_by_email` (pipeline_utils.py), keyed on the fallback orders
  table's `email` field, as an additive second pass in `04_load_zoho.py` for whatever the phone pass
  didn't fill. Result: Email's fallback fill rate went from 2.4% → 39.7%; overall Email `final_order_id`
  fill went 39.5% → 53.8%. Effect on headline macro numbers was small (Email is <1% of total volume)
  but the fix itself is real and should stay.

## 6b. Bugs Found & Fixed (2026-09-03 session)

- **Agent Outbound companion-task recovery was missing a valid task name.** The recovery step (see
  §6's "Agent Outbound companion-task fan-out" entry above) is designed to catch a connected call's
  real disposition when it lands on a separate task instead of the original "Missed Call Support"
  task. It only ever searched for that companion under the name `In Call`. Live schema check
  (`select name, count() from crm_tasks group by name`) showed PlatinumRx's CRM uses a second,
  equally common name for the exact same event: `In Call Support` (40,873 occurrences historically,
  vs `In Call`'s 394,684) — never searched for. Traced directly: 11 of 15 sampled "not disposed"
  Agent Outbound tasks had an `In Call Support` companion created 19–67ms after the original
  closed, already carrying a real disposition. Fixed by adding `In Call Support` to the companion
  match (see `README_clickhouse_queries.md` §5b for the corrected, now-documented query — the old
  query had never been written down anywhere reproducible, which is partly how the missing name
  went unnoticed for this long).
  **Result, shipped to the real pipeline and verified end-to-end (not projected): Agent Outbound's
  "no-connect" bucket dropped from 28,879 → 9,472 raw touches (7,835 post-dedup, down from 19,021).**
  Macro not-disposed headline moved from 136,935 (34.4%) to **127,630 (32.28%)** — see §8.
- **Second bug found while fixing the first: ClickHouse empty-string vs NULL for non-nullable
  columns.** `crm_task_dispositions.disposition` is a non-nullable `String`. A companion task that
  has a disposition *row* but no real value returns `''`, not `NULL`, from a LEFT JOIN — a plain
  pandas `.notna()` check does not catch this and will falsely count it as recovered. This first
  showed up as a real discrepancy (a naive full-scale estimate said ~22,730 recoverable; the actual
  end-to-end rerun only recovered 19,359) before being traced and fixed. Now handled both in the
  SQL (ranking prefers a companion with a real, non-empty disposition) and defensively in
  `00_load_clickhouse_extracts.py` (explicit `.replace("", pd.NA)` on load, so this can't silently
  reappear on a future extract even if a query regresses).
- **Dead end, deliberately not used: `crm_tasks.meta.lastTaskDisposition`.** Looked like a third
  recovery source. Cross-checked against 25 already-disposed tasks and found it's a stale snapshot
  of the customer's *previous, unrelated* interaction (shown to the agent as context at task
  creation), not this task's outcome — using it would have back-filled wrong answers. Ruled out
  before it did any damage; documented in README §5b so it isn't re-attempted.
- **Open item, not resolved by this fix, flagged for PlatinumRx engineering**: every recovered
  companion disposition sampled was entered by `internal_user_id = 186` (the confirmed
  automated/system account from §4) within 19–67 milliseconds of the original task closing — too
  fast for a human. Reads like a system/webhook process writing the real outcome to a new task
  record instead of the original one. This pipeline can now find it either way, but the underlying
  CRM behavior is worth its own investigation.

## 6c. Good to know (2026-09-16 session): Agent-OB-WH `status=60` tasks are auto-closed ghosts, not a
second real agent contact — a source of inflated IPI/repeat-contact counts wherever Ringg is heavy

While auditing `missing_or_less_quantity_received` and `dosage_or_usage_related_query` for why their
IPI ran well above the ~1.39 portfolio average, a Ringg call kept showing up paired with an
`Agent OB WH (completed)` contact seconds to minutes later, on the same order — initially read as "the
same live call logged twice by two systems." Direct verification against `crm_tasks` +
`crm_task_dispositions` in ClickHouse shows the real mechanism is different, and worth knowing before
trusting any IPI/repeat-contact number for a Ringg-heavy sub-disposition:

- **`crm_tasks.status` for `name='Missed Call Support'` splits into two very different populations**,
  confirmed portfolio-wide:
  - `status=20` ("Connected", genuinely agent-worked): 221,966 tasks, **100%** carry a real,
    independently-entered disposition in `crm_task_dispositions`, median time from `created_at` to
    `completed_at` is **~24 minutes**. This is the real "Ringg call was unresolved off-hours -> a human
    agent calls back later" flow — a legitimate second contact, correctly counted.
  - `status=60` (auto-closed): 112,638 tasks, only **2.1%** carry a real disposition of their own — the
    other 97.9% have an empty `crm_task_dispositions.disposition` — and median completion time is
    **86 seconds**. No human ever works these; they self-close almost immediately.
- **The bug**: our pipeline's Agent-Outbound companion-task disposition recovery (`05_load_calls.py`,
  documented in `README_clickhouse_queries.md` §5b) still finds *something* to attach to these
  `status=60` ghost tasks — but the recovered value is in Ringg's own ALL-CAPS format
  (`"CONSULTATION & MEDICINE"`, `"RETURN & PICKUP"`), not an agent's own typed-in text. Confirmed
  directly on the specific task_ids behind several duplicate pairs (e.g. task 14657483, paired with a
  Ringg call 12 seconds apart) — every one was `status=60` with zero real disposition on the task
  itself, yet carried a companion-recovered disposition mirroring Ringg's own outcome.
- **Net effect**: one Ringg call produces two contact rows — the Ringg row, and an auto-closed
  `status=60` placeholder that was never actually worked by anyone but ends up back-filled with a copy
  of Ringg's own result. This inflates IPI/"repeat contact" counts for any sub-disposition where Ringg
  is a large share of volume (confirmed: **98.6%** of `dosage_or_usage_related_query`'s multi-touch
  episodes reduce to this single mechanism; a smaller but real share, ~26%, of
  `missing_or_less_quantity_received`'s).
- **How to tell the two apart before trusting a repeat-contact number**: join the Agent-OB-WH contact
  back to `crm_tasks.status` (needs a fresh ClickHouse pull keyed by `task_id`, not currently cached
  locally). `status=20` = keep, it's a real second contact. `status=60` (or `50`, which shows the same
  mostly-empty pattern at smaller volume) paired with a Ringg row on the same order within a short
  window = drop, it's the same call. **Not yet fixed in the pipeline itself** — every deep-dive so far
  has hand-detected this per sub-disposition via timestamp proximity; a systematic fix belongs in
  `05_load_calls.py`'s Agent-Outbound leg classification, gating the companion-recovery merge on the
  *original* task's own `status` rather than merging it in unconditionally.

## 7. Investigated and Ruled Out (do not re-attempt these "fixes")

- **Rebuilding population counts via a stricter INNER JOIN** (done mid-session to "verify" Calls-Human
  population): this *lost* real records rather than finding a bug — LEFT JOIN is correct because a
  weak/partial disposition-chain match should still count the contact. Reverted.
  **Lesson: matching a smaller, "cleaner" number against a larger existing one is not automatically a
  fix — check what got dropped and why before assuming the strict version is right.**
- **Ringg's and LimeChat Bot's lower fallback match rates (30.8% / 29.7% vs Agent IB's 70.3%)**:
  confirmed NOT a phone-normalization bug (formats are clean; Ringg's phones exist in order history
  69.3% of the time *ever*, just usually >20 days before the call — a genuine "these callers ask about
  older orders" pattern, not a matching failure). LimeChat Bot's ceiling really is lower (31.8% of
  phones ever match, vs Agent IB's 78.9%) because it's a broader, more casual inbound channel
  (Instagram/WhatsApp/pre-purchase browsing), not a bug.
- **Chasing the older `PlatinumRx_CPO_Analysis_v3_slim.xlsx` sheet's ~6% View 2 share / 186k macro
  total**: that sheet was built earlier in this same session, before both the LimeChat System Tags
  routing refinement and other fixes above landed. Its much larger
  `issue_not_listed_order_management` count (6,807 vs current 164, post-dedup) traces to a cruder,
  earlier LimeChat tag-mapping that over-assigned that catch-all — the current, lower number is more
  accurate, not a regression. **Do not tune the pipeline to reproduce that sheet's numbers.**

## 8. Current Locked-In Headline Numbers (full period, May 1 – Aug 21 2026)

**Superseded 2026-09-03** by the Agent Outbound companion-join fix (§6b) — numbers below are
post-fix, output as `_v2` files (`run_config.yaml`'s `version_tag`), with the pre-fix `_v1` outputs
left on disk as the historical record rather than overwritten.

| Metric | Value (pre-fix, v1) | Value (post-fix, v2) |
|---|---|---|
| Total deduplicated macro contact count | 397,633 | **395,352** |
| View 1 (Not Disposed At All) | 136,935 (34.4% of macro) | **127,630 (32.28% of macro)** |
| Agent Outbound "no-connect" (post-dedup) | 19,021 | **7,835** |

The View 2 (Disposed, Issue Not Listed) and per-channel raw-touch deep-dive figures below predate
this fix and have not been re-run against the corrected data yet — treat them as historical until
refreshed:

| Metric | Value |
|---|---|
| Count of View 2 (Disposed, Issue Not Listed, all 9 variants) | **8,769** |
| Share of View 2 | **2.21%** |

View 2 raw-record deep-dive population (used for the per-channel workbook, not the macro share above):
Bot-Ringg 3,256 / Bot-LimeChat 21 / Human-Led-All 5,882 (Agent IB 3,449 + Agent Outbound 992 + LimeChat
Bot+Agent 1,441) = **9,159 total raw touches**.

## 9. Known Open Items / Caveats

- **August LimeChat and full-range Zoho data are not yet available** (per `run_config.yaml` comment) —
  the "full period" macro numbers understate August's LimeChat/Zoho contribution specifically; Calls
  should be complete through Aug 21.
- **Agent Outbound notes-lookup gap**: only 620 of 992 View-2 Agent Outbound `task_id`s successfully
  matched a `crm_task_dispositions` row when fetched directly by ID (vs. the pipeline's own join, which
  found all 992 via LEFT JOIN with companion-task fallback). Root cause not yet found — treated as
  "Uncapturable — notes lookup gap" in the workbook rather than silently dropped, but this is a
  discrepancy worth investigating if Agent Outbound's deep-dive needs to be tightened further.
  Post-dedup Zoho Email View2 count: 242 raw / small post-dedup — Email is included in the post-dedup
  channel split but was intentionally left out of the raw 3-bucket deep-dive scope (flagged, not hidden).
- **LimeChat Bot+Agent's Private Notes field has only ~2% fill rate** in View 2's population (29 of
  1,441) and ~0.6% in View 1's (357 of 57,808) — real signal, but thin. Don't expect LimeChat to
  surface New Field candidates at the same rate as Calls/Ringg; that's a text-availability fact, not
  under-investigation.
- **The 4 riskiest New Field proposals** (`fraud_or_delivery_misconduct_report`,
  `patient_name_change_on_order`, `delivery_status_not_updated_in_system`,
  `duplicate_order_placed_unintentionally`) have plausible-but-unconfirmed overlap with existing
  taxonomy keys (`unprofessional_behavior_by_delivery_executive`, `profile_updation_request`,
  `unable_to_track_shipment`, `cancellation_request_for_full_order` respectively) — flagged for the
  taxonomy owner to judge, not silently merged.

## 10. Process Learnings (how to work this pipeline going forward)

1. **Before recomputing any headline number, check whether the pipeline's own scripts already have a
   documented, decided definition for it** (grep the stage scripts' comments first) — several hours
   this session were spent re-deriving definitions ad hoc in scratch scripts that the pipeline already
   had settled, correctly, in code.
2. **A smaller/stricter number is not inherently more "correct."** Every time a rebuilt join produced a
   smaller population, the right move was to check what got excluded and why — not to assume the
   stricter version was the fix.
3. **When a proposed "new field" or classification pattern shows up, grep the full existing taxonomy
   (all ~173 raw keys, not just the ~88-row current master) before proposing it as new** — MRP mismatch
   was missed once already by not doing this rigorously the first time.
4. **Raw ClickHouse query results that exceed the tool's inline size limit get saved to a file** —
   process them with local Python/jq rather than reading the full dump into context; this is much
   cheaper and avoids truncation-induced errors (an early large ID-list query burned significant
   context this way before the pattern was caught).
5. **When comparing against an older reference sheet, first confirm what pipeline state produced it**
   (date it, check which fixes predate/postdate it) before treating a mismatch as evidence of a current
   bug — the reference itself may be the stale one.
