"""Stage 18: Re-implemented S/M cohort assignment (S0-S3, M0-M12, MX) for the
2026-05-01..2026-08-22 raw export, since the original "Apps Script replica" that used to produce
this is no longer in this repo (see Stage 16's docstring). Built from the cohort rules documented
in 16_cohort_definitions_table.py's ROWS table, run against Stage 17's raw export.

KEY FIX vs. the user's own Google Apps Script attempt at this same logic: that script compared
raw datetime CELL VALUES for equality (`c1.activeEDD === originalEDD`) to detect "no EDD change."
Google Sheets round-trips xlsx dates through a floating-point serial number, which introduces
sub-millisecond noise -- so that equality check almost never fired, collapsing every "no change"
cohort (M1/M2/M5/M6/M9/M12) to zero and dumping everything into Postponed/PulledEarly instead.
Fixed here by normalizing every date to DAY granularity before comparing (`.dt.normalize()`),
matching exactly how the Within/On/Post columns were already computed on the Python side.

METHODOLOGY (per the ROWS table):
- Single-contact orders (S-cohort): S0 if no EDD at all, else S1/S2/S3 by the contact's own
  Within/On/Post position vs Original EDD.
- Multi-contact orders (M-cohort): decided by contact 1 vs contact 2 ONLY (contact 3+ ignored,
  per the documented override). M0 if no EDD. Otherwise:
    - eddState = NoChange / Postponed / PulledEarly, from contact 1's Active EDD vs Original EDD
      (day-granularity).
    - breach = whether contact 2 landed Post the EDD that was ACTUALLY active at contact 2's own
      moment (not the stale Original EDD) -- this correctly distinguishes M9 (still breaching)
      from M12 (self-corrected before contact 2) without needing a separate "was it revised
      between contacts" flag: if contact 1 already landed after an unrevised EDD, a later contact
      2 landing on/before the active EDD is only possible if that EDD moved forward since --
      it's implied by chronology, not separately computed.
    - PulledEarly always -> MX (delivery moved up, a good-news case).
  Same code applied to every row of that order.

Reads:  output/PRX_EDD_Raw_Export_2026-05-01_to_2026-08-22.xlsx logic, recomputed from:
        intermediate/run_2026-05-01_2026-08-22/dedupable_raw.parquet
        raw/orders/edd_revisions_full_with_status.parquet
        raw/orders/order_analytics_edd_fallback.parquet
        reference/order_status_master.csv
Writes: output/PRX_EDD_Raw_Export_with_Cohort_2026-05-01_to_2026-08-22.xlsx
        output/PRX_Cohort_Definitions_Rebuilt.xlsx
"""
import pandas as pd
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

from pipeline_utils import PIPELINE_ROOT

TARGET_SUBDISPS = ["delayed_or_likely_to_be_delayed", "shipment_status_query_(no_delay)"]
WINDOW_DIR = PIPELINE_ROOT / "intermediate" / "run_2026-05-01_2026-08-22"

GREEN = "C6E0B4"
YELLOW = "FFE699"
ORANGE = "F8CBAD"
GREY = "D9D9D9"
HEADER_FILL = "632423"

