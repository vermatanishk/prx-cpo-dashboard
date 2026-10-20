"""Stage 26: MECE decomposition of the IPI uplift for 'missing_or_less_quantity_received' (v5 run).

Unit = repeat contact (2nd+ contact on an order in this sub-disposition). Every repeat contact gets exactly
one family, so the families partition (contacts - orders) and sum to IPI - 1 (the IPI uplift).

Families (state-first, first match wins):
  G  residual ghost  -- an Agent-OB task with crm_tasks.status != 20 (auto-closed placeholder, see
                        PROJECT_METHODOLOGY_AND_LEARNINGS.md 6c) still in the data, OR a genuine contact that is only a
                        'repeat' because every earlier contact on the order is such a ghost.
  F  agent callback  -- Agent-OB task with status == 20 (a human really called back).
  customer-initiated (Agent IB / LimeChat / Ringg / Email), classified by the order's return state at contact time:
  I  intake          -- no return request existed in the earlier contact's session:
        I1 never created (no 200/210/280 ever on the order)
        I2 created later (first 200/210 > 30 min after the previous genuine contact); split redial<=30m / later
  II double-touch    -- return created within 30 min of the previous contact; this contact is in the same window
  III awaiting       -- return raised (200/210), still waiting to be processed
  IV cancelled       -- last return state is 280 (reopened later / never reopened)
  V  later stage     -- pickup / QC / refund / post-refund

Reads : intermediate/run_2026-05-01_2026-10-01/{dedupable_raw,calls_agent_outbound_task_status,quantity_status_log}.parquet
        raw/orders/return_cancel_events_since_2026-05-01.csv  (README_clickhouse_queries.md sec 6)
Writes: output/PRX_MissingQty_RepeatContacts_MECE_v5.csv  (one row per contact, all 4,091)
"""
import pandas as pd, numpy as np
from pipeline_utils import PIPELINE_ROOT

R = PIPELINE_ROOT / "intermediate" / "run_2026-05-01_2026-10-01"
T = "missing_or_less_quantity_received"
TOL = pd.Timedelta(minutes=30)
RETURN_IDS = {200, 210, 220, 230, 231, 232, 233, 234, 236, 238, 240, 244, 245, 246, 247, 250, 260, 262, 265, 270, 280}
FWD = RETURN_IDS - {200, 210, 280}


def staffed(ts):  # Agent-IB (human) calls only occur ~03:00-14:30 UTC (IST 08:30-20:00)
    m = ts.dt.hour * 60 + ts.dt.minute
    return (m >= 180) & (m < 870)


def load():
    raw = pd.read_parquet(R / "dedupable_raw.parquet")
    raw["created_at"] = pd.to_datetime(raw["created_at"])
    sub = raw[raw.normalized_sub_disposition == T].dropna(subset=["final_order_id"]).copy()
    sub["oid"] = sub.final_order_id.astype("int64")
    ts = pd.read_parquet(R / "calls_agent_outbound_task_status.parquet")
    ts["task_created_at"] = pd.to_datetime(ts["task_created_at"])
    sub = sub.sort_values("created_at")
    sub = pd.merge_asof(sub, ts.sort_values("task_created_at")[["task_created_at", "task_id", "task_status"]],
                        left_on="created_at", right_on="task_created_at", direction="nearest",
                        tolerance=pd.Timedelta(seconds=1))
    is_ob = sub.sub_channel.str.startswith("Agent OB")
    sub.loc[~is_ob, ["task_id", "task_status"]] = np.nan
    sub["is_ob"] = is_ob
    sub["ghost"] = is_ob & (sub.task_status != 20)
    sub = sub.sort_values(["oid", "created_at"]).reset_index(drop=True)
    sl = pd.read_parquet(R / "quantity_status_log.parquet")
    sl["created_at"] = pd.to_datetime(sl["created_at"])
    ev = {o: g.sort_values("created_at") for o, g in sl[sl.current_status_id.isin(RETURN_IDS)].groupby("order_id")}
    return sub, ev


