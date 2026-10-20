"""Shared helpers reused across every pipeline stage script."""
import re
import yaml
import pandas as pd
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent


def load_config():
    with open(PIPELINE_ROOT / "config" / "run_config.yaml") as f:
        return yaml.safe_load(f)


def intermediate_dir(cfg):
    d = PIPELINE_ROOT / "intermediate" / f"run_{cfg['run']['start_date']}_{cfg['run']['end_date']}"
    d.mkdir(parents=True, exist_ok=True)
    return d


MISSING_LITERALS_DEFAULT = {"-", "", "N/A", "NA", "n/a", "None", "null", "nan", "NaN"}


def clean_missing(series: pd.Series, extra_literals=None) -> pd.Series:
    """Blank out placeholder strings that aren't real nulls (per brief's known gotcha)."""
    literals = set(MISSING_LITERALS_DEFAULT)
    if extra_literals:
        literals |= set(extra_literals)
    s = series.astype(str).str.strip()
    s = s.where(~s.isin(literals), other=pd.NA)
    s = s.where(series.notna(), other=pd.NA)
    return s


def extract_single_numeric_id(value):
    """Defensively extract a single numeric ID from a possibly-garbage free-text field.
    Returns None if zero or MULTIPLE distinct numeric ids are found (ambiguous -> exclude from
    order-keyed joins rather than guessing), per the brief's data-quality gotcha.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    if s == "" or s.lower() in {v.lower() for v in MISSING_LITERALS_DEFAULT}:
        return None
    nums = re.findall(r"\d+", s)
    nums = [n.lstrip("0") or "0" for n in nums]
    distinct = set(nums)
    if len(distinct) == 1:
        result = int(next(iter(distinct)))
        # 0 is never a real order_id in this business (marketplace_orders.order_id starts well
        # above 2,000,000) -- some Calls disposition meta JSON uses a literal 0 as its "no order"
        # default instead of null, which was silently pooling unrelated contacts under a fake
        # shared "order 0" (confirmed: one such key had 253 touches in a single month).
        return result if result != 0 else None
    return None  # ambiguous multi-id -> not usable


def normalize_phone(value):
    """Strip non-digits; drop a leading '91' only when the result is exactly 12 digits (India)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) != 10:
        return None
    return digits


SALES_DISPOSITION_KEYS = {
    "scope_for_sale", "no_scope_for_sale", "potential_sale", "sales_related",
    "sales_related_created_a_new_task",
}


# Confirmed by the EIR (2026-10-06) as SALES dispositions, even though they are not in the Support
# master list and some arrive on Support-Team LimeChat tickets / Support CRM tasks. Stage 07 routes
# any Support row carrying one of these (original, pre-conformance value) to Sales, so the Support
# dashboards only contain master-list values. Applies to explicit tag/disposition values only --
# NOT to the "Sales Related / scope_for_sale" rows our own flow-token rules guess at (those keep the
# Team-Name Support verdict, per the earlier locked decision).
SALES_TREE_DISPOSITIONS = {"support_related", "scope_for_sale", "no_scope_for_sale", "potential_sale"}
SALES_TREE_SUBS = {"post_order_concern_feedback", "price_has_increased"}


