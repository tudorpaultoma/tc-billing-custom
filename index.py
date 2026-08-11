"""
Tencent Cloud Billing Aggregation SCF
Pulls monthly bill detail via DescribeBillDetail API (billing SDK),
flattens ComponentSet, groups by specified dimensions, aggregates cost
columns, and writes the aggregated CSV to COS.

Trigger: Timer (monthly cron) or manual invocation via API Gateway.
Auth:    Auto-detected from SCF runtime env vars
         (TENCENTCLOUD_SECRETID / TENCENTCLOUD_SECRETKEY /
          TENCENTCLOUD_SESSIONTOKEN) provided by the execution role.

Dependencies: tencentcloud-sdk-python (billing). COS upload uses stdlib urllib
              (SCF internal network auto-authenticates via execution role).
"""

import csv
import io
import json
import os
import logging
from datetime import datetime
from collections import defaultdict
from typing import List, Dict, Tuple, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

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

# API pagination — DescribeBillDetail returns max 1000 records per call.
# 300s timeout allows ~100+ pages, enough for bills with tens of thousands
# of detail records (each of which may expand into multiple component rows).
PAGE_LIMIT      = int(os.environ.get("PAGE_LIMIT", "1000"))

# ---------------------------------------------------------------------------
# Columns that define a group (each unique combination = one output row)
# ---------------------------------------------------------------------------
GROUP_COLUMNS = [
    "Payer Account ID",
    "Owner Account ID",
    "BillingMode",
    "ProductName",
    "Region",
    "InstanceName",
    "TransactionType",
    "StartDay",
    "EndDay",
]

# ---------------------------------------------------------------------------
# Numeric columns to SUM within each group
# ---------------------------------------------------------------------------
SUM_COLUMNS = [
    "Component Usage",
    "OriginalCost",
    "RI Deduction (Duration)",
    "RI Deduction (Cost)",
    "SP Deduction",
    "SP Deduction(Cost)",
    "Total Amount After Discount (Excluding Tax)",
    "Voucher Deduction",
    "Amount Before Tax",
    "TaxAmount",
    "Total Cost (Including Tax)",
]

# ---------------------------------------------------------------------------
# Columns carried forward as-is (first non-empty value wins per group).
# ---------------------------------------------------------------------------
PASS_THROUGH_COLUMNS = [
    "Component Contracted Price",
    "Component Price Measurement Unit",
    "Component Usage Unit",
    "SP Deduction Rate",
    "Discount Multiplier",
    "Blended Discount Multiplier",
    "Currency",
    "TaxRate",
    "tag_key:Country",
    "tag_key:GroupName",
    "tag_key:Type",
    "Product Code",
    "Bill Month",
]

# Full output column order
OUTPUT_COLUMNS = GROUP_COLUMNS + PASS_THROUGH_COLUMNS + SUM_COLUMNS


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


def first_non_empty(*values) -> str:
    """Return the first non-empty, non-None string value."""
    for v in values:
        s = str(v).strip() if v is not None else ""
        if s:
            return s
    return ""


def date_str_from_timestamp(ts: str) -> str:
    """Extract YYYY-MM-DD from a timestamp string like '2026-07-15 03:42:18'."""
    if not ts:
        return ""
    return str(ts)[:10]


