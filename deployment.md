# Tencent Cloud Bill Aggregation SCF — Deployment Guide

## Architecture

```
Billing Center                COS Source Bucket           SCF                     COS Dest Bucket
(Built-in Export) ──monthly──>  bill-csv/         ──COS trigger──>  index.py  ──PUT──>  aggregated-bills/
                               202607-bill.zip                     (group+sum)          202607-bill_aggregated.csv
```

---

## Step 1 — Enable Bill Export to COS (Built-in)

In the Billing Center console: **Bill Overview → Bill Data Storage → Enable**.

- Select "Monthly Bill Details" — delivered on the **2nd** of each month
- Choose the **source COS bucket** (e.g., `mycompany-bills-1234567890`)
- The file lands as a zipped CSV: `1234567890-202607-by_used_time-bill_details.zip`

This gives you the full L3 detailed bill in COS without writing any code.

---

## Step 2 — Create the SCF Execution Role

1. Go to **CAM Console → Roles → Create Role**
2. Select **Tencent Cloud Service** as the trusted entity
3. Choose **SCF (Serverless Cloud Function)**
4. Attach the custom policy below (replace placeholders):

### Custom Policy (Custom Policy → Create by Policy Syntax)

Replace `${region}`, `${appid}`, `${source-bucket}`, `${dest-bucket}` with your
actual values.

```json
{
  "version": "2.0",
  "statement": [
    {
      "effect": "allow",
      "action": ["cos:GetObject", "cos:HeadObject"],
      "resource": [
        "qcs::cos:ap-singapore:uid/1234567890:mycompany-bills-1234567890/*"
      ]
    },
    {
      "effect": "allow",
      "action": ["cos:PutObject", "cos:PutObjectACL"],
      "resource": [
        "qcs::cos:ap-singapore:uid/1234567890:mycompany-reports-1234567890/*"
      ]
    },
    {
      "effect": "allow",
      "action": [
        "cls:CreateLogset",
        "cls:CreateTopic",
        "cls:PutLogs",
        "cls:SearchLog"
      ],
      "resource": "*"
    }
  ]
}
```

### How to get your APPID and bucket names

- APPID: Visible in **Account Info → Account ID** (12-digit number)
- Source bucket: The bucket where bills land (Step 1)
- Destination bucket: The bucket where aggregated CSVs go (create one if needed)

### QCS Resource Format

```
qcs::cos:<region>:uid/<appid>:<bucket-name>/*
```

Example for bucket `my-bills-1234567890` in Singapore:
```
qcs::cos:ap-singapore:uid/1234567890:my-bills-1234567890/*
```

---

## Step 3 — Deploy the SCF

1. Go to **SCF Console → Functions → Create**
2. **Runtime:** Python 3.9 (or newer)
3. **Execution Role:** Select the role from Step 2
4. **Code:** Upload `index.py` (no external dependencies — stdlib only)
5. **Environment Variables:**

| Variable | Value | Description |
|----------|-------|-------------|
| `DEST_BUCKET` | `mycompany-reports-1234567890` | Destination COS bucket |
| `DEST_REGION` | `ap-singapore` | COS region for both buckets |
| `DEST_KEY_PREFIX` | `aggregated-bills/` | Prefix (folder) for output files |

6. **Memory:** 256 MB (more than enough; CSV processing is lightweight)
7. **Timeout:** 300 seconds (bills with 10k+ lines may need a minute or two)

### Trigger — Option A: COS Event (Recommended)

In the SCF console: **Trigger Management → Create Trigger → COS**.

- **Bucket:** Source bucket (where bills land)
- **Event Type:** `cos:ObjectCreated:Put` or `cos:ObjectCreated:Post`
- **Suffix filter:** `.zip` (or `.csv` if you export uncompressed)

Now every time a new monthly bill lands in the source bucket, the SCF fires
automatically.

### Trigger — Option B: Timer (Fallback)

If you prefer a scheduled run instead of event-driven:

- **Trigger Type:** Timer
- **Cron:** `0 3 3 * * *` (3rd of each month at 03:00 UTC)
- Add env vars `SOURCE_BUCKET` and `SOURCE_KEY` — but with a timer trigger
  you'd need the SCF to **list** objects in the source bucket to find the latest
  bill. The COS trigger (Option A) is simpler and preferred.

---

## Step 4 — Verify

1. Wait for the next monthly bill delivery (2nd of the month) — or manually
   upload a previous bill CSV to the source bucket.
2. Check the SCF logs in the CLS console.
3. Look for the output file in the destination bucket:
   `aggregated-bills/1234567890-202607-by_used_time-bill_details_aggregated.csv`

---

## Output CSV Columns

All columns from the detailed bill, grouped and aggregated:

| Column | Treatment |
|--------|-----------|
| Payer Account ID | Group key |
| Owner Account ID | Group key |
| BillingMode | Group key |
| ProductName | Group key |
| Region | Group key |
| InstanceName | Group key |
| TransactionType | Group key |
| StartDay | Group key (derived from Usage Start Time) |
| EndDay | Group key (derived from Usage End Time) |
| Component Contracted Price | First value per group |
| Component Price Measurement Unit | First value per group |
| Component Usage | **Summed** |
| Component Usage Unit | First value per group |
| OriginalCost | **Summed** |
| RI Deduction (Duration) | **Summed** |
| RI Deduction (Cost) | **Summed** |
| SP Deduction | **Summed** |
| SP Deduction Rate | First value per group |
| SP Deduction(Cost) | **Summed** |
| Discount Multiplier | First value per group |
| Blended Discount Multiplier | First value per group |
| Currency | First value per group |
| Total Amount After Discount (Excluding Tax) | **Summed** |
| Voucher Deduction | **Summed** |
| Amount Before Tax | **Summed** |
| TaxRate | First value per group |
| TaxAmount | **Summed** |
| Total Cost (Including Tax) | **Summed** |
| tag_key:Country | First value per group |
| tag_key:GroupName | First value per group |
| tag_key:Type | First value per group |
| Product Code | First value per group |
| Bill Month | First value per group |

> If two rows within the same group have different values for a pass-through
> column (e.g., different tags), the first non-empty value wins. In practice,
> rows grouped by the same payer/owner/product/day-range should have consistent
> categorical values.