def normalize_email(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip().lower()
    if not s or s in {v.lower() for v in MISSING_LITERALS_DEFAULT}:
        return None
    if "@" not in s:
        return None
    return s


EXACT_SYNONYM_MAP = {
    "pre_delivery_issue": "pre_delivery_issues",
    "return_pickup": "return_and_pickup",
    "enquiry_feedback": "enquiry_and_feedback",
    "payments_refunds": "payments_and_refunds",
    "consultation_medicine": "consultation_and_medicine",
    "account_app": "account_and_app",
    "wallet_rewards": "wallet_and_rewards",
    "call_dropped": "call_dropped_or_disconnected",
    "short_call_disconnected": "call_dropped_or_disconnected",
    "charges_and_fees_related": "charges_or_fees_related",
    "open": "open_valid_contact",
    "rnr_": "rnr",
    "switched_off_": "switched_off",
}


def normalize_disposition_value(value):
    """Reused verbatim from the analyst notebook's normalization rules:
    first comma-value only, trim/lowercase/casefold, & -> and, collapse -_/ space runs to '_',
    blank/dash -> not_disposed, then apply the exact synonym map.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "not_disposed"
    s = str(value).strip()
    if s == "" or s == "-":
        return "not_disposed"
    s = s.split(",")[0].strip()
    if s == "" or s == "-":
        return "not_disposed"
    s = s.casefold()
    s = s.replace("&", "and")
    s = re.sub(r"[-_/\s]+", "_", s).strip("_")
    if s == "" or s == "not_disposed":
        return "not_disposed"
    return EXACT_SYNONYM_MAP.get(s, s)


class MasterTaxonomy:
    """The current master disposition list (reference/dispositions_master.csv) plus the editable
    legacy-alias table (reference/disposition_aliases.csv). The master is the only source of truth
    for which (disposition, sub-disposition) pairs are valid in outputs."""

    def __init__(self):
        m = pd.read_csv(PIPELINE_ROOT / "reference" / "dispositions_master.csv")
        m["d"] = m["disposition"].map(normalize_disposition_value)
        m["s"] = m["sub_disposition_normalized"]
        self.pairs = set(zip(m["d"], m["s"]))
        self.parents = set(m["d"])
        self.subs = set(m["s"])
        sub_parents = m.groupby("s")["d"].agg(lambda x: sorted(set(x)))
        # sub-dispositions that belong to exactly one master parent (everything except "Issue not listed")
        self.unique_sub_parent = {s: p[0] for s, p in sub_parents.items() if len(p) == 1}
        self.sub_alias, self.disp_alias = {}, {}
        alias_path = PIPELINE_ROOT / "reference" / "disposition_aliases.csv"
        if alias_path.exists():
            for r in pd.read_csv(alias_path).itertuples():
                target = (r.master_disposition, r.master_sub_disposition)
                (self.sub_alias if r.kind == "sub" else self.disp_alias)[r.legacy_value] = target

    def conform_pair(self, d, s):
        """Returns (disposition, sub_disposition, status). status is one of:
        not_disposed | ok | fixed | sub_blank | off_master. Values returned are always master-valid
        (or the not_disposed placeholder) -- anything that cannot be mapped is blanked, never kept."""
        if d == "not_disposed" and s == "not_disposed":
            return d, s, "not_disposed"
        status = "ok"
        if d in self.disp_alias and (s == "not_disposed" or s not in self.subs):
            d, s = self.disp_alias[d]
            status = "fixed"
        if s in self.sub_alias:
            d, s = self.sub_alias[s]
            status = "fixed"
        elif s.startswith("issue_not_listed_") and s[len("issue_not_listed_"):] in self.parents:
            d, s = s[len("issue_not_listed_"):], "issue_not_listed"
            status = "fixed"
        if s in self.unique_sub_parent:
            parent = self.unique_sub_parent[s]
            if d != parent:
                d, status = parent, "fixed"
            return d, s, status
        if s == "issue_not_listed" and (d, s) in self.pairs:
            return d, s, status
        if s == "not_disposed":
            if d in self.parents:
                return d, s, "sub_blank"
            return "not_disposed", "not_disposed", "off_master"
        # sub-disposition not in the master and not alias-mappable
        return (d if d in self.parents else "not_disposed"), "not_disposed", "off_master"


def conform_to_master(df: pd.DataFrame, tax: "MasterTaxonomy" = None) -> pd.DataFrame:
    """Conform normalized_disposition / normalized_sub_disposition to the master list. Originals are
    preserved in master_orig_disposition / master_orig_sub_disposition; master_status records what
    happened. Works on the unique pairs only, so it is cheap on millions of rows."""
    tax = tax or MasterTaxonomy()
    df = df.copy()
    for col in ("normalized_disposition", "normalized_sub_disposition"):
        df[col] = df[col].astype("object").where(df[col].notna(), "not_disposed")
    df["master_orig_disposition"] = df["normalized_disposition"]
    df["master_orig_sub_disposition"] = df["normalized_sub_disposition"]
    pairs = df[["normalized_disposition", "normalized_sub_disposition"]].drop_duplicates()
    res = [tax.conform_pair(d, s) for d, s in zip(pairs["normalized_disposition"], pairs["normalized_sub_disposition"])]
    pairs["_d"], pairs["_s"], pairs["master_status"] = zip(*res) if res else ([], [], [])
    df = df.merge(pairs, on=["normalized_disposition", "normalized_sub_disposition"], how="left")
    df["normalized_disposition"], df["normalized_sub_disposition"] = df["_d"], df["_s"]
    return df.drop(columns=["_d", "_s"])


def fallback_order_join(df: pd.DataFrame, fallback: pd.DataFrame, window_days: int, ts_col: str,
                         phone_col: str = "phone_normalized") -> pd.DataFrame:
    """20-day-lookback phone-based fallback order match, standardized across all 3 channels:
    fallback order's created_at must be strictly EARLIER than the contact timestamp, most recent
    eligible order wins. Vectorized via merge_asof (grouped by phone) -- a naive per-row python
    loop took >10 minutes on ~250k rows against a 920k-row fallback table and was abandoned.
    Returns df with two new columns: order_id_fallback, order_created_at_fallback.
    """
    df = df.copy()
    df["_row_id"] = range(len(df))
    valid = df.dropna(subset=[phone_col, ts_col]).copy()
    if valid.empty:
        df["order_id_fallback"] = None
        df["order_created_at_fallback"] = None
        return df.drop(columns=["_row_id"])

    if "phone_normalized" not in fallback.columns:
        fallback = fallback.copy()
        fallback["phone_normalized"] = fallback["phone_number"].map(normalize_phone)
    fb = fallback.dropna(subset=["phone_normalized"])[["phone_normalized", "order_id", "created_at"]].copy()
    fb = fb.rename(columns={"order_id": "fb_order_id", "created_at": "fb_created_at",
                             "phone_normalized": phone_col})
    fb = fb.sort_values("fb_created_at")
    valid_sorted = valid[["_row_id", phone_col, ts_col]].sort_values(ts_col)

    merged = pd.merge_asof(
        valid_sorted, fb, left_on=ts_col, right_on="fb_created_at", by=phone_col,
        direction="backward", allow_exact_matches=False,
    )
    within_window = (merged[ts_col] - merged["fb_created_at"]) <= pd.Timedelta(days=window_days)
    merged["order_id_fallback"] = merged["fb_order_id"].where(within_window)
    merged["order_created_at_fallback"] = merged["fb_created_at"].where(within_window)

    result = df.merge(
        merged[["_row_id", "order_id_fallback", "order_created_at_fallback"]], on="_row_id", how="left",
    )
    return result.drop(columns=["_row_id"])


def fallback_order_join_by_email(df: pd.DataFrame, fallback: pd.DataFrame, window_days: int,
                                  ts_col: str, email_col: str = "email_normalized") -> pd.DataFrame:
    """Email-keyed sibling of fallback_order_join, for rows with no phone number to fall back on
    (e.g. Zoho Email tickets, ~97% of which carry no phone but do carry the ticket email). Same
    20-day-lookback / strictly-earlier / most-recent-wins semantics, keyed on email instead of
    phone. Returns df with order_id_fallback/order_created_at_fallback filled in only where they
    were not already set (so this is additive on top of an existing phone-based pass, not a
    replacement).
    """
    df = df.copy()
    if "order_id_fallback" not in df.columns:
        df["order_id_fallback"] = None
        df["order_created_at_fallback"] = None
    still_missing = df["order_id_fallback"].isna()
    df["_row_id"] = range(len(df))
    valid = df.loc[still_missing].dropna(subset=[email_col, ts_col]).copy()
    if valid.empty:
        return df.drop(columns=["_row_id"])

    if "email_normalized" not in fallback.columns:
        fallback = fallback.copy()
        fallback["email_normalized"] = fallback["email"].map(normalize_email)
    fb = fallback.dropna(subset=["email_normalized"])[["email_normalized", "order_id", "created_at"]].copy()
    fb = fb.rename(columns={"order_id": "fb_order_id", "created_at": "fb_created_at",
                             "email_normalized": email_col})
    fb = fb.sort_values("fb_created_at")
    valid_sorted = valid[["_row_id", email_col, ts_col]].sort_values(ts_col)

    merged = pd.merge_asof(
        valid_sorted, fb, left_on=ts_col, right_on="fb_created_at", by=email_col,
        direction="backward", allow_exact_matches=False,
    )
    within_window = (merged[ts_col] - merged["fb_created_at"]) <= pd.Timedelta(days=window_days)
    merged["order_id_fallback_email"] = merged["fb_order_id"].where(within_window)
    merged["order_created_at_fallback_email"] = merged["fb_created_at"].where(within_window)

    df = df.merge(
        merged[["_row_id", "order_id_fallback_email", "order_created_at_fallback_email"]],
        on="_row_id", how="left",
    )
    df["order_id_fallback"] = df["order_id_fallback"].combine_first(df["order_id_fallback_email"])
    df["order_created_at_fallback"] = df["order_created_at_fallback"].combine_first(
        df["order_created_at_fallback_email"])
    return df.drop(columns=["_row_id", "order_id_fallback_email", "order_created_at_fallback_email"])


def print_join_match_rate(name, left_keys: pd.Series, right_keys: pd.Series):
    """Mandatory printed validation after every join: match rate + independent set-intersection check."""
    left_set = set(left_keys.dropna().astype(str))
    right_set = set(right_keys.dropna().astype(str))
    matched = left_set & right_set
    rate = len(matched) / len(left_set) if left_set else 0.0
    print(f"[join-check] {name}: left_keys={len(left_set)} right_keys={len(right_set)} "
          f"matched={len(matched)} match_rate={rate:.1%}")
    return rate
