# tc-billing-custom

Tencent Cloud billing aggregation SCF. Reads a monthly detailed bill CSV from a
COS bucket, groups it by payer / owner / product / day-range, sums all cost
columns, and writes the aggregated CSV to a destination COS bucket.

## What it does

```
COS source bucket                 COS dest bucket
  monthly-bill-details.zip  ──>  aggregated-bills/
  (built-in bill export)         202607-bill_aggregated.csv
        │
        └── COS trigger ──> SCF (index.py)
                              groups by: Payer Account ID, Owner Account ID,
                                         ProductName, BillingMode, Region,
                                         InstanceName, TransactionType,
                                         StartDay, EndDay
                              sums:      OriginalCost, RI Deduction,
                                         SP Deduction, Total After Discount,
                                         Voucher, Tax, Total Cost, ...
```

## Quick start

1. **Enable bill export to COS** in the Billing Center console
   (Bill Overview → Bill Data Storage → Monthly Bill Details).
2. **Create two COS buckets** — one for incoming bills, one for output.
3. **Create a CAM role** with `policy.json` (replace `${...}` placeholders).
4. **Build the deployment zip:** `bash package.sh`
5. **Deploy to SCF** — upload `tc-billing-processor.zip`, set runtime to
   Python 3.9+, handler to `index.main_handler`.
6. **Set env vars** (see below).
7. **Add a COS trigger** on the source bucket, suffix filter `.zip`.

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DEST_BUCKET` | **Yes** | — | COS bucket for aggregated output |
| `DEST_REGION` | No | `ap-singapore` | Region of destination bucket |
| `DEST_KEY_PREFIX` | No | `aggregated-bills/` | Prefix (folder) for output files |
| `SOURCE_REGION` | No | `DEST_REGION` | Region of source bucket (if different) |
| `SOURCE_BUCKET` | Timer only | — | Source bucket (timer triggers only) |
| `SOURCE_KEY` | Timer only | — | Source object key (timer triggers only) |

## Files

| File | Purpose |
|------|---------|
| `index.py` | SCF handler — stdlib only, no dependencies |
| `policy.json` | CAM custom policy for the SCF execution role |
| `deployment.md` | Full step-by-step setup guide |
| `package.sh` | Build the deployment zip |
| `requirements.txt` | Empty (stdlib only) — kept for tooling compat |

## Output CSV columns

Group keys (one row per unique combination):

- Payer Account ID, Owner Account ID, BillingMode, ProductName, Region,
  InstanceName, TransactionType, StartDay, EndDay

Pass-through (first value per group):

- Component Contracted Price, Component Price Measurement Unit,
  Component Usage Unit, SP Deduction Rate, Discount Multiplier,
  Blended Discount Multiplier, Currency, TaxRate, tag_key:*, Product Code,
  Bill Month

Aggregated (summed):

- Component Usage, OriginalCost, RI Deduction (Duration), RI Deduction (Cost),
  SP Deduction, SP Deduction(Cost), Total Amount After Discount (Excluding Tax),
  Voucher Deduction, Amount Before Tax, TaxAmount, Total Cost (Including Tax)

## License

See [LICENSE](LICENSE).
