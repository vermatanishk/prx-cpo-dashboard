"""Stage 27: MECE decomposition of the IPI uplift for the whole 'return_and_pickup' disposition (v5 run).

Same method as stage 26 (missing_or_less_quantity_received) but the unit is every Return & Pickup contact on an order
(all sub-dispositions pooled), so downstream tickets (pickup delayed, refund chase tagged here) are visible.

Unit = repeat contact (2nd+ Return & Pickup contact on an order). Each gets exactly one family, state-first:
  G  residual ghost / F agent callback (Agent-OB task status != 20 / == 20)
  I1 return never created on the order      I2 return created later than the previous genuine contact
  II double-touch while return being created
  III raised (200/210), awaiting processing  IV cancelled in backend (280)
  V  later stage: approved / pickup / warehouse-QC / refund / post-refund

Reads : intermediate/run_2026-05-01_2026-10-01/{support_contacts,calls_agent_outbound_task_status}.parquet
        raw/orders/return_events_all_since_2026-04-20.parquet  (status log, return/refund ids, all orders)
Writes: output/PRX_ReturnPickup_RepeatContacts_MECE_v5.csv  (one row per contact)
"""
import numpy as np, pandas as pd
from pipeline_utils import PIPELINE_ROOT

R = PIPELINE_ROOT / "intermediate" / "run_2026-05-01_2026-10-01"
DISP = "return_and_pickup"
TOL = pd.Timedelta(minutes=30)
RAISE = {200, 210}


def staged(s):
    if s == 280: return "IV cancelled"
    if s in RAISE: return "III awaiting"
    if s == 220: return "V approved, pickup not scheduled"
    if s in (230, 231, 233): return "V pickup scheduled/failed"
    if s in (232, 234, 236): return "V picked up / in transit"
    if s in (238, 240, 244, 245, 246, 247, 250, 270): return "V warehouse / QC"
    if s in (260, 262): return "V refund in progress"
    if s == 265: return "V post-refund"
    return "V other"


def load():
    c = pd.read_parquet(R / "support_contacts.parquet")
    c = c[(c.normalized_disposition == DISP) & c.final_order_id.notna()].copy()
    c["oid"] = c.final_order_id.astype("int64")
    c["created_at"] = pd.to_datetime(c.created_at)
    ts = pd.read_parquet(R / "calls_agent_outbound_task_status.parquet")
    ts["task_created_at"] = pd.to_datetime(ts.task_created_at)
    c = c.sort_values("created_at")
    c = pd.merge_asof(c, ts.sort_values("task_created_at")[["task_created_at", "task_id", "task_status"]],
                      left_on="created_at", right_on="task_created_at", direction="nearest",
                      tolerance=pd.Timedelta(seconds=1))
    c["is_ob"] = c.sub_channel.str.startswith("Agent OB")
    c.loc[~c.is_ob, "task_status"] = np.nan
    c["ghost"] = c.is_ob & (c.task_status != 20)
    c = c.sort_values(["oid", "created_at"]).reset_index(drop=True)
    ev = pd.read_parquet(PIPELINE_ROOT / "raw/orders/return_events_all_since_2026-04-20.parquet")
    ev = {o: g.sort_values("created_at") for o, g in ev.groupby("order_id")}
    return c, ev


def classify(c, ev):
    c = c.copy()
    c["k"] = c.groupby("oid").cumcount() + 1
    c["prev_sub"] = c.groupby("oid").normalized_sub_disposition.shift(1)
    c["prev_sc"] = c.groupby("oid").sub_channel.shift(1)
    fam, sub, state, wait_h = [], [], [], []
    for oid, g in c.groupby("oid", sort=False):
        gen = []
        e = ev.get(oid)
        for r in g.itertuples():
            st, w = "", np.nan
            if e is not None:
                p = e[e.created_at <= r.created_at]
                if len(p):
                    st = staged(int(p.iloc[-1].current_status_id))
                    if st.startswith("III"):
                        w = (r.created_at - p[p.current_status_id.isin(RAISE)].created_at.max()).total_seconds() / 3600
            state.append(st); wait_h.append(w)
            if r.k == 1:
                fam.append("first contact"); sub.append("")
            elif r.ghost:
                fam.append("G residual ghost"); sub.append("ghost task")
            elif not gen:
                fam.append("G residual ghost"); sub.append("earlier contacts all ghost")
            elif r.is_ob:
                fam.append("F agent callback (status 20)"); sub.append("")
            else:
                prev_t = gen[-1]
                if e is None or len(e) == 0:
                    fam.append("I1 return never created"); sub.append("")
                else:
                    rr = e[e.current_status_id.isin(RAISE)]
                    r_time = rr.created_at.min() if len(rr) else e.created_at.min()
                    if r.created_at <= r_time + TOL:
                        if r_time > prev_t + TOL:
                            fam.append("I2 return created after earlier contact")
                            sub.append("redial <=30 min" if (r.created_at - prev_t) <= TOL else "re-contact >30 min")
                        else:
                            fam.append("II double-touch while return being created"); sub.append("")
                    else:
                        s = st
                        fam.append(s.split(" ")[0] + " " + (s.split(" ", 1)[1] if s.startswith("V") is False else "later stage"))
                        sub.append(s.split(" ", 1)[1] if s.startswith("V") else "")
                        if s.startswith("IV"):
                            later = e[(e.created_at > r.created_at) & e.current_status_id.isin(RAISE)]
                            sub[-1] = "reopened later" if len(later) else "never reopened"
            if not r.ghost:
                gen.append(r.created_at)
    c["family"], c["subfamily"], c["state"], c["wait_h"] = fam, sub, state, wait_h
    return c


def main():
    c, ev = load()
    c = classify(c, ev)
    N_o, N_c = c.oid.nunique(), len(c)
    rep = c[c.k >= 2]
    print(f"[27] contacts={N_c} orders={N_o} IPI={N_c/N_o:.4f} repeats={len(rep)}")
    t = rep.groupby("family").agg(repeat_contacts=("oid", "size"), orders=("oid", "nunique"))
    t["pct_of_repeat"] = (t.repeat_contacts / len(rep) * 100).round(1)
    t["IPI_pts"] = (t.repeat_contacts / N_o).round(3)
    print(t.sort_values("repeat_contacts", ascending=False).to_string())
    assert abs(t.IPI_pts.sum() - (N_c / N_o - 1)) < 0.002
    print(rep.groupby(["family", "subfamily"]).size().to_string())
    out = PIPELINE_ROOT / "output" / "PRX_ReturnPickup_RepeatContacts_MECE_v5.csv"
    cols = ["oid", "k", "channel", "sub_channel", "created_at", "normalized_sub_disposition", "prev_sub", "prev_sc",
            "task_status", "state", "wait_h", "family", "subfamily"]
    c[cols].to_csv(out, index=False)
    print(f"[27] wrote {out}")


if __name__ == "__main__":
    main()
