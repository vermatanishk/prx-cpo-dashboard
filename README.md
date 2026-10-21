# PRX CPO Dashboard

Single source of truth for the PlatinumRX contacts-per-order (CPO) view. The pipeline produces aggregate
numbers, this repo stores them, and a Google Apps Script rebuilds **one** tab in the Sheet from the repo —
no more uploading an xlsx every round.

```
pipeline (local: raw/ + intermediate/)  ->  Stage 33 export  ->  data/*.csv  --git push-->  GitHub
                                                                                              |
                                          Sheet tab "CPO Dashboard"  <-  apps_script/Code.gs  (reads data/ via GitHub API)
```

**Privacy:** the repo is public and holds aggregates only (counts by month / channel / disposition). Raw tickets,
phones, order IDs and intermediate parquet files are git-ignored and never leave the pipeline machine.

## What the tab shows

One continuous view, months latest-first plus Total (same as the original "Summary" tab), with the other tabs rolled in:

1. Summary (orders, users, contacts, IPO / IPI / CPO)
2. Contact volume by channel — before and after dedup (with reconciliation check rows)
3. Contacts to resolution — by Order ID, and by Order ID × Sub-disposition, with Bot / Human split,
   percentiles, and Overall FCR / BFCR / HFCR
4. Breakdown by disposition → sub-disposition (pre-dedup): each cell reads `volume | % share | IPI` —
   disposition rows: share of all contacts; sub-disposition rows: share of their disposition. IPO by sub-disposition follows;
   the numeric calculation-base rows sit underneath in a collapsed group (click + to expand)

### Reading the formulas

* Every numeric row has a **Ref** letter (A, B, … Z, AA, …).
* The **Formula** column says how a derived row is built from those letters — `B / A`, `J x K`, `AE - AF`.
* Derived rows are live sheet formulas over the lettered rows, so you can click a cell and see the same arithmetic.
  Total-column ratios use the same formula over the Total column (never an average of monthly ratios).

| Ref | Metric | Formula |
|---|---|---|
| A | Total Orders | base |
| B | Orders with a Support contact | base |
| D | % Support-driven orders | B / A |
| H | Raw contacts (before dedup) | base |
| I | Unique issues (after dedup: Order ID × Sub-disposition) | base |
| J | IPO — issues per order | I / A |
| K | IPI — interactions per issue | H / I |
| L | CPO — contacts per order | J x K  (= H / A) |

(Refs below row L shift if rows are added; always read them from the sheet.)

## Refreshing (every pipeline round)

```bash
# after Stages 00-17 have run in the pipeline checkout
scripts/publish.sh "Refresh: orders to <date>"
```

That runs `pipeline/scripts/33_export_dashboard_data.py`, copies `export/*` into `data/`, syncs scripts and
reference files, commits and pushes. Then in the Sheet: **PRX CPO → Refresh from GitHub** (or wait for the 8am trigger).

## Apps Script setup (one time)

1. In the Sheet: *Extensions → Apps Script*. Paste `apps_script/Code.gs`; in *Project Settings* tick
   "Show appsscript.json" and paste `apps_script/appsscript.json`.
2. Reload the Sheet, then **PRX CPO → Refresh from GitHub.** No token is needed while the repo is public
   (if it is made private again, create a read-only fine-grained token and use **Set GitHub token…**).
3. Optional: **Schedule daily refresh (8am)**.

The script writes to a tab named `CPO Dashboard` (change `CONFIG.SHEET_NAME` to point it at your existing tab).

## Repo layout

| Path | What |
|---|---|
| `data/dashboard_long.csv` | tidy `section,key,month,value` — all base numbers the tab is built from |
| `data/labels.csv` | display names + ordering of dispositions / sub-dispositions |
| `data/meta.json` | version, date range, generation time, caveats |
| `apps_script/` | `Code.gs`, `appsscript.json` |
| `pipeline/` | scripts, reference taxonomy, run config (reproducible; no data) |
| `docs/` | methodology and ClickHouse query notes |
| `scripts/publish.sh` | export + commit + push |

## Caveats carried with the data

* The latest month is right-censored (late-month orders haven't generated all their contacts yet), so its IPO / CPO read low.
* Zoho email data ends 2026-08-25, so Email is 0 afterwards.
* Only order-linked Support contacts are counted; contacts without an Order ID are excluded from CPO.