def extract_tags(tags: Optional[List[dict]]) -> Dict[str, str]:
    """
    Convert the Tags array from the API into a flat dict.
    Input:  [{"TagKey": "Country", "TagValue": "US"}, ...]
    Output: {"tag_key:Country": "US", "tag_key:GroupName": "", ...}
    """
    result = {
        "tag_key:Country": "",
        "tag_key:GroupName": "",
        "tag_key:Type": "",
    }
    if not tags:
        return result
    for tag in tags:
        key = tag.get("TagKey", "")
        val = tag.get("TagValue", "")
        mapped = f"tag_key:{key}"
        if mapped in result:
            result[mapped] = val
    return result


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

        # Flatten SDK objects to plain dicts for later processing
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
    """Convert a BillDetail SDK object into a plain dict, nesting ComponentSet."""
    components = []
    if item.ComponentSet:
        for comp in item.ComponentSet:
            components.append({
                "ComponentCodeName": getattr(comp, "ComponentCodeName", "") or "",
                "ItemCodeName": getattr(comp, "ItemCodeName", "") or "",
                "SinglePrice": getattr(comp, "SinglePrice", "") or "",
                "PriceUnit": getattr(comp, "PriceUnit", "") or "",
                "UsedAmount": getattr(comp, "UsedAmount", "") or "",
                "UsedAmountUnit": getattr(comp, "UsedAmountUnit", "") or "",
                "Cost": getattr(comp, "Cost", "") or "",
                "Discount": getattr(comp, "Discount", "") or "",
                "ReduceType": getattr(comp, "ReduceType", "") or "",
                "RealCost": getattr(comp, "RealCost", "") or "",
                "VoucherPayAmount": getattr(comp, "VoucherPayAmount", "") or "",
                "CashPayAmount": getattr(comp, "CashPayAmount", "") or "",
                "IncentivePayAmount": getattr(comp, "IncentivePayAmount", "") or "",
                "TransferPayAmount": getattr(comp, "TransferPayAmount", "") or "",
                "ContractPrice": getattr(comp, "ContractPrice", "") or "",
                "RiTimeSpan": getattr(comp, "RiTimeSpan", "") or "",
                "OriginalCostWithRI": getattr(comp, "OriginalCostWithRI", "") or "",
                "SPDeductionRate": getattr(comp, "SPDeductionRate", "") or "",
                "OriginalCostWithSP": getattr(comp, "OriginalCostWithSP", "") or "",
                "BlendedDiscount": getattr(comp, "BlendedDiscount", "") or "",
                "TaxRate": getattr(comp, "TaxRate", "") or "",
                "TaxAmount": getattr(comp, "TaxAmount", "") or "",
                "Currency": getattr(comp, "Currency", "") or "",
            })

    # Tags
    tags = []
    if getattr(item, "Tags", None):
        for t in item.Tags:
            tags.append({
                "TagKey": getattr(t, "TagKey", "") or "",
                "TagValue": getattr(t, "TagValue", "") or "",
            })

    return {
        "PayerUin": getattr(item, "PayerUin", "") or "",
        "OwnerUin": getattr(item, "OwnerUin", "") or "",
        "BusinessCodeName": getattr(item, "BusinessCodeName", "") or "",
        "ProductCodeName": getattr(item, "ProductCodeName", "") or "",
        "PayModeName": getattr(item, "PayModeName", "") or "",
        "ProjectName": getattr(item, "ProjectName", "") or "",
        "RegionName": getattr(item, "RegionName", "") or "",
        "ZoneName": getattr(item, "ZoneName", "") or "",
        "ResourceId": getattr(item, "ResourceId", "") or "",
        "ResourceName": getattr(item, "ResourceName", "") or "",
        "ActionTypeName": getattr(item, "ActionTypeName", "") or "",
        "FeeBeginTime": getattr(item, "FeeBeginTime", "") or "",
        "FeeEndTime": getattr(item, "FeeEndTime", "") or "",
        "BusinessCode": getattr(item, "BusinessCode", "") or "",
        "ProductCode": getattr(item, "ProductCode", "") or "",
        "BillMonth": getattr(item, "BillMonth", "") or "",
        "ComponentSet": components,
        "Tags": tags,
    }


# ---------------------------------------------------------------------------
# Flatten: one BillDetail → N rows (one per component)
# ---------------------------------------------------------------------------

