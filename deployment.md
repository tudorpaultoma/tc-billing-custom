# Tencent Cloud Bill Aggregation SCF — Deployment Guide

## Architecture

```
DescribeBillDetail API              SCF                     COS Dest Bucket
  (billing SDK, paginated)  ──>  index.py  ──PUT──>  aggregated-bills/
  ComponentSet flattened         (group+sum)          202607_aggregated.csv
```

The SCF calls the `DescribeBillDetail` API directly — no dependency on
built-in bill export to COS, no COS source bucket needed.

---

## Step 1 — Create the SCF Execution Role

1. Go to **CAM Console → Roles → Create Role**
2. Select **Tencent Cloud Service** as the trusted entity
3. Choose **SCF (Serverless Cloud Function)**
4. Attach the custom policy below (replace placeholders in the COS resource line):

### Custom Policy (Create by Policy Syntax)

Replace `${region}`, `${appid}`, `${dest-bucket}` with your actual values.

```json
{
  "version": "2.0",
  "statement": [
    {
      "effect": "allow",
      "action": ["finance:DescribeBillDetail"],
      "resource": "*"
    },
    {
      "effect": "allow",
      "action": ["cos:PutObject"],
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
- Destination bucket: full COS bucket name including APPID suffix

### QCS Resource Format

```
qcs::cos:<region>:uid/<appid>:<bucket-name>/*
```

---

## Step 2 — Build the Deployment Package

```bash
bash package.sh
```

This creates `tc-billing-processor.zip` with `index.py` and all SDK dependencies.

> **Note:** If `package.sh` fails on macOS (ARM vs x86 platform mismatch for
> binary wheels), install dependencies into a local venv and zip manually:
> ```bash
> pip install -t build/ -r requirements.txt
> cp index.py build/
> cd build && zip -r ../tc-billing-processor.zip . && cd ..
> rm -rf build/
> ```

---

## Step 3 — Deploy the SCF

1. Go to **SCF Console → Functions → Create**
2. **Runtime:** Python 3.9 (or newer)
3. **Execution Role:** Select the role from Step 1
4. **Code:** Upload `tc-billing-processor.zip`
5. **Handler:** `index.main_handler`
6. **Memory:** 512 MB (the billing SDK and COS SDK have non-trivial footprints)
7. **Timeout:** 300 seconds (large bills with 10k+ lines need time for
   pagination and aggregation)

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MONTH` | **Yes** | — | Bill month in `YYYY-MM` format (e.g. `2026-07`) |
| `DEST_BUCKET` | **Yes** | — | COS bucket for aggregated output |
| `BILLING_REGION` | No | `ap-singapore` | Billing API region (intl endpoint) |
| `DEST_REGION` | No | `BILLING_REGION` | Region of destination COS bucket |
| `DEST_KEY_PREFIX` | No | `aggregated-bills/` | Prefix (folder) for output files |
| `PAGE_LIMIT` | No | `1000` | Records per API page (max allowed) |

---

## Step 4 — Add a Timer Trigger

1. In the SCF console: **Trigger Management → Create Trigger → Timer**
2. **Cron:** `0 3 3 * * *` (3rd of each month at 03:00 UTC)
3. This runs the SCF for the **previous month** — set `MONTH` env var
   dynamically or use a fixed value and update it each month.

> **Tip:** To make `MONTH` dynamic (auto-compute previous month), you can
> modify `index.py` to compute it from `datetime` if `MONTH` is not set.
> The current version requires an explicit `MONTH` env var for clarity.

### Alternative: API Gateway Trigger

You can also trigger the SCF via an API Gateway endpoint and pass `month` in
the request body:
```json
{"month": "2026-07"}
```

---

## Step 5 — Verify

1. Trigger the SCF manually (Console → Test) or wait for the timer.
2. Check the SCF logs in the CLS console.
3. Look for the output file in the destination bucket:
   `aggregated-bills/202607_aggregated.csv`

---

## Output CSV Columns

Same output schema as before — grouped and aggregated:

| Column | Treatment |
|--------|-----------|
| Payer Account ID | Group key |
| Owner Account ID | Group key |
| BillingMode | Group key |
| ProductName | Group key |
| Region | Group key |
| InstanceName | Group key |
| TransactionType | Group key |
| StartDay | Group key (from FeeBeginTime) |
| EndDay | Group key (from FeeEndTime) |
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