ROWS = [
    ("S1", "Before OG EDD", "No Change", "No Second contact",
     "Pure anxiety-driven contact",
     "Live, trustworthy tracker + proactive WhatsApp on status", "Product"),
    ("S2", "On OG EDD", "No Change", "No Second contact",
     "Legitimate delivery-day check-in - possible lack of visibility",
     "Real-time \"out for delivery\" / live rider location", "Product"),
    ("S3", "After OG EDD", "Postponed", "No Second contact",
     "Order was already late, but agent was able to reassure in one contact",
     "Good outcome for CX - to be improved upon by taking learnings", "CX / Logistics"),
    ("S0", "No EDD data", "N/A", "No Second contact",
     "No EDD anywhere (revision history or order_analytics) - nothing to measure a promise against",
     "Investigate remaining ~0.7% no-EDD orders (dispatch/ERP integration gap)", "Product / Data"),
    ("M1", "Before OG EDD", "No Change", "On or Before Revised EDD",
     "Reassurance didn't stick the first time", "L1 script: give a specific date + next steps", "CX"),
    ("M2", "Before OG EDD", "No Change", "After Revised EDD",
     "Real delay happened, was never caught/communicated between contacts",
     "Proactive alert the moment a delay looks likely", "Product / comms"),
    ("M3", "Before OG EDD", "Postponed", "On or Before Revised EDD",
     "Customer needed a second nudge even though the system had already adjusted",
     "Confirmation SMS/WhatsApp when EDD is revised", "Product / comms"),
    ("M4", "Before OG EDD", "Postponed", "After Revised EDD",
     "The revised promise was missed too", "Logistics RCA on repeat-delay orders", "Logistics"),
    ("M5", "On OG EDD", "No Change", "On or Before Revised EDD",
     "Same as M1, triggered on delivery day instead", "Same fix as M1", "CX"),
    ("M6", "On OG EDD", "No Change", "After Revised EDD",
     "Same as M2, triggered on delivery day instead", "Same fix as M2", "Product / comms"),
    ("M7", "On OG EDD", "Postponed", "On or Before Revised EDD",
     "Same as M3, triggered on delivery day instead", "Same fix as M3", "Product / comms"),
    ("M8", "On OG EDD", "Postponed", "After Revised EDD",
     "Same as M4, triggered on delivery day instead", "Same fix as M4", "Logistics"),
    ("M9", "After OG EDD", "No Change (as of 1st contact)", "After Revised EDD",
     "Order was late and the system never once updated the promise across two touches",
     "Highest-priority logistics fix - total silence on a known-late order", "Logistics"),
    ("M10", "After OG EDD", "Postponed", "On or Before Revised EDD",
     "Customer needed extra reassurance even though the (revised) promise held",
     "L1 empowerment: firm new-date commitment + confirmation SMS", "CX"),
    ("M11", "After OG EDD", "Postponed", "After Revised EDD",
     "Order was already broken, got a new promise, and broke that promise too",
     "Containment SLA at Logistics", "Logistics"),
    ("M12", "After OG EDD", "No Change (as of 1st) -> Revised before 2nd", "On or Before Revised EDD",
     "Order was silently overdue at first contact, but caught up with a revised (and honored) "
     "promise before the customer followed up again",
     "Same underlying gap as M9 but self-corrected - tighten the trigger to be proactive",
     "Product / Logistics"),
    ("M0", "No EDD data", "N/A", "N/A",
     "Same data gap as S0, compounded by a repeat contact on an order with no trackable promise",
     "Same fix as S0, prioritized given the repeat-contact signal", "Product / Data"),
    ("MX", "Varies", "Pulled Early (order due sooner than originally promised)", "N/A",
     "Delivery moved up after the first contact - a good-news case, not a failure",
     "None required; optionally notify the customer the order is arriving early", "Logistics"),
]

FIRST_CONTACT_FILL = {"Before OG EDD": GREEN, "On OG EDD": YELLOW, "After OG EDD": ORANGE,
                       "No EDD data": GREY, "Varies": GREY}
EDD_CHANGE_FILL_PREFIX = {"No Change": YELLOW, "Postponed": ORANGE, "N/A": GREY, "Pulled": GREEN}
SECOND_CONTACT_FILL = {"On or Before Revised EDD": GREEN, "After Revised EDD": ORANGE,
                        "No Second contact": GREY, "N/A": GREY}


def fill_for(col, value):
    if col == "first":
        return FIRST_CONTACT_FILL.get(value, GREY)
    if col == "edd":
        for prefix, color in EDD_CHANGE_FILL_PREFIX.items():
            if value.startswith(prefix):
                return color
        return GREY
    if col == "second":
        return SECOND_CONTACT_FILL.get(value, GREY)


def position(contact_date, edd_date):
    if pd.isna(edd_date):
        return None
    if contact_date < edd_date:
        return "Within"
    if contact_date == edd_date:
        return "On"
    return "Post"


def assign_cohorts(group):
    total = len(group)
    og = group["og_day"].iloc[0]

    if total == 1:
        vo = group["vs_original"].iloc[0]
        if pd.isna(og):
            code = "S0"
        else:
            code = {"Within": "S1", "On": "S2", "Post": "S3"}.get(vo, "S0")
        return pd.Series([code] * total, index=group.index)

    g = group.sort_values(["contact_date", "active_day"], na_position="last")
    c1, c2 = g.iloc[0], g.iloc[1]

    if pd.isna(og):
        return pd.Series(["M0"] * total, index=group.index)

    c1_active = c1["active_day"]
    if pd.isna(c1_active):
        edd_state = "NoData"
    elif c1_active == og:
        edd_state = "NoChange"
    elif c1_active > og:
        edd_state = "Postponed"
    else:
        edd_state = "PulledEarly"

    breach = c2["vs_active_at_contact"] == "Post"
    c1_vo = c1["vs_original"]

    if edd_state == "NoData":
        code = "M0"
    elif edd_state == "PulledEarly":
        code = "MX"
    elif edd_state == "NoChange":
        if c1_vo == "Within":
            code = "M2" if breach else "M1"
        elif c1_vo == "On":
            code = "M6" if breach else "M5"
        else:
            code = "M9" if breach else "M12"
    else:  # Postponed
        if c1_vo == "Within":
            code = "M4" if breach else "M3"
        elif c1_vo == "On":
            code = "M8" if breach else "M7"
        else:
            code = "M11" if breach else "M10"

    return pd.Series([code] * total, index=group.index)


