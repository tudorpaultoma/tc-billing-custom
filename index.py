"""
Tencent Cloud Billing Aggregation SCF
Pulls monthly bill detail via DescribeBillDetail API (billing SDK),
flattens ComponentSet, groups by configurable dimensions, aggregates cost
columns, and writes the aggregated CSV to COS.

Trigger: Timer (monthly cron) or manual invocation via API Gateway.
Auth:    Auto-detected from SCF runtime env vars
         (TENCENTCLOUD_SECRETID / TENCENTCLOUD_SECRETKEY /
          TENCENTCLOUD_SESSIONTOKEN) provided by the execution role.

Column selection is configurable. The FIELD_CATALOG below is the single
source of truth: it lists every extractable column, its kind (group / sum /
pass-through) and whether it is included by default. A text config (env var
or a COS object) can override any column with IN / OUT, e.g.:

    ZoneName = IN
    OriginalCost = OUT

Dependencies: tencentcloud-sdk-python (billing). COS upload/download uses
              stdlib urllib with v5 HMAC-SHA1 signing.
"""

import csv
import io
import json
import os
import hmac
import hashlib
import logging
from datetime import datetime, timezone
from typing import List, Dict, Tuple, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import quote

from tencentcloud.common import credential
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
    TencentCloudSDKException,
)
from tencentcloud.billing.v20180709 import billing_client, models

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Configuration — set via SCF environment variables
# ---------------------------------------------------------------------------
BILLING_REGION  = os.environ.get("BILLING_REGION", "ap-singapore")
MONTH           = os.environ.get("MONTH", "")  # required, e.g. "2026-07"
DEST_BUCKET     = os.environ.get("DEST_BUCKET", "")
DEST_REGION     = os.environ.get("DEST_REGION", BILLING_REGION)
DEST_KEY_PREFIX = os.environ.get("DEST_KEY_PREFIX", "aggregated-bills/")

# Column config sources (all optional; built-in defaults apply otherwise).
COLUMN_CONFIG   = os.environ.get("COLUMN_CONFIG", "")      # inline config text
CONFIG_BUCKET   = os.environ.get("CONFIG_BUCKET", "")      # COS bucket holding config
CONFIG_KEY      = os.environ.get("CONFIG_KEY", "")         # COS object key (path)
CONFIG_REGION   = os.environ.get("CONFIG_REGION", DEST_REGION)

# API pagination — intl DescribeBillDetail endpoint allows max 300 per page.
PAGE_LIMIT      = int(os.environ.get("PAGE_LIMIT", "300"))


