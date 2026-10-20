"""Stage 06+07: validate normalized dispositions against the reference taxonomy, then combine
LimeChat + Zoho + Calls into one row-per-contact table with a common schema.

(Disposition normalization itself already happened inline in stages 03/04/05, using the shared
normalize_disposition_value() function from pipeline_utils -- this stage's job is the cross-channel
consistency CHECK the brief requires: every normalized sub-disposition must appear in the reference
taxonomy, or be explicitly flagged, never silently kept/dropped.)

Reads:  intermediate/.../limechat_classified.parquet
        intermediate/.../zoho_classified.parquet
        intermediate/.../calls_classified.parquet
        reference/disposition_actionability.csv
Writes: intermediate/.../combined_contacts.parquet
"""
import pandas as pd
from pipeline_utils import (
    load_config, intermediate_dir, PIPELINE_ROOT, extract_single_numeric_id, SALES_DISPOSITION_KEYS,
    MasterTaxonomy, conform_to_master, SALES_TREE_DISPOSITIONS, SALES_TREE_SUBS,
)


def main():
    cfg = load_config()
    out_dir = intermediate_dir(cfg)

    actionability = pd.read_csv(PIPELINE_ROOT / "reference" / "disposition_actionability.csv")
    known_subdisp = set(actionability["sub_disposition_normalized"].dropna())

    limechat = pd.read_parquet(out_dir / "limechat_classified.parquet")
    zoho = pd.read_parquet(out_dir / "zoho_classified.parquet")
    calls = pd.read_parquet(out_dir / "calls_classified.parquet")

    limechat_std = pd.DataFrame({
        "channel": "LimeChat",
        "sub_channel": limechat["sub_channel"],
        "contact_id": limechat["Ticket ID"],
        "created_at": limechat["created_at_dt"],
        "phone_normalized": limechat["phone_normalized"],
        "order_id_direct": limechat["order_id_direct"],
        "final_order_id": limechat["final_order_id"],
        "sales_or_support": limechat["sales_or_support"],
        "normalized_disposition": limechat["normalized_disposition"],
        "normalized_sub_disposition": limechat["normalized_sub_disposition"],
        "attribution_source": limechat["attribution_source"],
        "attribution_confidence": limechat["attribution_confidence"],
        "is_connected": pd.NA,
        "is_completed": pd.NA,
    })

    zoho_std = pd.DataFrame({
        "channel": "Email",
        "sub_channel": zoho["sub_channel"],
        "contact_id": zoho["Ticket Id"],
        "created_at": zoho["created_at_dt"],
        "phone_normalized": zoho["phone_normalized"],
        "order_id_direct": zoho["order_id_direct"],
        "final_order_id": zoho["final_order_id"],
        # Zoho's Channel=='Email' filter (applied in Stage 04) excludes ORM/social channels, but an
        # email ticket can still be a genuine sales inquiry -- classify by the real disposition
        # (confirmed: 189 of 3,512 Email rows are Sales-category) rather than assuming Email==Support.
        "sales_or_support": zoho["normalized_disposition"].map(
            lambda d: "Sales" if d in SALES_DISPOSITION_KEYS else "Support"
        ),
        "normalized_disposition": zoho["normalized_disposition"],
        "normalized_sub_disposition": zoho["normalized_sub_disposition"],
        "attribution_source": "zoho_channel_filter",
        "attribution_confidence": "high",
        "is_connected": pd.NA,
        "is_completed": pd.NA,
    })

    calls_std = pd.DataFrame({
        "channel": "Calls",
        "sub_channel": calls["leg"],
        "contact_id": pd.NA,
        "created_at": calls["created_at_dt"],
        "phone_normalized": calls["phone_normalized"],
        "order_id_direct": calls["order_id_direct"],
        "final_order_id": calls["final_order_id"],
        "sales_or_support": calls["sales_or_support"],
        "normalized_disposition": calls["normalized_disposition"],
        "normalized_sub_disposition": calls["normalized_sub_disposition"],
        "attribution_source": "calls_leg_filter",
        "attribution_confidence": "high",
        "is_connected": calls["is_connected"],
        "is_completed": calls["is_completed"],
    })

    combined = pd.concat([limechat_std, zoho_std, calls_std], ignore_index=True)
    # final_order_id is already a clean numeric ID from each channel's own extraction/fallback join
    # -- do NOT re-run extract_single_numeric_id here, it treats "2345678.0" as TWO ids (integer
    # part + decimal part) via its digit-regex and nulls out the row. Just coerce to nullable Int64.
    combined["final_order_id"] = pd.to_numeric(combined["final_order_id"], errors="coerce").astype("Int64")

    # --- Conform Support rows to the MASTER disposition list (reference/dispositions_master.csv) ---
    # Outputs must only carry master-valid (disposition, sub-disposition) pairs. Previously this stage
    # only checked against the broader actionability table (173 keys, 52% legacy/unmapped), so legacy
    # names and wrong parent/sub pairings (e.g. multi-valued LimeChat tags) passed silently. See
    # MasterTaxonomy.conform_pair: re-derives the parent from the master wherever the sub-disposition
    # is unambiguous, maps issue_not_listed_<parent> and alias-table legacy spellings, and blanks
    # (never keeps) anything that cannot be mapped. Sales / manual-review rows are left untouched.
    tax = MasterTaxonomy()
    sup = combined["sales_or_support"] == "Support"
    conformed = conform_to_master(
        combined.loc[sup, ["normalized_disposition", "normalized_sub_disposition"]], tax)
    combined["master_orig_disposition"] = combined["normalized_disposition"]
    combined["master_orig_sub_disposition"] = combined["normalized_sub_disposition"]
    combined["master_status"] = "not_support"
    for col in ("normalized_disposition", "normalized_sub_disposition", "master_orig_disposition",
                "master_orig_sub_disposition", "master_status"):
        combined.loc[sup, col] = conformed[col].to_numpy()

    # Explicit Sales-tree values (confirmed Sales by the EIR) leave the Support universe -- but only
    # when the row has no master-valid sub-disposition of its own (e.g. a ticket whose first Level 2
    # tag is a Sales parent yet whose chosen sub-disposition is a valid Support one stays Support).
    # Original values are restored on routed rows (they are Sales now; nothing was lost).
    sales_flip = sup & combined["master_status"].isin(["off_master", "sub_blank", "not_disposed"]) & (
        combined["master_orig_disposition"].isin(SALES_TREE_DISPOSITIONS)
        | combined["master_orig_sub_disposition"].isin(SALES_TREE_SUBS)
    ) | (sup & combined["master_orig_sub_disposition"].isin(SALES_TREE_SUBS))
    print(f"[07] routed {int(sales_flip.sum())} Support rows to Sales (explicit Sales-tree disposition/sub): "
          f"{combined.loc[sales_flip, 'sub_channel'].value_counts().to_dict()}")
    combined.loc[sales_flip, "normalized_disposition"] = combined.loc[sales_flip, "master_orig_disposition"]
    combined.loc[sales_flip, "normalized_sub_disposition"] = combined.loc[sales_flip, "master_orig_sub_disposition"]
    combined.loc[sales_flip, "master_status"] = "routed_to_sales"
    combined.loc[sales_flip, "sales_or_support"] = "Sales"
    sup = combined["sales_or_support"] == "Support"

    # unmapped_normalized_value now means: a Support value that could NOT be mapped to the master.
    combined["unmapped_normalized_value"] = combined["master_status"] == "off_master"
    print("[07] master conformance (Support rows) by channel:")
    print(pd.crosstab(combined.loc[sup, "sub_channel"], combined.loc[sup, "master_status"], margins=True))
    off = combined[combined["unmapped_normalized_value"]]
    print(f"[07] {len(off)} Support rows ({len(off) / sup.sum():.1%}) hold a value that is NOT in the master and "
          f"could not be aliased -- blanked, listed in the audit file. Top original values:")
    print(off.groupby(["master_orig_disposition", "master_orig_sub_disposition"]).size()
             .sort_values(ascending=False).head(15))

    audit = (combined[combined["master_status"].isin(["fixed", "off_master", "sub_blank", "routed_to_sales"])]
             .groupby(["master_status", "sub_channel", "master_orig_disposition", "master_orig_sub_disposition",
                       "normalized_disposition", "normalized_sub_disposition"]).size()
             .rename("rows").reset_index()
             .rename(columns={"normalized_disposition": "conformed_disposition",
                              "normalized_sub_disposition": "conformed_sub_disposition"})
             .sort_values(["master_status", "rows"], ascending=[True, False]))
    audit_path = PIPELINE_ROOT / "output" / (
        f"PRX_Master_Conformance_Audit_{cfg['run']['start_date']}_to_{cfg['run']['end_date']}_"
        f"{cfg['run']['version_tag']}.csv")
    audit.to_csv(audit_path, index=False)
    print(f"[07] wrote conformance audit -> {audit_path}")

    combined.to_parquet(out_dir / "combined_contacts.parquet", index=False)
    print(f"\n[07] wrote {len(combined)} combined contact rows -> {out_dir / 'combined_contacts.parquet'}")
    print(f"[07] channel distribution:\n{combined['channel'].value_counts()}")
    print(f"[07] sales_or_support distribution:\n{combined['sales_or_support'].value_counts(dropna=False)}")
    sales_pct = (combined["sales_or_support"] == "Sales").mean()
    print(f"[07] Sales share of ALL combined contacts: {sales_pct:.1%} (brief's known rate ~5-6%)")


if __name__ == "__main__":
    main()
