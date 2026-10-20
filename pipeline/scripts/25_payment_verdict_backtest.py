"""
Stage-0 backtest for the payment/refund verdict logic
(see Payment_Refund_Resolution_Logic_Spec.md).

Inputs (extracted via ClickHouse MCP, see README_clickhouse_queries.md style):
  intermediate/payment_backtest/ledger_carts.csv   one row per payments cart (UTC)
  intermediate/payment_backtest/phone_uid.csv      phone -> user_id
  intermediate/payment_backtest/contacts.csv       support contacts (IST) for
                                                    amount_deducted_twice / amount_deducted_but_order_not_placed
Outputs:
  intermediate/payment_backtest/backtest_contacts.csv   one row per contact with verdict + journey outcome
  prints a scorecard.

Point-in-time rule: the verdict is computed only from facts that were true at the contact
time Tc; the outcome is read from what happened afterwards (data end = DATA_END).
"""
import os
import numpy as np
import pandas as pd

BASE = os.path.join(os.path.dirname(__file__), "..", "intermediate", "payment_backtest")
DATA_END = pd.Timestamp("2026-10-07 00:00:00")
IST_OFFSET = pd.Timedelta(hours=5, minutes=30)
LOOKBACK = pd.Timedelta(days=10)       # carts considered for a contact
PENDING_T = pd.Timedelta(minutes=30)   # T_pending
ORDER_GRACE = pd.Timedelta(minutes=60)  # order appears <=60min after capture in 99.5% of carts (measured)
REFUND_SLA = pd.Timedelta(hours=24)     # population (Aug1-Sep19, order-sized no-order captures): 69/71 refunded, p90 6 min, p99 17h. A refund that has not appeared by 24h is not coming on its own.
SIBLING_WINDOW = pd.Timedelta(minutes=60)
EXCLUDED_AMOUNTS = {9.0, 59.0, 79.0}   # fixed-price non-order payments, out of V1 scope
LIFECYCLE_FLOWS = {"CANCELLED", "RTO_IN_TRANSIT", "RTO_DELIVERED", "LOST", "LOST_DISPUTED_MANIFEST",
                   "FAKE_DELIVERED", "return", "return-online", "refund_against_order",
                   "order_edit", "mrp_qc_update"}


def load():
    L = pd.read_csv(os.path.join(BASE, "ledger_carts.csv"))
    for c in ["t_first", "t_last", "t_cap", "t_ref", "t_ref_upd"]:
        L[c] = pd.to_datetime(L[c], errors="coerce")
    L.loc[L.t_cap < pd.Timestamp("2000-01-01"), "t_cap"] = pd.NaT
    # LEFT JOIN misses come back from ClickHouse as epoch-0 / 0 / '' -> treat as "no refund"
    L.loc[(L.n_ref.fillna(0) == 0) | (L.t_ref < pd.Timestamp("2000-01-01")), ["t_ref", "t_ref_upd"]] = pd.NaT
    L["n_ref"] = L["n_ref"].fillna(0)
    L["ref_amt"] = np.where(L.n_ref == 0, 0.0, L.ref_amt)
    L["rstat"] = np.where(L.n_ref == 0, "", L.rstat.fillna(""))
    L["rflows"] = L["rflows"].fillna("[]")
    L["fcodes"] = L["fcodes"].fillna("[]")
    # Order linkage must come from marketplace_orders.user_cart_id, NOT payments_online_transaction.order_id:
    # the txn row is often never back-filled (19 of 23 "stuck no-order" carts in the first backtest had delivered orders).
    O = pd.read_csv(os.path.join(BASE, "orders_by_cart.csv"))
    O["order_created"] = pd.to_datetime(O["order_created"], errors="coerce")
    O = O.groupby("cart_id").agg(mo_order_id=("order_id", "first"), mo_status=("status_id", "max"),
                                 mo_created=("order_created", "min")).reset_index()
    L = L.merge(O, on="cart_id", how="left")
    P = pd.read_csv(os.path.join(BASE, "phone_uid.csv"), dtype=str)
    C = pd.read_csv(os.path.join(BASE, "contacts.csv"), dtype=str)
    # Calls are stored in UTC; LimeChat / Email are IST (verified against Sherlock I765: call 11:45 UTC-paid vs chat 12:12 IST)
    C["tc"] = pd.to_datetime(C["created_at"]) - np.where(C["channel"] == "Calls", pd.Timedelta(0), IST_OFFSET)
    return L, P, C


