"""Stage 08: dedup contacts to unique issues.

Rule (per project brief, flagged there as "confirm with stakeholder -- changed once already"):
group by (Final Order ID x normalized Sub-disposition); sort by Created At ascending; Primary =
first row flagged Actionable in the group, else first row overall in the group. Rows with no
usable Final Order ID are excluded from this grouping only -- they remain in raw/audit totals.

Also strips Sales-classified rows first, since dedup/CPO are Support-only metrics (BRD).

Reads:  intermediate/.../combined_contacts.parquet
        reference/disposition_actionability.csv
Writes: intermediate/.../support_contacts.parquet   (Sales stripped, before dedup)
        intermediate/.../deduped_primary_contacts.parquet
"""
import pandas as pd
from pipeline_utils import load_config, intermediate_dir, PIPELINE_ROOT


def main():
    cfg = load_config()
    out_dir = intermediate_dir(cfg)

    combined = pd.read_parquet(out_dir / "combined_contacts.parquet")
    actionability = pd.read_csv(PIPELINE_ROOT / "reference" / "disposition_actionability.csv")
    actionable_map = actionability.set_index("sub_disposition_normalized")["Actionable Classification"]
    actionable_map = actionable_map[~actionable_map.index.duplicated(keep="first")]

    # Support-only universe for dedup/CPO (BRD: strip Sales before any support metric). Rows still
    # needing human review are kept in the raw/audit universe but excluded from Support totals too,
    # since we don't yet know if they're Sales or Support.
    support = combined[combined["sales_or_support"] == "Support"].copy()
    print(f"[08] {len(combined)} combined rows -> {len(support)} Support-only rows "
          f"({len(combined) - len(support)} stripped as Sales or Needs-Manual-Review)")

    support["actionable"] = support["normalized_sub_disposition"].map(actionable_map).fillna("Unknown")
    support["is_actionable"] = support["actionable"].eq("Actionable")

    has_order_id = support["final_order_id"].notna()
    dedupable = support[has_order_id].copy()
    not_dedupable = support[~has_order_id].copy()
    print(f"[08] {len(dedupable)} rows have a usable Final Order ID (dedup-eligible), "
          f"{len(not_dedupable)} do not (kept in raw totals, excluded from dedup/CPO-by-order cuts)")

    dedupable = dedupable.sort_values("created_at")
    dedupable["_group_key"] = list(zip(dedupable["final_order_id"], dedupable["normalized_sub_disposition"]))

    def pick_primary(g):
        actionable_rows = g[g["is_actionable"]]
        primary = actionable_rows.iloc[0] if not actionable_rows.empty else g.iloc[0]
        return primary

    primaries = dedupable.groupby("_group_key", group_keys=False).apply(pick_primary)
    primaries = primaries.reset_index(drop=True)

    group_sizes = dedupable.groupby("_group_key").size()
    primaries = primaries.merge(
        group_sizes.rename("raw_contacts_in_group").reset_index().assign(
            _group_key=lambda d: d["_group_key"]
        )[["_group_key", "raw_contacts_in_group"]],
        on="_group_key", how="left",
    )

    n_groups = dedupable["_group_key"].nunique()
    assert len(primaries) == n_groups, (
        f"[08] FATAL: expected exactly one Primary per (Final Order ID x Sub-disposition) group "
        f"({n_groups} groups) but got {len(primaries)} Primary rows"
    )
    print(f"[08] {n_groups} unique (Final Order ID x Sub-disposition) groups -> "
          f"{len(primaries)} Primary contacts (assertion passed: exactly one Primary per group)")

    # NOTE: bot-resolved status is NOT computed here anymore. A per-(order, sub-disposition)-group
    # "were all rows in this narrow group bot-channel" check has no 48-hour no-further-contact
    # concept and is scoped too narrowly (a human touch on a DIFFERENT issue for the same order
    # wouldn't count against it). The correct, already-existing 48h-aware, whole-order-journey
    # definition lives in 09_resolution_fcr.py's resolution_by_order.parquet -- Stage 10 joins that
    # onto these Primaries instead.

    support.to_parquet(out_dir / "support_contacts.parquet", index=False)
    dedupable.drop(columns=["_group_key"]).to_parquet(out_dir / "dedupable_raw.parquet", index=False)
    not_dedupable.to_parquet(out_dir / "not_dedupable_raw.parquet", index=False)
    primaries.drop(columns=["_group_key"]).to_parquet(out_dir / "deduped_primary_contacts.parquet", index=False)

    print(f"[08] wrote support_contacts.parquet ({len(support)}), "
          f"deduped_primary_contacts.parquet ({len(primaries)})")


if __name__ == "__main__":
    main()