def flatten_details(details: List[dict]) -> List[dict]:
    """
    Expand each BillDetail into one row per component in its ComponentSet.
    """
    rows = []
    for d in details:
        tags = extract_tags(d.get("Tags", []))
        components = d.get("ComponentSet", [])
        if not components:
            # Edge case: no components — still emit a row with empty component fields
            components = [{}]

        for comp in components:
            # Total Cost (Including Tax) = sum of payment amounts + tax
            cash = safe_float(comp.get("CashPayAmount", 0))
            incentive = safe_float(comp.get("IncentivePayAmount", 0))
            transfer = safe_float(comp.get("TransferPayAmount", 0))
            voucher = safe_float(comp.get("VoucherPayAmount", 0))
            tax = safe_float(comp.get("TaxAmount", 0))
            total_with_tax = cash + incentive + transfer + voucher + tax

            row = {
                # Group keys
                "Payer Account ID": d.get("PayerUin", ""),
                "Owner Account ID": d.get("OwnerUin", ""),
                "BillingMode": d.get("PayModeName", ""),
                "ProductName": d.get("BusinessCodeName", ""),
                "Region": d.get("RegionName", ""),
                "InstanceName": d.get("ResourceName", ""),
                "TransactionType": d.get("ActionTypeName", ""),
                "StartDay": date_str_from_timestamp(d.get("FeeBeginTime", "")),
                "EndDay": date_str_from_timestamp(d.get("FeeEndTime", "")),
                # Component-level fields
                "Component Contracted Price": comp.get("ContractPrice", ""),
                "Component Price Measurement Unit": comp.get("PriceUnit", ""),
                "Component Usage": safe_float(comp.get("UsedAmount", 0)),
                "Component Usage Unit": comp.get("UsedAmountUnit", ""),
                "OriginalCost": safe_float(comp.get("Cost", 0)),
                "RI Deduction (Duration)": safe_float(comp.get("RiTimeSpan", 0)),
                "RI Deduction (Cost)": safe_float(comp.get("OriginalCostWithRI", 0)),
                # SP Deduction = original SP cost before rate is applied
                "SP Deduction": safe_float(comp.get("OriginalCostWithSP", 0)),
                "SP Deduction Rate": comp.get("SPDeductionRate", ""),
                "SP Deduction(Cost)": safe_float(comp.get("OriginalCostWithSP", 0)),
                "Discount Multiplier": comp.get("Discount", ""),
                "Blended Discount Multiplier": comp.get("BlendedDiscount", ""),
                "Currency": comp.get("Currency", ""),
                "Total Amount After Discount (Excluding Tax)": safe_float(comp.get("RealCost", 0)),
                "Voucher Deduction": voucher,
                "Amount Before Tax": cash,
                "TaxRate": comp.get("TaxRate", ""),
                "TaxAmount": tax,
                "Total Cost (Including Tax)": total_with_tax,
                # Tags and product metadata
                "tag_key:Country": tags.get("tag_key:Country", ""),
                "tag_key:GroupName": tags.get("tag_key:GroupName", ""),
                "tag_key:Type": tags.get("tag_key:Type", ""),
                "Product Code": d.get("BusinessCode", ""),
                "Bill Month": d.get("BillMonth", ""),
            }
            rows.append(row)

    return rows


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate(rows: List[dict]) -> List[dict]:
    """Group rows by GROUP_COLUMNS and aggregate numeric columns."""
    groups: Dict[Tuple, dict] = {}

    for row in rows:
        key_parts = tuple(str(row.get(col, "")).strip() for col in GROUP_COLUMNS)
        group_key = key_parts

        if group_key not in groups:
            entry = {}
            for col in GROUP_COLUMNS:
                entry[col] = str(row.get(col, "")).strip()
            for col in PASS_THROUGH_COLUMNS:
                entry[col] = ""
            for col in SUM_COLUMNS:
                entry[col] = 0.0
            groups[group_key] = entry

        grp = groups[group_key]

        for col in SUM_COLUMNS:
            grp[col] += safe_float(row.get(col, 0))

        for col in PASS_THROUGH_COLUMNS:
            if not grp.get(col):
                val = str(row.get(col, "")).strip() if row.get(col) is not None else ""
                if val:
                    grp[col] = val

    return list(groups.values())


# ---------------------------------------------------------------------------
# CSV serialisation
# ---------------------------------------------------------------------------

def write_csv(rows: List[dict]) -> bytes:
    """Serialize aggregated rows to CSV bytes (UTF-8 with BOM for Excel)."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


# ---------------------------------------------------------------------------
# COS upload
# ---------------------------------------------------------------------------

COS_ENDPOINT = "https://{bucket}.cos.{region}.myqcloud.com/{key}"


def upload_to_cos(bucket: str, region: str, key: str, body: bytes):
    """
    Upload bytes to COS via HTTP PUT.
    From within SCF, the internal network auto-authenticates requests when
    the execution role has cos:PutObject on the target bucket.
    """
    url = COS_ENDPOINT.format(bucket=bucket, region=region, key=key)
    logger.info("Uploading %d bytes to %s", len(body), url)
    req = Request(url, data=body, method="PUT")
    req.add_header("Content-Type", "text/csv")
    try:
        with urlopen(req, timeout=120) as resp:
            if resp.status not in (200, 204):
                body_text = resp.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"COS PUT failed: HTTP {resp.status} — {body_text[:500]}"
                )
    except URLError as e:
        raise RuntimeError(f"COS upload failed for {key}: {e}")
    logger.info("COS upload complete")


# ---------------------------------------------------------------------------
# SCF Entry Point
# ---------------------------------------------------------------------------

def main_handler(event, context):
    """
    Trigger: Timer (monthly cron) or manual / API Gateway invocation.

    Reads MONTH from env var, fetches all bill details from the billing API,
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

    # --- Flatten ComponentSet ---
    flat_rows = flatten_details(raw_details)
    logger.info("Flattened to %d component-level rows", len(flat_rows))

    # --- Aggregate ---
    aggregated = aggregate(flat_rows)
    logger.info("Aggregated to %d rows", len(aggregated))

    # --- Write CSV to COS ---
    output_csv = write_csv(aggregated)
    dest_key = f"{DEST_KEY_PREFIX}{month}_aggregated.csv"
    upload_to_cos(DEST_BUCKET, DEST_REGION, dest_key, output_csv)

    logger.info("Done. %d rows → cos://%s/%s", len(aggregated), DEST_BUCKET, dest_key)

    return {
        "status": "ok",
        "month": month,
        "detail_records": len(raw_details),
        "component_rows": len(flat_rows),
        "aggregated_rows": len(aggregated),
        "destination": f"cos://{DEST_BUCKET}/{dest_key}",
    }