def state_family(s):
    if s == 280: return "IV cancelled in backend"
    if s in (200, 210): return "III raised, awaiting processing"
    if s in (220, 230, 231, 232, 233, 234): return "V later stage: pickup"
    if s in (231, 233): return "V later stage: pickup"
    if s in (260, 262): return "V later stage: refund in progress"
    if s == 265: return "V later stage: post-refund"
    return "V later stage: warehouse/QC"


def classify(sub, ev):
    sub = sub.copy()
    sub["k"] = sub.groupby("oid").cumcount() + 1
    sub["prev_sc"] = sub.groupby("oid").sub_channel.shift(1)
    sub["gap_h"] = (sub.created_at - sub.groupby("oid").created_at.shift(1)).dt.total_seconds() / 3600
    fam, subfam = [], []
    for oid, g in sub.groupby("oid", sort=False):
        genuine_times = []          # times of earlier genuine (non-ghost) contacts
        for r in g.itertuples():
            if r.k == 1:
                fam.append("first contact"); subfam.append("")
            elif r.ghost:
                fam.append("G residual ghost"); subfam.append("ghost task is itself a repeat slot")
            elif not genuine_times:
                fam.append("G residual ghost"); subfam.append("genuine contact counted as repeat only because earlier contact(s) are ghost tasks")
            elif r.is_ob:
                fam.append("F agent callback (status 20)")
                subfam.append("previous contact = Ringg" if r.prev_sc == "Ringg" else "previous contact = other")
            else:
                prev_t = genuine_times[-1]
                e = ev.get(oid)
                gap_prev = (r.created_at - prev_t).total_seconds() / 3600
                if e is None or len(e) == 0:
                    fam.append("I1 intake: return never created"); subfam.append("")
                else:
                    rr = e[e.current_status_id.isin([200, 210])]
                    r_time = rr.created_at.min() if len(rr) else e.created_at.min()
                    if r.created_at <= r_time + TOL:
                        if r_time > prev_t + TOL:
                            fam.append("I2 intake: return created later than earlier contact")
                            subfam.append("redial <=30 min" if gap_prev <= 0.5 else "re-contact >30 min")
                        else:
                            fam.append("II double-touch while return being created"); subfam.append("")
                    else:
                        s = int(e[e.created_at <= r.created_at].iloc[-1].current_status_id)
                        f = state_family(s)
                        fam.append(f.split(":")[0] if f.startswith("V") else f)
                        if f.startswith("IV"):
                            later = e[(e.created_at > r.created_at) & e.current_status_id.isin([200, 210])]
                            subfam.append("reopened later" if len(later) else "never reopened")
                        elif f.startswith("V"):
                            subfam.append(f.split(": ")[1])
                        else:
                            subfam.append("")
            if not r.ghost:
                genuine_times.append(r.created_at)
    sub["family"], sub["subfamily"] = fam, subfam
    return sub


def main():
    sub, ev = load()
    c = classify(sub, ev)
    N_o, N_c = c.oid.nunique(), len(c)
    rep = c[c.k >= 2]
    print(f"[26] contacts={N_c} orders={N_o} IPI={N_c/N_o:.4f} repeat contacts={len(rep)}")
    t = rep.groupby("family").agg(repeat_contacts=("oid", "size"), orders=("oid", "nunique"))
    t["pct_of_repeat"] = (t.repeat_contacts / len(rep) * 100).round(1)
    t["pct_of_all_contacts"] = (t.repeat_contacts / N_c * 100).round(1)
    t["pct_of_all_orders"] = (t.orders / N_o * 100).round(1)
    t["IPI_pts"] = (t.repeat_contacts / N_o).round(3)
    print(t.sort_values("repeat_contacts", ascending=False).to_string())
    assert abs(t.IPI_pts.sum() - (N_c / N_o - 1)) < 0.002, "families must partition the IPI uplift"
    print(rep.groupby(["family", "subfamily"]).size().to_string())
    out = PIPELINE_ROOT / "output" / "PRX_MissingQty_RepeatContacts_MECE_v5.csv"
    c[["oid", "k", "channel", "sub_channel", "created_at", "task_status", "gap_h", "family", "subfamily"]].to_csv(out, index=False)
    print(f"[26] wrote {out}")


if __name__ == "__main__":
    main()