# ---------------------------------------------------------------------------
# Field catalog — the single source of truth for every extractable column.
#
#   col     : output CSV column name (what appears as a header / in config)
#   kind    : "group" (defines a row), "sum" (numeric aggregate),
#             "pass" (first non-empty value per group)
#   level   : "top" (BillDetail field), "component" (BillDetailComponent field),
#             "derived" (computed from other fields)
#   field   : raw API attribute name (None for derived)
#   default : "IN" (included by default) or "OUT" (opt-in via config)
#
# The default set below reproduces the original fixed columns. Everything
# marked OUT can be enabled via the column config (see README).
# ---------------------------------------------------------------------------
FIELD_CATALOG = [
    # --- Group dimensions (one output row per unique combination) ---
    {"col": "Payer Account ID",   "kind": "group", "level": "top",      "field": "PayerUin",         "default": "IN"},
    {"col": "Owner Account ID",   "kind": "group", "level": "top",      "field": "OwnerUin",         "default": "IN"},
    {"col": "BillingMode",        "kind": "group", "level": "top",      "field": "PayModeName",      "default": "IN"},
    {"col": "ProductName",        "kind": "group", "level": "top",      "field": "BusinessCodeName", "default": "IN"},
    {"col": "Region",             "kind": "group", "level": "top",      "field": "RegionName",       "default": "IN"},
    {"col": "InstanceName",       "kind": "group", "level": "top",      "field": "ResourceName",     "default": "IN"},
    {"col": "TransactionType",    "kind": "group", "level": "top",      "field": "ActionTypeName",   "default": "IN"},
    {"col": "StartDay",           "kind": "group", "level": "derived",  "field": None,               "default": "IN"},
    {"col": "EndDay",             "kind": "group", "level": "derived",  "field": None,               "default": "IN"},

    # --- Pass-through columns (first value per group), included by default ---
    {"col": "Component Contracted Price",            "kind": "pass", "level": "component", "field": "ContractPrice",     "default": "IN"},
    {"col": "Component Price Measurement Unit",      "kind": "pass", "level": "component", "field": "PriceUnit",         "default": "IN"},
    {"col": "Component Usage Unit",                  "kind": "pass", "level": "component", "field": "UsedAmountUnit",    "default": "IN"},
    {"col": "SP Deduction Rate",                     "kind": "pass", "level": "component", "field": "SPDeductionRate",   "default": "IN"},
    {"col": "Discount Multiplier",                   "kind": "pass", "level": "component", "field": "Discount",          "default": "IN"},
    {"col": "Blended Discount Multiplier",           "kind": "pass", "level": "component", "field": "BlendedDiscount",   "default": "IN"},
    {"col": "Currency",                              "kind": "pass", "level": "component", "field": "Currency",          "default": "IN"},
    {"col": "TaxRate",                               "kind": "pass", "level": "component", "field": "TaxRate",           "default": "IN"},
    {"col": "Product Code",                          "kind": "pass", "level": "top",       "field": "BusinessCode",     "default": "IN"},
    {"col": "Bill Month",                            "kind": "pass", "level": "top",       "field": "BillMonth",        "default": "IN"},

    # --- Summed columns, included by default ---
    {"col": "Component Usage",                       "kind": "sum",  "level": "component", "field": "UsedAmount",       "default": "IN"},
    {"col": "OriginalCost",                          "kind": "sum",  "level": "component", "field": "Cost",             "default": "IN"},
    {"col": "RI Deduction (Duration)",               "kind": "sum",  "level": "component", "field": "RiTimeSpan",        "default": "IN"},
    {"col": "RI Deduction (Cost)",                   "kind": "sum",  "level": "component", "field": "OriginalCostWithRI", "default": "IN"},
    {"col": "SP Deduction",                          "kind": "sum",  "level": "component", "field": "SPDeduction",       "default": "IN"},
    {"col": "SP Deduction(Cost)",                    "kind": "sum",  "level": "component", "field": "OriginalCostWithSP", "default": "IN"},
    {"col": "Total Amount After Discount (Excluding Tax)", "kind": "sum", "level": "component", "field": "RealCost",     "default": "IN"},
    {"col": "Voucher Deduction",                     "kind": "sum",  "level": "component", "field": "VoucherPayAmount",  "default": "IN"},
    {"col": "Amount Before Tax",                     "kind": "sum",  "level": "component", "field": "CashPayAmount",     "default": "IN"},
    {"col": "TaxAmount",                             "kind": "sum",  "level": "component", "field": "TaxAmount",         "default": "IN"},
    {"col": "Total Cost (Including Tax)",            "kind": "sum",  "level": "derived",  "field": None,               "default": "IN"},

    # --- Extra top-level fields (opt-in via config) ---
    {"col": "SubProductName",        "kind": "pass", "level": "top", "field": "ProductCodeName", "default": "OUT"},
    {"col": "Subproduct Code",       "kind": "pass", "level": "top", "field": "ProductCode",     "default": "OUT"},
    {"col": "ProjectName",           "kind": "pass", "level": "top", "field": "ProjectName",     "default": "OUT"},
    {"col": "ZoneName",              "kind": "pass", "level": "top", "field": "ZoneName",        "default": "OUT"},
    {"col": "ResourceId",            "kind": "pass", "level": "top", "field": "ResourceId",      "default": "OUT"},
    {"col": "Operator Account ID",   "kind": "pass", "level": "top", "field": "OperateUin",      "default": "OUT"},
    {"col": "Transaction Type Code", "kind": "pass", "level": "top", "field": "ActionType",      "default": "OUT"},
    {"col": "Region ID",             "kind": "pass", "level": "top", "field": "RegionId",        "default": "OUT"},
    {"col": "Project ID",            "kind": "pass", "level": "top", "field": "ProjectId",       "default": "OUT"},
    {"col": "Transaction ID",        "kind": "pass", "level": "top", "field": "BillId",          "default": "OUT"},
    {"col": "Order ID",              "kind": "pass", "level": "top", "field": "OrderId",         "default": "OUT"},
    {"col": "Transaction Time",      "kind": "pass", "level": "top", "field": "PayTime",         "default": "OUT"},
    {"col": "Fee Begin Time",        "kind": "pass", "level": "top", "field": "FeeBeginTime",    "default": "OUT"},
    {"col": "Fee End Time",          "kind": "pass", "level": "top", "field": "FeeEndTime",      "default": "OUT"},
    {"col": "Billing Day",           "kind": "pass", "level": "top", "field": "BillDay",         "default": "OUT"},
    {"col": "Region Type",           "kind": "pass", "level": "top", "field": "RegionType",      "default": "OUT"},
    {"col": "Region Type Name",      "kind": "pass", "level": "top", "field": "RegionTypeName",  "default": "OUT"},
    {"col": "Remark",                "kind": "pass", "level": "top", "field": "ReserveDetail",   "default": "OUT"},
    {"col": "Calculation Formula",   "kind": "pass", "level": "top", "field": "Formula",         "default": "OUT"},
    {"col": "Billing Rules URL",     "kind": "pass", "level": "top", "field": "FormulaUrl",      "default": "OUT"},
    {"col": "Discount Object",       "kind": "pass", "level": "top", "field": "DiscountObject",  "default": "OUT"},
    {"col": "Discount Type",         "kind": "pass", "level": "top", "field": "DiscountType",    "default": "OUT"},
    {"col": "Discount Content",      "kind": "pass", "level": "top", "field": "DiscountContent", "default": "OUT"},
    {"col": "Billing Record ID",     "kind": "pass", "level": "top", "field": "Id",              "default": "OUT"},

    # --- Extra component fields (opt-in via config) ---
    {"col": "Component Type",           "kind": "pass", "level": "component", "field": "ComponentCodeName", "default": "OUT"},
    {"col": "Component Name",           "kind": "pass", "level": "component", "field": "ItemCodeName",      "default": "OUT"},
    {"col": "Component List Price",     "kind": "pass", "level": "component", "field": "SinglePrice",       "default": "OUT"},
    {"col": "Specified Price",          "kind": "pass", "level": "component", "field": "SpecifiedPrice",    "default": "OUT"},
    {"col": "Component Code",           "kind": "pass", "level": "component", "field": "ComponentCode",     "default": "OUT"},
    {"col": "Item Code",                "kind": "pass", "level": "component", "field": "ItemCode",          "default": "OUT"},
    {"col": "Instance Type",            "kind": "pass", "level": "component", "field": "InstanceType",      "default": "OUT"},
    {"col": "Offer Type",               "kind": "pass", "level": "component", "field": "ReduceType",        "default": "OUT"},
    {"col": "Duration Unit",            "kind": "pass", "level": "component", "field": "TimeUnitName",      "default": "OUT"},
    {"col": "Component Config",         "kind": "pass", "level": "component", "field": "ComponentConfig",   "default": "OUT"},
    {"col": "Original Usage/Duration",  "kind": "sum",  "level": "component", "field": "RealTotalMeasure",  "default": "OUT"},
    {"col": "Deducted Usage/Duration",  "kind": "sum",  "level": "component", "field": "DeductedMeasure",    "default": "OUT"},
    {"col": "Usage Duration",           "kind": "sum",  "level": "component", "field": "TimeSpan",           "default": "OUT"},
    {"col": "Free Credit (Incentive)",  "kind": "sum",  "level": "component", "field": "IncentivePayAmount", "default": "OUT"},
    {"col": "Royalty (Transfer)",       "kind": "sum",  "level": "component", "field": "TransferPayAmount",  "default": "OUT"},
]

