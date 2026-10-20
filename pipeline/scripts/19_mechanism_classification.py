"""Stage 19: Mechanism classification -- collapse the 12 M-cohorts (M1-M12, MX, M0) from Stage 18
into a small set of MECE customer-journey failure MECHANISMS, so a handful of fixes can cover all
cohorts instead of needing "one fix per cohort."

Reads:  output/PRX_EDD_Raw_Export_with_Cohort_2026-05-01_to_2026-08-22.xlsx ("Contact-Level Detail")
Writes: output/PRX_Mechanism_Classification_2026-05-01_to_2026-08-22.xlsx
        ("Contact-Level Detail" + "Mechanism Pareto" + "Mechanism x Channel Discontinuity")

MECHANISM MAPPING (1-3 derived straight from each cohort's documented root cause in
16_cohort_definitions_table.py / 18_cohort_assignment.py's ROWS table -- see module docstring
there for the full first-contact / EDD-change / second-contact logic behind each code):

  1. Expectation/promise breach   -- EDD was revised after contact 1, and the revised EDD was
     ALSO missed by contact 2.                                    cohorts: M4, M8, M11
  2. Information decay / system silence -- EDD never updated even though a real delay was
     happening; customer left in the dark.                       cohorts: M2, M6, M9, M12
  3. Reassurance failure -- EDD unchanged or already-adjusted-and-holding; contact 2 happened
     even though nothing was factually wrong, just needed reassurance/confirmation.
                                                                   cohorts: M1, M3, M5, M7, M10
  Out of scope (tagged, not folded into 1-3):
     M0 -- "Out of scope: no EDD data"
     MX -- "Out of scope: good news"
  S-cohorts (single-contact orders) are out of scope for this whole exercise -- Mechanism is left
     as "N/A -- single contact, not applicable".

CROSS-CUTTING SIGNALS (do not map from cohort code -- computed directly from the data):

  5. Channel/context loss -- IS computable here: for every multi-contact order, compare contact 1
     vs contact 2 (sorted by "Contact Made (date)"; ties broken stably, matching Stage 18's own
     first/second-contact selection). `channel_discontinuous` = True if contact 2's Channel OR
     Sub-Channel differs from contact 1's. Applied order-wide (every contact row for that order
     gets the same flag), same convention Stage 18 uses for `Cohort` itself.

  4. Resolution failure (did contact 1 give correct/current info) -- investigated against every
     already-computed candidate signal in intermediate/run_2026-05-01_2026-08-22/ and
     reference/disposition_actionability.csv. None of them measure resolution *quality*:
       - resolution_by_order.parquet / resolution_by_issue.parquet: `n_resolutions` /
         `touches_per_resolution` are purely a 48h SILENCE-GAP heuristic (see 09_resolution_fcr.py
         -- "Resolved = no further contact ... within 48 hours"). That measures time-until-next-
         contact, not whether contact 1's answer was correct. Explicitly not a quality proxy.
       - limechat_classified.parquet's `is_resolved?` looked promising but empirically doesn't
         discriminate: True for 413,684/413,710 tickets overall (99.994%) and 81,540/81,547
         (99.99%) even restricted to EDD/shipment-status tickets. It really means "ticket got
         closed," which happens almost unconditionally -- it carries no information about whether
         the given ETA/status was actually correct or held up.
       - calls_classified.parquet only carries call-connectivity fields (`is_connected`,
         `is_completed`), not answer correctness.
       - reference/disposition_actionability.csv classifies whether a DISPOSITION CODE is
         actionable by an agent in general -- a taxonomy property, not a per-contact outcome.
     Conclusion: no field in the instrumented pipeline outputs distinguishes "contact 1 resolved
     the question correctly" from "contact 1 didn't." Per the brief, we do NOT invent a fake
     proxy. Mechanism-4 status is tagged "Not instrumented -- needs transcript-level review" for
     every M-cohort contact instead.
"""
import pandas as pd
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

