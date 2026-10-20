"""Stage 03: LimeChat Sales/Support attribution + disposition extraction + order-ID attribution.

This is the piece that had NO prior working code (per the project brief) — newly built here
following the brief's priority chain exactly.

Reads:  intermediate/.../limechat_raw.parquet
        reference/system_tags_rules.csv
        reference/disposition_actionability.csv  (to know which parent categories are "Sales Related")
Writes: intermediate/.../limechat_classified.parquet
"""
import pandas as pd
from pipeline_utils import (
    load_config, intermediate_dir, PIPELINE_ROOT, normalize_disposition_value,
    normalize_phone, extract_single_numeric_id, print_join_match_rate, fallback_order_join,
    SALES_DISPOSITION_KEYS, MasterTaxonomy,
)

TAX = MasterTaxonomy()


def pick_level3(level2, level3):
    """LimeChat tag cells can hold several comma-separated tags (~6k Support tickets). The old code
    kept only the FIRST Level 3 token, so a ticket tagged e.g. L3 'Delayed..., Cancellation...' kept
    whichever came first even when only a later token is a valid master sub-disposition. Prefer the
    first token that conforms to the master list (directly, via alias, or as issue_not_listed_<parent>);
    otherwise fall back to the first token, exactly as before."""
    tokens = [t.strip() for t in (level3 or "").split(",") if t.strip()]
    if len(tokens) <= 1:
        return level3 or None
    l2 = [t.strip() for t in (level2 or "").split(",") if t.strip()]
    d = normalize_disposition_value(l2[0]) if l2 else "not_disposed"
    for t in tokens:
        if TAX.conform_pair(d, normalize_disposition_value(t))[2] in ("ok", "fixed"):
            return t
    return tokens[0]


def load_system_tags_rules():
    df = pd.read_csv(PIPELINE_ROOT / "reference" / "system_tags_rules.csv")
    # Guard: every rule must emit master-valid values (or leave the sub-disposition blank). A rule that
    # needs the alias/conformance layer to be valid means the rule table has drifted from the master.
    bad = []
    for r in df.itertuples():
        if pd.isna(r.disposition):
            continue
        sub = normalize_disposition_value(r.sub_disposition) if pd.notna(r.sub_disposition) else "not_disposed"
        status = TAX.conform_pair(normalize_disposition_value(r.disposition), sub)[2]
        if status not in ("ok", "sub_blank"):
            bad.append((r.token, r.disposition, r.sub_disposition, status))
    if bad:
        print(f"[03] WARNING: {len(bad)} system-tag rules emit values that are not master-valid: {bad}")
    else:
        print(f"[03] system_tags_rules.csv: {len(df)} rules, all master-valid")
    return df.sort_values("priority").to_dict("records")


def classify_row(team_name, level1, level2, level3, system_tags, rules):
    """Returns (sales_or_support, disposition, sub_disposition, attribution_source, confidence)."""
    team_name = (team_name or "").strip()
    level1 = (level1 or "").strip()
    level2 = (level2 or "").strip()
    level3 = (level3 or "").strip()
    tags = [t.strip() for t in (system_tags or "").split(",") if t.strip()]

    # 1. Team Name — most authoritative, but sparse.
    if team_name.lower() == "sales team":
        return "Sales", None, None, "team_name", "high"
    if team_name.lower() == "support team":
        # Team Name alone doesn't give a disposition — fall through to Level 2/3 for that,
        # but we already know Sales/Support here.
        support_flag = "team_name"
    else:
        support_flag = None

    # 2. Level 1 Tags — sales/support keyword, checked as substring across any comma-token.
    if support_flag is None:
        l1_tokens = [t.strip().lower() for t in level1.split(",") if t.strip()]
        if any("sales" in t for t in l1_tokens):
            return "Sales", None, None, "level1_tags", "high"
        if any("support" in t for t in l1_tokens):
            support_flag = "level1_tags"

    # 3. Level 2 Tags — direct disposition parent-category mapping; also tells us Sales vs Support
    # UNLESS Team Name already authoritatively said "Support Team" -- a support-team conversation
    # can still land on a sales-adjacent disposition topic without the contact itself being Sales
    # (Team Name is the higher-priority signal and must not be silently overridden downstream).
    if level2:
        disp_norm = normalize_disposition_value(level2)
        sales_or_support = "Support" if support_flag else (
            "Sales" if disp_norm in SALES_DISPOSITION_KEYS else "Support"
        )
        source = support_flag or "level2_tags"
        return sales_or_support, level2, pick_level3(level2, level3), source, "high"

    # 4. System Tags flow-token rules — first matching token in priority order. Same rule: don't
    # let a rule's Sales/Support guess override an already-known Team Name verdict.
    for rule in rules:
        if rule["token"] in tags:
            source = support_flag or "system_tags_flow"
            sales_or_support = "Support" if support_flag else rule["sales_or_support"]
            return sales_or_support, rule["disposition"], rule["sub_disposition"], source, "medium"

    # 5. System Tags team-routing tag — Sales/Support only, no disposition. Skipped entirely once
    # Team Name/Level 1 already gave us an authoritative "Support" verdict (same override-guard as
    # steps 3-4) -- otherwise a stray "chat_sales_-_wb" token could still flip a known-Support
    # contact to Sales.
    if support_flag:
        return "Support", None, None, support_flag, "medium"
    if "chat_support_-_wb" in tags:
        return "Support", None, None, "system_tags_routing", "low"
    if "chat_sales_-_wb" in tags:
        return "Sales", None, None, "system_tags_routing", "low"

    # 6. Never guess.
    return "Needs Manual Review", None, None, "manual_review_needed", "none"


