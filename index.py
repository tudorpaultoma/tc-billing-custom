"""
Tencent Cloud Billing Processor SCF
Reads monthly bill CSV from COS source bucket, groups by specified dimensions,
aggregates cost columns, and writes the result to a destination COS bucket.

Trigger: COS event (PutObject on source bucket) or Timer trigger.
Dependencies: None (stdlib only — csv, io, zipfile, gzip, json, os, urllib).
"""

import csv
import io
import json
import os
import zipfile
import gzip
import logging
from datetime import datetime
from collections import defaultdict
from urllib.request import Request, urlopen
from urllib.error import URLError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Configuration — set via SCF environment variables
# ---------------------------------------------------------------------------
DEST_BUCKET     = os.environ.get("DEST_BUCKET", "")
DEST_REGION     = os.environ.get("DEST_REGION", "ap-singapore")
DEST_KEY_PREFIX = os.environ.get("DEST_KEY_PREFIX", "aggregated-bills/")

# Source COS region — defaults to DEST_REGION if not set (common case:
# both buckets in the same region).
SOURCE_REGION = os.environ.get("SOURCE_REGION", DEST_REGION)

# COS endpoint template for direct HTTP access (SCF-internal network)
COS_ENDPOINT = "https://{bucket}.cos.{region}.myqcloud.com/{key}"