# Raw field names per level, used by the generic extractor.
_TOP_FIELDS   = [e["field"] for e in FIELD_CATALOG if e["level"] == "top"]
_COMP_FIELDS  = [e["field"] for e in FIELD_CATALOG if e["level"] == "component"]

# Tag columns included by default when no tag config is supplied (legacy set).
DEFAULT_TAG_COLUMNS = ["tag_key:Country", "tag_key:GroupName", "tag_key:Type"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_float(value) -> float:
    """Parse to float, returning 0.0 on failure or None."""
    if value is None:
        return 0.0
    s = str(value).strip()
    if s in ("", "-", "N/A", "None"):
        return 0.0
    try:
        return float(s.replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def date_str_from_timestamp(ts: str) -> str:
    """Extract YYYY-MM-DD from a timestamp string like '2026-07-15 03:42:18'."""
    if not ts:
        return ""
    return str(ts)[:10]


def _derived_start_day(d: dict, comp: dict) -> str:
    return date_str_from_timestamp(d.get("FeeBeginTime", ""))


def _derived_end_day(d: dict, comp: dict) -> str:
    return date_str_from_timestamp(d.get("FeeEndTime", ""))


def _derived_total_with_tax(d: dict, comp: dict) -> float:
    """Total cost including tax = cash + incentive + transfer + voucher + tax."""
    cash = safe_float(comp.get("CashPayAmount", 0))
    incentive = safe_float(comp.get("IncentivePayAmount", 0))
    transfer = safe_float(comp.get("TransferPayAmount", 0))
    voucher = safe_float(comp.get("VoucherPayAmount", 0))
    tax = safe_float(comp.get("TaxAmount", 0))
    return cash + incentive + transfer + voucher + tax


# Map derived column name -> computor(dict, component_dict).
_DERIVED = {
    "StartDay": _derived_start_day,
    "EndDay": _derived_end_day,
    "Total Cost (Including Tax)": _derived_total_with_tax,
}


def get_credential():
    """
    Obtain credentials from SCF runtime environment variables.
    When an SCF execution role is attached, the runtime injects:
      TENCENTCLOUD_SECRETID, TENCENTCLOUD_SECRETKEY,
      TENCENTCLOUD_SESSIONTOKEN
    """
    secret_id = os.environ.get("TENCENTCLOUD_SECRETID", "")
    secret_key = os.environ.get("TENCENTCLOUD_SECRETKEY", "")
    token = os.environ.get("TENCENTCLOUD_SESSIONTOKEN", "")
    if not secret_id or not secret_key:
        raise RuntimeError(
            "No credentials found. Attach an execution role to the SCF "
            "so that TENCENTCLOUD_SECRETID / TENCENTCLOUD_SECRETKEY "
            "/ TENCENTCLOUD_SESSIONTOKEN are injected at runtime."
        )
    return credential.Credential(secret_id, secret_key, token)


# ---------------------------------------------------------------------------
# DescribeBillDetail — paginated fetch
# ---------------------------------------------------------------------------

def fetch_all_bill_details(client, month: str) -> List[dict]:
    """
    Paginate through DescribeBillDetail for the given month.
    Returns a list of raw BillDetail dicts (flattened from the SDK objects).
    """
    all_details = []
    offset = 0
    page = 0

    while True:
        req = models.DescribeBillDetailRequest()
        req.Month = month
        req.Limit = PAGE_LIMIT
        req.Offset = offset
        req.NeedRecordNum = 0

        try:
            resp = client.DescribeBillDetail(req)
        except TencentCloudSDKException as e:
            raise RuntimeError(f"DescribeBillDetail failed: {e}")

        detail_set = resp.DetailSet or []
        if not detail_set:
            break

        for item in detail_set:
            all_details.append(_bill_detail_to_dict(item))

        page += 1
        n = len(detail_set)
        logger.info("Page %d: fetched %d records (cumulative: %d)", page, n, len(all_details))

        if n < PAGE_LIMIT:
            break
        offset += n

    logger.info("Total BillDetail records fetched: %d", len(all_details))
    return all_details


def _bill_detail_to_dict(item) -> dict:
    """
    Convert a BillDetail SDK object into a plain dict, extracting every field
    listed in FIELD_CATALOG (top-level) plus the full ComponentSet and Tags.
    Missing attributes map to "" via getattr.
    """
    d = {f: (getattr(item, f, "") or "") for f in _TOP_FIELDS}

    components = []
    for comp in (getattr(item, "ComponentSet", None) or []):
        components.append({f: (getattr(comp, f, "") or "") for f in _COMP_FIELDS})
    d["ComponentSet"] = components

    d["Tags"] = [
        {"TagKey": getattr(t, "TagKey", "") or "",
         "TagValue": getattr(t, "TagValue", "") or ""}
        for t in (getattr(item, "Tags", None) or [])
    ]
    return d


# ---------------------------------------------------------------------------
# Column resolution (catalog + config overrides)
# ---------------------------------------------------------------------------

def parse_column_config(text: str) -> Dict[str, str]:
    """
    Parse a config text into {column_name: "IN"|"OUT"} overrides.

    Accepted line forms (comments start with #):
        ZoneName = IN
        ZoneName,IN
        ZoneName IN
    The last token must be IN or OUT (case-insensitive).
    """
    overrides: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        if "=" in line:
            col, _, val = line.partition("=")
        elif "," in line:
            col, _, val = line.partition(",")
        else:
            parts = line.split()
            if len(parts) < 2:
                continue
            col, val = " ".join(parts[:-1]), parts[-1]

        col = col.strip()
        val = val.strip().upper()
        if not col or val not in ("IN", "OUT"):
            logger.warning("Ignoring config line (bad value): %r", raw)
            continue
        overrides[col] = val
    return overrides


def resolve_fixed_columns(overrides: Dict[str, str]) -> Tuple[List[str], List[str], List[str]]:
    """Split the catalog into (group, sum, pass) column lists after overrides."""
    group, sum_cols, pass_cols = [], [], []
    for entry in FIELD_CATALOG:
        col = entry["col"]
        state = overrides.get(col, entry["default"]).upper()
        if state != "IN":
            continue
        kind = entry["kind"]
        if kind == "group":
            group.append(col)
        elif kind == "sum":
            sum_cols.append(col)
        else:
            pass_cols.append(col)
    return group, sum_cols, pass_cols


def discover_tag_keys(details: List[dict]) -> List[str]:
    """Return sorted 'tag_key:<Key>' column names seen across all records."""
    keys = set()
    for d in details:
        for t in d.get("Tags", []):
            key = t.get("TagKey", "")
            if key:
                keys.add(f"tag_key:{key}")
    return sorted(keys)


def resolve_tag_columns(overrides: Dict[str, str], discovered: List[str]) -> List[str]:
    """
    Decide which tag columns to include.

    - No tag config at all  -> legacy default set (Country / GroupName / Type).
    - "tag_key:* = IN"      -> all discovered tag columns (minus explicit OUTs).
    - Otherwise             -> only columns explicitly marked IN.
    """
    tag_overrides = {k: v for k, v in overrides.items() if k.startswith("tag_key:")}
    if not tag_overrides:
        return [c for c in DEFAULT_TAG_COLUMNS if c in discovered]

    include_all = tag_overrides.pop("tag_key:*", "").upper() == "IN"
    selected = set(discovered) if include_all else set()
    for k, v in tag_overrides.items():
        if v.upper() == "IN":
            selected.add(k)
        else:
            selected.discard(k)
    return sorted(selected)


def load_column_overrides() -> Dict[str, str]:
    """Merge inline (COLUMN_CONFIG) and COS (CONFIG_BUCKET/CONFIG_KEY) overrides."""
    overrides: Dict[str, str] = {}
    if COLUMN_CONFIG.strip():
        overrides.update(parse_column_config(COLUMN_CONFIG))

    if CONFIG_BUCKET and CONFIG_KEY:
        logger.info("Loading column config from cos://%s/%s", CONFIG_BUCKET, CONFIG_KEY)
        body = download_from_cos(CONFIG_BUCKET, CONFIG_REGION, CONFIG_KEY)
        overrides.update(parse_column_config(body.decode("utf-8")))

    return overrides


# ---------------------------------------------------------------------------
# Flatten: one BillDetail -> N rows (one per component)
# ---------------------------------------------------------------------------

def flatten_details(details: List[dict], tag_columns: List[str]) -> List[dict]:
    """
    Expand each BillDetail into one row per component in its ComponentSet.
    Every catalog column is emitted (aggregation filters later), plus the
    requested tag columns.
    """
    rows = []
    for d in details:
        tags = extract_tags(d.get("Tags", []), tag_columns)
        components = d.get("ComponentSet", [])
        if not components:
            components = [{}]

        for comp in components:
            row = {}
            for entry in FIELD_CATALOG:
                col = entry["col"]
                level = entry["level"]
                if level == "top":
                    val = d.get(entry["field"], "")
                elif level == "component":
                    val = comp.get(entry["field"], "")
                else:  # derived
                    val = _DERIVED[col](d, comp)

                row[col] = safe_float(val) if entry["kind"] == "sum" else val

            for tk in tag_columns:
                row[tk] = tags.get(tk, "")
            rows.append(row)

    return rows


def extract_tags(tags: Optional[List[dict]], tag_columns: List[str]) -> Dict[str, str]:
    """Map a Tags array to the requested 'tag_key:<Key>' columns."""
    result = {tk: "" for tk in tag_columns}
    if not tags:
        return result
    for tag in tags:
        mapped = f"tag_key:{tag.get('TagKey', '')}"
        if mapped in result:
            result[mapped] = tag.get("TagValue", "")
    return result


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate(rows: List[dict], group_cols: List[str],
              sum_cols: List[str], pass_cols: List[str]) -> List[dict]:
    """Group rows by group_cols and aggregate numeric columns."""
    groups: Dict[Tuple, dict] = {}

    for row in rows:
        key = tuple(str(row.get(col, "")).strip() for col in group_cols)

        if key not in groups:
            entry = {}
            for col in group_cols:
                entry[col] = str(row.get(col, "")).strip()
            for col in pass_cols:
                entry[col] = ""
            for col in sum_cols:
                entry[col] = 0.0
            groups[key] = entry

        grp = groups[key]

        for col in sum_cols:
            grp[col] += safe_float(row.get(col, 0))

        for col in pass_cols:
            if not grp.get(col):
                val = str(row.get(col, "")).strip() if row.get(col) is not None else ""
                if val:
                    grp[col] = val

    return list(groups.values())


# ---------------------------------------------------------------------------
# CSV serialisation
# ---------------------------------------------------------------------------

def write_csv(rows: List[dict], output_columns: List[str]) -> bytes:
    """Serialize aggregated rows to CSV bytes (UTF-8 with BOM for Excel)."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=output_columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


# ---------------------------------------------------------------------------
# COS (with v5 HMAC-SHA1 authentication)
# ---------------------------------------------------------------------------

COS_ENDPOINT = "https://{bucket}.cos.{region}.myqcloud.com/{key}"


def _cos_url_encode(s: str) -> str:
    """URL-encode for COS v5 signing. Encodes !'()* and other special chars."""
    return quote(str(s), safe="-_.~")


def _cos_sign(secret_id: str, secret_key: str, method: str, path: str,
              sign_headers: Dict[str, str]) -> str:
    """
    Generate COS v5 Authorization header value.
    See: https://www.tencentcloud.com/document/product/436/7778
    """
    now = int(datetime.now(timezone.utc).timestamp())
    key_time = f"{now};{now + 600}"

    encoded = []
    for k, v in sign_headers.items():
        ek = _cos_url_encode(k).lower()
        ev = _cos_url_encode(v)
        encoded.append((ek, ev))
    encoded.sort(key=lambda x: x[0])

    header_list = ";".join(k for k, _ in encoded)
    http_headers = "&".join(f"{k}={v}" for k, v in encoded)

    http_string = f"{method.lower()}\n{path}\n\n{http_headers}\n"

    http_sha1 = hashlib.sha1(http_string.encode()).hexdigest()
    string_to_sign = f"sha1\n{key_time}\n{http_sha1}\n"

    sign_key = hmac.new(secret_key.encode(), key_time.encode(),
                        hashlib.sha1).hexdigest()
    signature = hmac.new(sign_key.encode(), string_to_sign.encode(),
                         hashlib.sha1).hexdigest()

    return (
        f"q-sign-algorithm=sha1"
        f"&q-ak={secret_id}"
        f"&q-sign-time={key_time}"
        f"&q-key-time={key_time}"
        f"&q-header-list={header_list}"
        f"&q-url-param-list="
        f"&q-signature={signature}"
    )


def _cos_request(method: str, bucket: str, region: str, key: str,
                 body: Optional[bytes] = None) -> bytes:
    """
    Perform a signed COS request (PUT for upload, GET for download) and
    return the response body bytes.
    """
    secret_id = os.environ.get("TENCENTCLOUD_SECRETID", "")
    secret_key = os.environ.get("TENCENTCLOUD_SECRETKEY", "")
    token = os.environ.get("TENCENTCLOUD_SESSIONTOKEN", "")

    if not secret_id or not secret_key:
        raise RuntimeError("No COS credentials. Attach an execution role to the SCF.")

    encoded_key = quote(key, safe="/")
    url = COS_ENDPOINT.format(bucket=bucket, region=region, key=encoded_key)
    host = f"{bucket}.cos.{region}.myqcloud.com"

    sign_headers = {"host": host}
    if body is not None:
        sign_headers["content-type"] = "text/csv"
    if token:
        sign_headers["x-cos-security-token"] = token

    auth = _cos_sign(secret_id, secret_key, method, f"/{key}", sign_headers)

    req = Request(url, data=body, method=method)
    req.add_header("Host", host)
    if body is not None:
        req.add_header("Content-Type", "text/csv")
        req.add_header("Content-Length", str(len(body)))
    if token:
        req.add_header("x-cos-security-token", token)
    req.add_header("Authorization", auth)

    try:
        with urlopen(req, timeout=120) as resp:
            return resp.read()
    except HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(
            f"COS {method} failed for {key}: HTTP {e.code} {e.reason} "
            f"— Response: {error_body[:1000]}"
        )
    except URLError as e:
        raise RuntimeError(f"COS {method} failed for {key}: {e}")


def upload_to_cos(bucket: str, region: str, key: str, body: bytes):
    """Upload bytes to COS via a signed PUT."""
    logger.info("Uploading %d bytes to cos://%s/%s", len(body), bucket, key)
    _cos_request("PUT", bucket, region, key, body=body)
    logger.info("COS upload complete")


def download_from_cos(bucket: str, region: str, key: str) -> bytes:
    """Download bytes from COS via a signed GET."""
    logger.info("Downloading cos://%s/%s", bucket, key)
    data = _cos_request("GET", bucket, region, key)
    logger.info("COS download complete (%d bytes)", len(data))
    return data


# ---------------------------------------------------------------------------
# SCF Entry Point
# ---------------------------------------------------------------------------

def main_handler(event, context):
    """
    Trigger: Timer (monthly cron) or manual / API Gateway invocation.

    Reads MONTH from env var, fetches all bill details from the billing API,
    resolves the active column set (catalog defaults + config overrides),
    flattens components, aggregates by group, writes CSV to COS.
    """
    logger.info("Event: %s", json.dumps(event, default=str))

    month = MONTH or event.get("month", "")
    if not month:
        raise ValueError(
            "MONTH env var is required (e.g. '2026-07'). "
            "Set it in SCF environment variables."
        )
    if not DEST_BUCKET:
        raise ValueError(
            "DEST_BUCKET env var is required. Set it to the COS bucket "
            "where aggregated CSVs should be written."
        )

    logger.info("Month: %s | Billing region: %s | Dest: cos://%s/%s",
                month, BILLING_REGION, DEST_BUCKET, DEST_KEY_PREFIX)

    # --- Auth ---
    cred = get_credential()

    # --- Billing API client ---
    http_profile = HttpProfile()
    http_profile.endpoint = "billing.tencentcloudapi.com"
    client_profile = ClientProfile(httpProfile=http_profile)
    bc = billing_client.BillingClient(cred, BILLING_REGION, client_profile)

    # --- Fetch all bill details (paginated) ---
    raw_details = fetch_all_bill_details(bc, month)

    if not raw_details:
        logger.warning("No bill details returned for month %s", month)
        return {"status": "empty", "month": month, "detail_records": 0}

    # --- Resolve active columns ---
    overrides = load_column_overrides()
    group_cols, sum_cols, pass_cols = resolve_fixed_columns(overrides)
    tag_cols = resolve_tag_columns(overrides, discover_tag_keys(raw_details))
    full_pass_cols = pass_cols + tag_cols
    output_cols = group_cols + full_pass_cols + sum_cols
    logger.info("Columns: %d group, %d pass-through (incl. %d tags), %d sum",
                len(group_cols), len(full_pass_cols), len(tag_cols), len(sum_cols))

    # --- Flatten ComponentSet ---
    flat_rows = flatten_details(raw_details, tag_cols)
    logger.info("Flattened to %d component-level rows", len(flat_rows))

    # --- Aggregate ---
    aggregated = aggregate(flat_rows, group_cols, sum_cols, full_pass_cols)
    logger.info("Aggregated to %d rows", len(aggregated))

    # --- Write CSV to COS ---
    output_csv = write_csv(aggregated, output_cols)
    dest_key = f"{DEST_KEY_PREFIX}{month}_aggregated.csv"
    upload_to_cos(DEST_BUCKET, DEST_REGION, dest_key, output_csv)

    logger.info("Done. %d rows -> cos://%s/%s", len(aggregated), DEST_BUCKET, dest_key)

    return {
        "status": "ok",
        "month": month,
        "detail_records": len(raw_details),
        "component_rows": len(flat_rows),
        "aggregated_rows": len(aggregated),
        "columns": output_cols,
        "destination": f"cos://{DEST_BUCKET}/{dest_key}",
    }