def verdict(r, tc, siblings=None):
    """Return (verdict, liability) using only facts known at tc."""
    cap_amt = r.cap_amt
    PENDING_CONFIRM = {0, 2, 3}      # order shell exists, awaiting Rx / doctor confirmation
    NOT_AN_ORDER = {4, 81, 82}      # rx rejected / hidden-cancelled: money is owed back
    linked = pd.notna(r.mo_order_id) and pd.notna(r.mo_created) and r.mo_created <= tc + pd.Timedelta(minutes=1)
    st = r.mo_status if linked else np.nan
    txn_linked = (r.n_orders > 0) and pd.notna(r.t_cap) and r.t_cap <= tc   # order_id stamped on the payment row
    mo_ok = linked and (st not in NOT_AN_ORDER) and (st not in PENDING_CONFIRM)
    has_order = bool(txn_linked or mo_ok)   # EITHER side of the link counts; neither alone is complete
    pending_confirm = linked and (st in PENDING_CONFIRM) and not txn_linked
    ref_at = pd.notna(r.t_ref) and r.t_ref <= tc
    n_cap = r.n_cap
    open_unresolved = r.n_open > 0 and (tc - r.t_last) < PENDING_T

    if n_cap == 0:
        # online attempt(s) never captured but an order exists -> customer switched to COD / another mode (40k carts since Aug)
        if has_order or (pending_confirm and not np.isnan(r.mo_status)):
            return ("P2c_online_not_completed_order_placed_COD", "NONE")
        # failed here, but the customer paid successfully on another cart within the hour
        if siblings is not None and len(siblings):
            ok = siblings[(siblings.cart_id != r.cart_id) & (siblings.n_cap > 0) & ((siblings.t_first - r.t_first).abs() <= SIBLING_WINDOW)]
            if len(ok):
                return ("P1s_failed_here_paid_on_other_cart", "NONE_BANK")
        if r.n_open > 0 and r.n_fail == 0:
            return ("P3_pending_unknown", "UNKNOWN") if open_unresolved else ("P2_failed_only", "BANK")
        if r.n_fail > 0:
            return ("P2_failed_only", "UNKNOWN" if open_unresolved else "BANK")
        return ("P0_no_payment_activity", "UNKNOWN")

    if pending_confirm and n_cap == 1:
        return ("P4b_paid_order_awaiting_confirmation", "NONE")
    if not has_order and n_cap == 1 and round(cap_amt, 2) in EXCLUDED_AMOUNTS:
        return ("P7_excluded_non_order", "OUT_OF_SCOPE")

    if n_cap >= 2:
        extra = cap_amt - (cap_amt / n_cap if has_order else 0.0)
        tol = max(5.0, 0.01 * extra)   # duplicate captures on one cart can differ by a few rupees (cart revised between attempts)
        if ref_at and r.ref_amt >= extra - tol:
            return ("P8_dup_with_order_refunded" if has_order else "P9_dup_no_order_refunded", "PRX_IN_FLIGHT")
        return ("P8_dup_with_order" if has_order else "P9_dup_no_order", "PRX_OVERDUE")

    # exactly one capture
    if has_order:
        if ref_at:
            flows = set(eval(r.rflows)) if isinstance(r.rflows, str) else set()
            if flows & LIFECYCLE_FLOWS:
                return ("R_lifecycle_refund", "PRX_IN_FLIGHT")
            return ("R_other_refund", "PRX_IN_FLIGHT")
        if r.n_fail > 0:
            return ("P1_failed_then_paid", "NONE_BANK")
        if siblings is not None and len(siblings):
            sib = siblings[(siblings.cart_id != r.cart_id) & (siblings.n_cap == 0) & (siblings.n_fail > 0)
                           & ((siblings.t_first - r.t_first).abs() <= SIBLING_WINDOW)]
            if len(sib):
                return ("P1s_failed_attempt_on_other_cart", "NONE_BANK")
        return ("P4_paid_order_ok", "NONE")
    if ref_at:
        return ("P5_paid_no_order_refund_started", "PRX_IN_FLIGHT")
    age = tc - r.t_cap if pd.notna(r.t_cap) else pd.Timedelta(0)
    if age < ORDER_GRACE:
        return ("P6a_order_being_created", "NONE")
    if age < REFUND_SLA:
        return ("P6b_no_order_auto_refund_expected", "PRX_EXPECTED")
    return ("P6_paid_no_order_no_refund", "PRX_OVERDUE")


SEVERITY = {  # which cart to anchor a contact on when several are in the lookback
    "P9_dup_no_order": 9, "P8_dup_with_order": 9, "P6_paid_no_order_no_refund": 8,
    "P9_dup_no_order_refunded": 7, "P8_dup_with_order_refunded": 7, "P5_paid_no_order_refund_started": 6,
    "P3_pending_unknown": 5, "P1_failed_then_paid": 5, "P2_failed_only": 5,
    "R_lifecycle_refund": 3, "R_other_refund": 3, "P4_paid_order_ok": 1, "P1s_failed_attempt_on_other_cart": 5, "P1s_failed_here_paid_on_other_cart": 5, "P2c_online_not_completed_order_placed_COD": 3, "P6a_order_being_created": 4, "P6b_no_order_auto_refund_expected": 6, "P4b_paid_order_awaiting_confirmation": 2,
    "P7_excluded_non_order": 0, "P0_no_payment_activity": 0,
}


