# tc-billing-custom

Tencent Cloud billing aggregation SCF. Pulls monthly bill detail from the
`DescribeBillDetail` API, flattens the component-level breakdown, groups by
payer / owner / product / day-range, sums all cost columns, and writes the
aggregated CSV to COS.

## What it does

```
DescribeBillDetail API              SCF                     COS
  (paginated, component-level)  ──>  index.py  ──PUT──>  aggregated-bills/
  flattens ComponentSet              (group+sum)          202607_aggregated.csv
```

No manual CSV parsing, no dependency on bill export to COS. The SCF calls the
billing API directly, flattens each detail record's ComponentSet into individual
rows, then aggregates by:

- Payer Account ID, Owner Account ID, BillingMode, ProductName, Region,
  InstanceName, TransactionType, StartDay, EndDay

Summed columns: OriginalCost, RI Deduction, SP Deduction, Total After Discount,
Voucher, Tax, Total Cost Including Tax, and more.

## Quick start

1. **Create a COS bucket** for the aggregated output.
2. **Create a CAM role** — see `policy.json` (needs `billing:DescribeBillDetail`
   + `cos:PutObject`). Replace `${...}` placeholders.
3. **Build the deployment zip:** `bash package.sh`
4. **Deploy to SCF:**
   - Upload `tc-billing-processor.zip`
   - Runtime: Python 3.9+
   - Handler: `index.main_handler`
   - Memory: 512 MB | Timeout: 300s
   - Attach the role from step 2
5. **Set env vars** (see below).
6. **Add a Timer trigger** — e.g., `0 3 3 * * *` (3rd of each month at 03:00).

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MONTH` | **Yes** | — | Bill month in `YYYY-MM` format (e.g. `2026-07`) |
| `DEST_BUCKET` | **Yes** | — | COS bucket for aggregated output |
| `BILLING_REGION` | No | `ap-singapore` | Billing API region |
| `DEST_REGION` | No | `BILLING_REGION` | COS region for output |
| `DEST_KEY_PREFIX` | No | `aggregated-bills/` | Output folder prefix |
| `PAGE_LIMIT` | No | `1000` | Records per API page |

> Auth is automatic — the SCF execution role injects `TENCENTCLOUD_SECRETID`,
> `TENCENTCLOUD_SECRETKEY`, and `TENCENTCLOUD_SESSIONTOKEN` at runtime.
> No need to store credentials in env vars.

## Files

| File | Purpose |
|------|---------|
| `index.py` | SCF handler — API fetch, flatten, aggregate, COS upload |
| `requirements.txt` | Dependencies (tencentcloud-sdk-python, cos-python-sdk-v5) |
| `policy.json` | CAM custom policy for the SCF execution role |
| `deployment.md` | Full step-by-step setup guide |
| `package.sh` | Build the deployment zip with all dependencies |
| `.gitignore` | Ignores zips, build artifacts, Python/IDE files |

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