def main():
    cfg = load_config()
    out_dir = intermediate_dir(cfg)
    df = pd.read_parquet(out_dir / "limechat_raw.parquet")
    rules = load_system_tags_rules()

    results = df.apply(
        lambda r: classify_row(
            r.get("Team Name"), r.get("Level 1 Tags (Main)"), r.get("Level 2 Tags"),
            r.get("Level 3 Tags"), r.get("System Tags"), rules,
        ),
        axis=1, result_type="expand",
    )
    results.columns = ["sales_or_support", "raw_disposition", "raw_sub_disposition",
                        "attribution_source", "attribution_confidence"]
    df = pd.concat([df, results], axis=1)

    df["normalized_disposition"] = df["raw_disposition"].map(normalize_disposition_value)
    df["normalized_sub_disposition"] = df["raw_sub_disposition"].map(normalize_disposition_value)

    # Order-ID attribution: direct Custom Order ID first.
    df["order_id_direct"] = df["Custom Order ID"].map(extract_single_numeric_id)
    df["phone_normalized"] = df["Phone Number"].map(normalize_phone)
    df["created_at_dt"] = pd.to_datetime(df["Created At"], errors="coerce")

    # Sub-channel split, needed downstream for the channel-volume report.
    df["is_handoff_bool"] = df["is_handoff?"].str.strip().str.lower().eq("true")
    df["sub_channel"] = df.apply(
        lambda r: "LimeChat Bot + Agent" if (r["is_handoff_bool"] and str(r.get("Team Name", "")).strip() == "Support Team")
        else "LimeChat Bot", axis=1,
    )

    # 20-day phone fallback for rows with no direct Custom Order ID (standardized helper shared
    # with Zoho/Calls -- fallback order's created_at must be strictly earlier than the chat).
    fallback = pd.read_parquet(out_dir / "fallback_orders.parquet")
    df = fallback_order_join(df, fallback, cfg["fallback_join"]["window_days"], "created_at_dt")
    df["final_order_id"] = df["order_id_direct"].combine_first(df["order_id_fallback"])

    out_path = out_dir / "limechat_classified.parquet"
    df.to_parquet(out_path, index=False)

    # Validation prints (brief's known rates: sales ~5-6% of combined; manual-review ~1%).
    n = len(df)
    sales_pct = (df["sales_or_support"] == "Sales").mean()
    manual_pct = (df["sales_or_support"] == "Needs Manual Review").mean()
    order_fill = df["order_id_direct"].notna().mean()
    final_fill = df["final_order_id"].notna().mean()
    print(f"[03] classified {n} LimeChat rows -> {out_path}")
    print(f"[03] final_order_id fill after 20-day fallback: {final_fill:.1%} (direct was {order_fill:.1%})")
    print(f"[03] sales_or_support distribution:\n{df['sales_or_support'].value_counts(dropna=False)}")
    print(f"[03] Sales share: {sales_pct:.1%} (brief's known rate ~5-6% of COMBINED contacts, "
          f"so this LimeChat-only figure may differ)")
    print(f"[03] Needs Manual Review share: {manual_pct:.1%} (brief's known rate ~1%)")
    print(f"[03] attribution_source distribution:\n{df['attribution_source'].value_counts(dropna=False)}")
    print(f"[03] Custom Order ID direct-fill rate: {order_fill:.1%}")
    print(f"[03] sub_channel distribution:\n{df['sub_channel'].value_counts()}")
    undisposed_pct = (df["normalized_disposition"] == "not_disposed").mean()
    print(f"[03] not_disposed share: {undisposed_pct:.1%} (brief's known rate: ~49% zero-signal even "
          f"after full tag pipeline)")


if __name__ == "__main__":
    main()