def outcome(r, tc, v):
    """What actually happened after tc for this cart; and whether it matches the verdict's claim."""
    ref_after = pd.notna(r.t_ref) and r.t_ref > tc
    ref_ever = r.n_ref > 0 if pd.notna(r.n_ref) else False
    ref_done = r.rstat == "REFUND_PROCESSED"
    if v.startswith("P1") or v.startswith("P2") or v.startswith("P4"):  # incl. P1s, P2c, P4b
        # claim: nothing owed by PRX. Contradicted if a refund for this cart appears after contact.
        if ref_after and not (set(eval(r.rflows)) & LIFECYCLE_FLOWS):
            return "CONTRADICTED_refund_issued_after"
        return "CONFIRMED_no_refund_needed"
    if v.startswith("P6a"):
        return "CONFIRMED_order_created" if (pd.notna(r.mo_created) and r.mo_created > tc) else "UNRESOLVED_no_order_no_refund"
    if v.startswith("P6"):
        # claim: PRX holds money, refund overdue / expected.
        if pd.notna(r.mo_created) and r.mo_created > tc and r.mo_status not in (4, 81, 82):
            return "RESOLVED_order_created_later"
        if ref_after and ref_done:
            return "CONFIRMED_refund_followed"
        if ref_after:
            return "CONFIRMED_refund_started_not_done"
        return "UNRESOLVED_no_refund_by_data_end"
    if v.startswith("P8") or v.startswith("P9"):
        if v.endswith("refunded"):
            return "CONFIRMED_already_refunded"
        if ref_after and ref_done:
            return "CONFIRMED_refund_followed"
        if ref_after:
            return "CONFIRMED_refund_started_not_done"
        return "UNRESOLVED_no_refund_by_data_end"
    if v.startswith("P5"):
        return "CONFIRMED_refund_done" if ref_done else "STUCK_refund_not_processed"
    if v.startswith("R_"):
        return "CONFIRMED_refund_done" if ref_done else "STUCK_refund_not_processed"
    if v.startswith("P3"):
        return "RESOLVED_to_final_state" if (r.n_open == 0 or r.n_cap > 0) else "STILL_PENDING"
    return "n/a"


def main():
    L, P, C = load()
    pu = P.drop_duplicates().groupby("phone_number")["uid"].apply(lambda s: [int(x) for x in s]).to_dict()
    L["uid"] = L["uid"].astype(int)
    by_uid = {u: g for u, g in L.groupby("uid")}
    rows = []
    for _, c in C.iterrows():
        uids = pu.get(c.phone_normalized, [])
        cands = []
        for u in uids:
            g = by_uid.get(u)
            if g is None:
                continue
            g = g[(g.t_first <= c.tc) & (g.t_first >= c.tc - LOOKBACK)]
            for _, r in g.iterrows():
                v, liab = verdict(r, c.tc, by_uid.get(r.uid))
                cands.append((SEVERITY.get(v, 0), r, v, liab))
        oid0 = pd.to_numeric(c.final_order_id, errors="coerce")
        if pd.notna(oid0):
            for u in uids:
                g = by_uid.get(u)
                if g is None:
                    continue
                for _, r in g[g.oid == oid0].iterrows():
                    if r.t_first <= c.tc and not any(x[1].cart_id == r.cart_id for x in cands):
                        v, liab = verdict(r, c.tc, by_uid.get(r.uid))
                        cands.append((SEVERITY.get(v, 0), r, v, liab))
        if not cands:
            rows.append(dict(contact_id=c.contact_id, sub=c.normalized_sub_disposition, tc=c.tc, verdict="NO_CART_IN_WINDOW",
                             liability="UNKNOWN", outcome="n/a", cart_id=np.nan))
            continue
        # prefer a cart tied to the contact's own order id, else highest severity
        oid = pd.to_numeric(c.final_order_id, errors="coerce")
        tied = [x for x in cands if pd.notna(oid) and x[1].oid == oid]
        sev, r, v, liab = (tied or sorted(cands, key=lambda x: -x[0]))[0] if tied else sorted(cands, key=lambda x: -x[0])[0]
        rows.append(dict(contact_id=c.contact_id, sub=c.normalized_sub_disposition, channel=None, tc=c.tc,
                         cart_id=r.cart_id, n_carts_in_window=len(cands), verdict=v, liability=liab,
                         n_fail=r.n_fail, n_cap=r.n_cap, cap_amt=r.cap_amt, n_orders=r.n_orders,
                         n_ref=r.n_ref, ref_amt=r.ref_amt, t_first=r.t_first, t_ref=r.t_ref,
                         outcome=outcome(r, c.tc, v), tied_to_order=bool(tied)))
    out = pd.DataFrame(rows)
    out.to_csv(os.path.join(BASE, "backtest_contacts.csv"), index=False)

    print("contacts:", len(out), "| by sub-disposition:\n", out["sub"].value_counts().to_string())
    for s, g in out.groupby("sub"):
        print(f"\n===== {s} (n={len(g)}) =====")
        print(pd.crosstab(g.verdict, g.outcome).to_string())
        print("liability mix:\n", g.liability.value_counts().to_string())
    return out


if __name__ == "__main__":
    main()