from pipeline_utils import PIPELINE_ROOT

SRC_PATH = PIPELINE_ROOT / "output" / "PRX_EDD_Raw_Export_with_Cohort_2026-05-01_to_2026-08-22.xlsx"
OUT_PATH = PIPELINE_ROOT / "output" / "PRX_Mechanism_Classification_2026-05-01_to_2026-08-22.xlsx"

HEADER_FILL = "632423"
NA_LABEL = "N/A -- single contact, not applicable"
OOS_NO_EDD = "Out of scope: no EDD data"
OOS_GOOD_NEWS = "Out of scope: good news"
RES4_NOT_INSTRUMENTED = "Not instrumented -- needs transcript-level review"
RES4_NA = "N/A -- single contact, not applicable"

COHORT_TO_MECHANISM = {}
for c in ["M4", "M8", "M11"]:
    COHORT_TO_MECHANISM[c] = "1. Expectation/promise breach"
for c in ["M2", "M6", "M9", "M12"]:
    COHORT_TO_MECHANISM[c] = "2. Information decay / system silence"
for c in ["M1", "M3", "M5", "M7", "M10"]:
    COHORT_TO_MECHANISM[c] = "3. Reassurance failure"
COHORT_TO_MECHANISM["M0"] = OOS_NO_EDD
COHORT_TO_MECHANISM["MX"] = OOS_GOOD_NEWS

M_COHORT_CODES = set(COHORT_TO_MECHANISM.keys())


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


def compute_channel_discontinuity(df):
    """For every multi-contact (M-cohort) order, compare contact 1 vs contact 2 (sorted by
    Contact Made (date), matching Stage 18's own first/second-contact selection convention) and
    flag whether Channel or Sub-Channel changed. Applied order-wide: every row for that order
    gets the same flag. Returns a Series aligned to df.index (NaN for S-cohort / non-M rows).
    """
    flag = pd.Series(pd.NA, index=df.index, dtype="object")
    m_df = df[df["Cohort"].isin(M_COHORT_CODES)]
    for order_id, g in m_df.groupby("Order ID"):
        g = g.sort_values("Contact Made (date)", kind="stable")
        if len(g) < 2:
            # Shouldn't happen for M-cohorts (multi-contact by definition), but guard anyway.
            flag.loc[g.index] = False
            continue
        c1, c2 = g.iloc[0], g.iloc[1]
        discontinuous = (c1["Channel"] != c2["Channel"]) or (c1["Sub-Channel"] != c2["Sub-Channel"])
        # Treat NaN-vs-NaN as "not discontinuous" (nothing to compare), any other NaN mismatch as True.
        if pd.isna(c1["Channel"]) and pd.isna(c2["Channel"]):
            chan_diff = False
        else:
            chan_diff = c1["Channel"] != c2["Channel"]
        if pd.isna(c1["Sub-Channel"]) and pd.isna(c2["Sub-Channel"]):
            sub_diff = False
        else:
            sub_diff = c1["Sub-Channel"] != c2["Sub-Channel"]
        discontinuous = bool(chan_diff or sub_diff)
        flag.loc[g.index] = discontinuous
    return flag