def autosize_and_freeze(writer, sheet_name, df):
    ws = writer.sheets[sheet_name]
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for i, col in enumerate(df.columns, start=1):
        width = min(60, max(10, int(df[col].astype(str).map(len).clip(upper=60).max() if len(df) else 10) + 2))
        ws.column_dimensions[get_column_letter(i)].width = width


def main():
    dedupable = pd.read_parquet(WINDOW_DIR / "dedupable_raw.parquet")
    sub = dedupable[dedupable["normalized_sub_disposition"].isin(TARGET_SUBDISPS)].copy()
    sub = sub.rename(columns={"final_order_id": "order_id"})
    sub["order_id"] = sub["order_id"].astype("int64")
    sub["created_at"] = pd.to_datetime(sub["created_at"])
    print(f"[18] contacts in scope: {len(sub)}")

    rev = pd.read_parquet(PIPELINE_ROOT / "raw" / "orders" / "edd_revisions_full_with_status.parquet")
    rev["order_id"] = rev["order_id"].astype("int64")
    rev["old_edd"] = pd.to_datetime(rev["old_edd"], format="mixed", errors="coerce")
    rev["new_edd"] = pd.to_datetime(rev["new_edd"], format="mixed", errors="coerce")
    rev["created_at"] = pd.to_datetime(rev["created_at"])
    rev = rev.dropna(subset=["order_id", "created_at"]).sort_values(["order_id", "created_at"])

    first_rev = rev.groupby("order_id").first().reset_index()
    first_rev["original_edd"] = first_rev["old_edd"].combine_first(first_rev["new_edd"])
    orig_map = first_rev.set_index("order_id")["original_edd"]
    last_rev = rev.groupby("order_id").last().reset_index()
    final_map = last_rev.set_index("order_id")["new_edd"]
    rev_count = rev.groupby("order_id").size().rename("edd_change_count")

    sub["original_edd"] = sub["order_id"].map(orig_map)
    sub["final_edd_ever"] = sub["order_id"].map(final_map)
    sub = sub.merge(rev_count, on="order_id", how="left")
    sub["edd_change_count"] = sub["edd_change_count"].fillna(0).astype(int)

    fallback = pd.read_parquet(PIPELINE_ROOT / "raw" / "orders" / "order_analytics_edd_fallback.parquet")
    fallback_map = fallback.set_index("order_id")[["original_edd", "final_edd_ever"]]
    no_revision = sub["original_edd"].isna()
    sub.loc[no_revision, "original_edd"] = sub.loc[no_revision, "order_id"].map(fallback_map["original_edd"])
    sub.loc[no_revision, "final_edd_ever"] = sub.loc[no_revision, "order_id"].map(fallback_map["final_edd_ever"])

    rev_sorted = rev.sort_values("created_at")
    sub_sorted = sub.sort_values("created_at")
    merged = pd.merge_asof(
        sub_sorted, rev_sorted[["order_id", "created_at", "new_edd", "order_status_id"]],
        on="created_at", by="order_id", direction="backward", allow_exact_matches=True,
    )
    merged["active_edd_at_contact"] = merged["new_edd"].combine_first(merged["original_edd"])

    status_master = pd.read_csv(PIPELINE_ROOT / "reference" / "order_status_master.csv")
    status_master = status_master.drop_duplicates(subset="status_id", keep="first")
    status_map = status_master.set_index("status_id")[["status_name", "customer_status"]]
    merged = merged.merge(status_map, left_on="order_status_id", right_index=True, how="left")

    merged["contact_date"] = merged["created_at"].dt.normalize()
    merged["original_edd_date"] = merged["original_edd"].dt.normalize()
    merged["active_edd_date"] = merged["active_edd_at_contact"].dt.normalize()
    merged["vs_original"] = [position(c, e) for c, e in zip(merged["contact_date"], merged["original_edd_date"])]
    merged["vs_active_at_contact"] = [position(c, e) for c, e in zip(merged["contact_date"], merged["active_edd_date"])]

    order_contact_counts = merged.groupby("order_id").size().rename("total_contacts_for_order")
    merged = merged.merge(order_contact_counts, on="order_id", how="left")

    edd_change_days = (merged["final_edd_ever"] - merged["original_edd"]).dt.days
    edd_change_label = pd.Series(pd.NA, index=merged.index, dtype="object")
    edd_change_label[edd_change_days > 0] = "Postponed"
    edd_change_label[edd_change_days < 0] = "Pulled Early"
    edd_change_label[edd_change_days == 0] = "No Change"

    # --- cohort assignment (day-granularity comparisons; see module docstring) ---
    merged["og_day"] = merged["original_edd_date"]
    merged["active_day"] = merged["active_edd_date"]
    merged["cohort"] = merged.groupby("order_id", group_keys=False).apply(assign_cohorts, include_groups=False)

    n_distinct_per_order = merged.groupby("order_id")["cohort"].nunique()
    assert (n_distinct_per_order == 1).all(), "some order got >1 cohort code across its rows"
    assert merged["cohort"].notna().all(), "some row has no cohort code"

    final = merged[[
        "order_id", "original_edd", "final_edd_ever", "edd_change_count", "contact_date",
        "total_contacts_for_order", "normalized_sub_disposition", "channel", "sub_channel",
        "order_status_id", "status_name", "customer_status", "active_edd_at_contact",
        "vs_original", "vs_active_at_contact", "cohort",
    ]].rename(columns={
        "order_id": "Order ID",
        "original_edd": "Original EDD (first created)",
        "final_edd_ever": "Final EDD (last changed, ever)",
        "edd_change_count": "EDD Change Count",
        "contact_date": "Contact Made (date)",
        "total_contacts_for_order": "Total Contacts for this Order",
        "normalized_sub_disposition": "Shipment Status / Delivery Delay",
        "channel": "Channel",
        "sub_channel": "Sub-Channel",
        "order_status_id": "Order Stage ID at Contact",
        "status_name": "Order Stage (internal)",
        "customer_status": "Order Stage (customer-facing)",
        "active_edd_at_contact": "Active EDD at Time of Contact",
        "vs_original": "Within/On/Post (vs Original EDD)",
        "vs_active_at_contact": "Within/On/Post (vs EDD at Time of Contact)",
        "cohort": "Cohort",
    }).sort_values(["Order ID", "Contact Made (date)"])

    out_path = PIPELINE_ROOT / "output" / "PRX_EDD_Raw_Export_with_Cohort_2026-05-01_to_2026-08-22.xlsx"
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        final.to_excel(writer, sheet_name="Contact-Level Detail", index=False)
        autosize_and_freeze(writer, "Contact-Level Detail", final)
    print(f"[18] wrote {out_path} ({len(final)} rows)")

    # --- cohort definitions summary ---
    counts = final["Cohort"].value_counts()
    total = len(final)
    def_path = PIPELINE_ROOT / "output" / "PRX_Cohort_Definitions_Rebuilt.xlsx"
    with pd.ExcelWriter(def_path, engine="openpyxl") as writer:
        headers = ["#", "First contact", "EDD change", "Second+ contact", "Count",
                   "Overall share", "Root cause", "Fix", "Owner"]
        wb = writer.book
        ws = wb.create_sheet("Cohort Definitions")
        for leftover in [s for s in wb.sheetnames if s != "Cohort Definitions"]:
            del wb[leftover]

        ws.merge_cells("A1:I1")
        ws["A1"] = "PRX CPO Reduction | 'delayed_or_likely_to_be_delayed' & 'shipment_status_query' (rebuilt, EDD-fallback fix applied)"
        ws["A1"].font = Font(bold=True, size=13, color="632423")
        ws["A2"] = "Cohorts Mx - multi-contact orders (contact 1 vs contact 2 only) | Cohorts Sx - single-contact orders"
        ws["A2"].font = Font(italic=True, size=9)

        header_row = 4
        for c, h in enumerate(headers, start=1):
            cell = ws.cell(row=header_row, column=c, value=h)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor=HEADER_FILL)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        row_i = header_row + 1
        for code, first, edd, second, cause, fix, owner in ROWS:
            count = int(counts.get(code, 0))
            share = count / total
            vals = [code, first, edd, second, count, share, cause, fix, owner]
            for c, v in enumerate(vals, start=1):
                cell = ws.cell(row=row_i, column=c, value=v)
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                if c == 2:
                    cell.fill = PatternFill("solid", fgColor=fill_for("first", first))
                elif c == 3:
                    cell.fill = PatternFill("solid", fgColor=fill_for("edd", edd))
                elif c == 4:
                    cell.fill = PatternFill("solid", fgColor=fill_for("second", second))
                elif c == 6:
                    cell.number_format = "0.00%"
            row_i += 1

        total_row = row_i
        ws.cell(row=total_row, column=1, value="TOTAL").font = Font(bold=True)
        ws.cell(row=total_row, column=5, value=int(counts.sum())).font = Font(bold=True)
        tc = ws.cell(row=total_row, column=6, value=1.0)
        tc.number_format = "0.00%"
        tc.font = Font(bold=True)

        widths = [5, 14, 26, 22, 8, 12, 42, 42, 16]
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = f"A{header_row + 1}"

    print(f"[18] wrote {def_path}")
    print(counts.reindex([r[0] for r in ROWS]))
    print("TOTAL:", int(counts.sum()), "/", total)


if __name__ == "__main__":
    main()