# ---------------------------------------------------------------------------
# Columns that define a group (each unique combination = one output row)
# These MUST exist in the source CSV.
# ---------------------------------------------------------------------------
GROUP_COLUMNS = [
    "Payer Account ID",
    "Owner Account ID",
    "BillingMode",
    "ProductName",
    "Region",
    "InstanceName",
    "TransactionType",
    "StartDay",     # derived from "Usage Start Time"
    "EndDay",       # derived from "Usage End Time"
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
# Columns carried forward as-is (first seen value wins per group).
# These are string/categorical columns that should be identical within a group.
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

# Full output column order (group keys first, then pass-through, then sums)
OUTPUT_COLUMNS = GROUP_COLUMNS + PASS_THROUGH_COLUMNS + SUM_COLUMNS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def derive_day(date_str: str) -> str:
    """Extract YYYY-MM-DD from a datetime string like '2026-07-15 03:42:18'."""
    if not date_str:
        return ""
    try:
        return date_str[:10]  # first 10 chars = YYYY-MM-DD
    except Exception:
        return ""


def safe_float(value: str) -> float:
    """Parse a string to float, returning 0.0 on failure."""
    if not value or value.strip() in ("", "-", "N/A"):
        return 0.0
    try:
        return float(value.strip().replace(",", ""))
    except ValueError:
        logger.debug("Could not parse float: %r", value)
        return 0.0


def read_csv_from_bytes(data: bytes) -> list[dict]:
    """Read CSV bytes into a list of dicts. Handles UTF-8-BOM."""
    # Try UTF-8, fall back to latin-1 for legacy encodings
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("Could not decode CSV data with any supported encoding")

    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


def fetch_from_cos(bucket: str, region: str, key: str) -> bytes:
    """Read an object from COS via the SCF-internal network (no auth needed
    when the SCF role has the right CAM policy)."""
    url = COS_ENDPOINT.format(bucket=bucket, region=region, key=key)
    logger.info("Fetching %s", url)
    req = Request(url)
    try:
        with urlopen(req, timeout=60) as resp:
            return resp.read()
    except URLError as e:
        raise RuntimeError(f"Failed to fetch COS object {key}: {e}")


def put_to_cos(bucket: str, region: str, key: str, body: bytes, content_type: str = "text/csv"):
    """Write an object to COS via HTTP PUT."""
    url = COS_ENDPOINT.format(bucket=bucket, region=region, key=key)
    logger.info("Uploading to %s (%d bytes)", url, len(body))
    req = Request(url, data=body, method="PUT")
    req.add_header("Content-Type", content_type)
    try:
        with urlopen(req, timeout=60) as resp:
            if resp.status not in (200, 204):
                raise RuntimeError(f"PUT failed: {resp.status} {resp.read()}")
    except URLError as e:
        raise RuntimeError(f"Failed to write COS object {key}: {e}")


def decompress(data: bytes, filename: str) -> bytes:
    """If the file is a zip/gz, extract the first CSV inside. Otherwise return as-is."""
    lower = filename.lower()
    if lower.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not csv_names:
                raise ValueError("No CSV found inside the zip archive")
            return zf.read(csv_names[0])
    elif lower.endswith(".gz"):
        return gzip.decompress(data)
    return data


def aggregate(rows: list[dict]) -> list[dict]:
    """Group rows and aggregate numeric columns."""
    groups: dict[tuple, dict] = {}

    for row in rows:
        # Derive day columns
        start_day = derive_day(row.get("Usage Start Time", ""))
        end_day   = derive_day(row.get("Usage End Time", ""))

        # Build group key
        key_parts = []
        for col in GROUP_COLUMNS:
            if col == "StartDay":
                key_parts.append(start_day)
            elif col == "EndDay":
                key_parts.append(end_day)
            else:
                key_parts.append(row.get(col, "").strip())
        group_key = tuple(key_parts)

        if group_key not in groups:
            # Initialise group
            entry = {}
            for col in GROUP_COLUMNS:
                if col == "StartDay":
                    entry[col] = start_day
                elif col == "EndDay":
                    entry[col] = end_day
                else:
                    entry[col] = row.get(col, "").strip()
            for col in PASS_THROUGH_COLUMNS:
                entry[col] = row.get(col, "").strip()
            for col in SUM_COLUMNS:
                entry[col] = 0.0
            groups[group_key] = entry

        grp = groups[group_key]

        # Sum numeric columns
        for col in SUM_COLUMNS:
            grp[col] += safe_float(row.get(col, "0"))

        # Pass-through columns: keep first non-empty value for each
        for col in PASS_THROUGH_COLUMNS:
            if not grp.get(col):
                val = row.get(col, "").strip()
                if val:
                    grp[col] = val

    return list(groups.values())


def write_csv(rows: list[dict]) -> bytes:
    """Serialize aggregated rows to CSV bytes."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


# ---------------------------------------------------------------------------
# SCF Entry Point
# ---------------------------------------------------------------------------

def main_handler(event, context):
    """
    Receives either:
      - COS trigger event: {"Records": [{"cos": {"cosObject": {"key": "...", "bucket": "..."}}}]}
      - Timer trigger event: reads SOURCE_BUCKET + SOURCE_KEY from env vars
    """
    logger.info("Event: %s", json.dumps(event, default=str))

    # --- Determine source CSV location ---
    source_bucket = ""
    source_key    = ""

    if "Records" in event:
        # COS trigger
        record = event["Records"][0]["cos"]
        cos_info = record.get("cosObject", record.get("object", record))
        source_bucket = (cos_info.get("bucket", {}) if isinstance(cos_info.get("bucket"), dict)
                         else cos_info.get("name", ""))
        source_key    = cos_info.get("key", "")
        # COS trigger encodes the key; urllib may need it, but our fetch
        # function uses it as-is (SCF internal network handles encoding).
    else:
        # Timer / manual trigger — use env vars
        source_bucket = os.environ.get("SOURCE_BUCKET", "")
        source_key    = os.environ.get("SOURCE_KEY", "")

    if not source_bucket or not source_key:
        raise ValueError(
            "Missing source bucket/key. "
            "Set SOURCE_BUCKET/SOURCE_KEY env vars for timer triggers, "
            "or use a COS trigger."
        )

    if not DEST_BUCKET:
        raise ValueError(
            "DEST_BUCKET env var is required. Set it to the COS bucket "
            "where aggregated CSVs should be written."
        )

    # --- Download & decompress ---
    raw = fetch_from_cos(source_bucket, SOURCE_REGION, source_key)
    csv_data = decompress(raw, source_key)

    # --- Parse & aggregate ---
    rows = read_csv_from_bytes(csv_data)
    logger.info("Parsed %d rows", len(rows))

    aggregated = aggregate(rows)
    logger.info("Aggregated to %d rows", len(aggregated))

    # --- Write output CSV to destination COS ---
    output_csv = write_csv(aggregated)

    # Build output key: prefix + original filename (sans extension) + _aggregated.csv
    base_name = source_key.rsplit("/", 1)[-1]  # filename only
    stem = base_name.rsplit(".", 1)[0]          # remove .csv/.zip/.gz
    dest_key = f"{DEST_KEY_PREFIX}{stem}_aggregated.csv"

    put_to_cos(DEST_BUCKET, DEST_REGION, dest_key, output_csv)

    logger.info("Done. %d rows written to s3://%s/%s", len(aggregated), DEST_BUCKET, dest_key)

    return {
        "status": "ok",
        "source_rows": len(rows),
        "aggregated_rows": len(aggregated),
        "destination": f"s3://{DEST_BUCKET}/{dest_key}",
    }