def main():
    df = pd.read_excel(SRC_PATH, sheet_name="Contact-Level Detail")
    print(f"[19] loaded {len(df)} contact rows from {SRC_PATH.name}")

    # --- Mechanism (1-3 + out-of-scope tags), from cohort code ---
    df["Mechanism"] = df["Cohort"].map(COHORT_TO_MECHANISM).fillna(NA_LABEL)

    # --- Mechanism 5: channel/context loss ---
    df["channel_discontinuous"] = compute_channel_discontinuity(df)

    # --- Mechanism 4: resolution failure -- not instrumented (see module docstring) ---
    is_m_cohort = df["Cohort"].isin(M_COHORT_CODES)
    df["mechanism_4_resolution_signal"] = RES4_NA
    df.loc[is_m_cohort, "mechanism_4_resolution_signal"] = RES4_NOT_INSTRUMENTED

    with pd.ExcelWriter(OUT_PATH, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Contact-Level Detail", index=False)
        autosize_and_freeze(writer, "Contact-Level Detail", df)
        style_header(writer, "Contact-Level Detail", len(df.columns))

        # --- Summary sheet 1: Mechanism Pareto (M-cohort contacts only) ---
        m_df = df[is_m_cohort].copy()
        total_m = len(m_df)
        pareto = (
            m_df["Mechanism"].value_counts().rename_axis("Mechanism").reset_index(name="Contact Count")
        )
        pareto["Share of M-cohort Contacts"] = pareto["Contact Count"] / total_m
        pareto = pareto.sort_values("Contact Count", ascending=False).reset_index(drop=True)
        total_row = pd.DataFrame([{
            "Mechanism": "TOTAL (M-cohort contacts)", "Contact Count": total_m,
            "Share of M-cohort Contacts": 1.0,
        }])
        pareto_out = pd.concat([pareto, total_row], ignore_index=True)
        pareto_out.to_excel(writer, sheet_name="Mechanism Pareto", index=False)
        ws = writer.sheets["Mechanism Pareto"]
        for r in range(2, len(pareto_out) + 2):
            ws.cell(row=r, column=3).number_format = "0.00%"
        ws.cell(row=len(pareto_out) + 1, column=1).font = Font(bold=True)
        ws.cell(row=len(pareto_out) + 1, column=2).font = Font(bold=True)
        ws.cell(row=len(pareto_out) + 1, column=3).font = Font(bold=True)
        autosize_and_freeze(writer, "Mechanism Pareto", pareto_out)
        style_header(writer, "Mechanism Pareto", len(pareto_out.columns))

        # --- Summary sheet 2: Mechanism x channel_discontinuous cross-tab ---
        ct_counts = pd.crosstab(m_df["Mechanism"], m_df["channel_discontinuous"].astype(bool))
        ct_counts = ct_counts.reindex(columns=[False, True], fill_value=0)
        ct_counts.columns = ["channel_discontinuous=False (count)", "channel_discontinuous=True (count)"]
        ct_counts["Row Total"] = ct_counts.sum(axis=1)
        ct_counts["Share Discontinuous"] = (
            ct_counts["channel_discontinuous=True (count)"] / ct_counts["Row Total"]
        )
        ct_counts = ct_counts.sort_values("Row Total", ascending=False)
        ct_out = ct_counts.reset_index()
        overall_true = m_df["channel_discontinuous"].sum()
        overall_total = len(m_df)
        overall_row = pd.DataFrame([{
            "Mechanism": "TOTAL (M-cohort contacts)",
            "channel_discontinuous=False (count)": overall_total - overall_true,
            "channel_discontinuous=True (count)": overall_true,
            "Row Total": overall_total,
            "Share Discontinuous": overall_true / overall_total,
        }])
        ct_out = pd.concat([ct_out, overall_row], ignore_index=True)
        ct_out.to_excel(writer, sheet_name="Mechanism x Channel Disc.", index=False)
        ws2 = writer.sheets["Mechanism x Channel Disc."]
        for r in range(2, len(ct_out) + 2):
            ws2.cell(row=r, column=5).number_format = "0.00%"
        for c in range(1, 6):
            ws2.cell(row=len(ct_out) + 1, column=c).font = Font(bold=True)
        autosize_and_freeze(writer, "Mechanism x Channel Disc.", ct_out)
        style_header(writer, "Mechanism x Channel Disc.", len(ct_out.columns))

    print(f"[19] wrote {OUT_PATH}")
    print("[19] Mechanism Pareto (M-cohort contacts):")
    print(pareto_out.to_string(index=False))
    print("[19] Mechanism x channel_discontinuous cross-tab:")
    print(ct_out.to_string(index=False))


if __name__ == "__main__":
    main()
