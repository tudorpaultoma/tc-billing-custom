# tc-billing-custom

Tencent Cloud billing aggregation SCF. Pulls monthly bill detail from the
`DescribeBillDetail` API, flattens the component-level breakdown, groups by
payer / owner / product / day-range, sums all cost columns, and writes the
aggregated CSV to COS.

The output columns are **configurable** — a built-in catalog lists every
extractable field, ships with a sensible default set, and lets you turn any
column on/off via a text config (see [Configuring columns](#configuring-columns)).

## What it does

```
DescribeBillDetail API              SCF                     COS
  (paginated, component-level)  ──>  index.py  ──PUT──>  aggregated-bills/
  flattens ComponentSet              (group+sum)          202607_aggregated.csv
```

No manual CSV parsing, no dependency on bill export to COS. The SCF calls the
billing API directly, flattens each detail record's ComponentSet into individual
rows, then aggregates by the configured group keys.

## Quick start

1. **Create a COS bucket** for the aggregated output.
2. **Create a CAM role** — see `policy.json` (needs `finance:DescribeBill*`
   + `cos:PutObject`; add `cos:GetObject` if you load the column config from COS).
   Replace `${...}` placeholders.
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
| `PAGE_LIMIT` | No | `300` | Records per API page (intl endpoint max) |
| `COLUMN_CONFIG` | No | — | Inline column config text (see below) |
| `CONFIG_BUCKET` | No | — | COS bucket holding a column config file |
| `CONFIG_KEY` | No | — | COS object key (path) of the config file |
| `CONFIG_REGION` | No | `DEST_REGION` | Region of the config bucket |

> Auth is automatic — the SCF execution role injects `TENCENTCLOUD_SECRETID`,
> `TENCENTCLOUD_SECRETKEY`, and `TENCENTCLOUD_SESSIONTOKEN` at runtime.
> No need to store credentials in env vars.

## Configuring columns

The SCF ships with a **field catalog** (in `index.py`, `FIELD_CATALOG`) that
enumerates every column that can be extracted from the `DescribeBillDetail`
response — top-level bill fields, component fields, and a few derived columns.
Each entry declares:

- **kind** — how the column is treated in aggregation:
  - `group` — dimension; each unique combination becomes one output row
  - `sum` — numeric; values are summed within each group
  - `pass-through` — text; the first non-empty value per group is kept
- **default** — `IN` (included out of the box) or `OUT` (opt-in)

The default set reproduces the original fixed schema, so behavior is unchanged
unless you override it.

### The config file

A plain text file where each non-comment line selects a column:

```
# Lines starting with # are ignored.
ZoneName = IN          # add an opt-in column
OriginalCost = OUT     # drop a default column
```

Accepted forms: `Column = IN`, `Column,IN`, or `Column IN`. Values are
case-insensitive. A full template listing **every** available column is in
[`columns.example`](columns.example) — copy it, uncomment what you need, and
change `IN`/`OUT` as desired.

### Where to put it

Two options (both optional — with no config, built-in defaults apply):

1. **COS file** (recommended — reconfigure without redeploying):
   upload the config text as a COS object, then set `CONFIG_BUCKET`,
   `CONFIG_KEY`, and optionally `CONFIG_REGION`.
   *Requires `cos:GetObject` on the config bucket in the execution role.*
2. **Inline env var**: set `COLUMN_CONFIG` to the raw text (note SCF env vars
   are size-limited, so this suits small overrides only).

Both can be combined; the inline `COLUMN_CONFIG` is applied first, then COS
overrides it on conflict.

### Tags

Tag columns are dynamic: any tag key on a resource becomes a column named
`tag_key:<Key>`. By default only `tag_key:Country`, `tag_key:GroupName`, and
`tag_key:Type` are included. Control them with:

```
tag_key:* = IN              # include every tag key found in the bill
tag_key:Environment = IN    # include a specific key
tag_key:Team = OUT          # drop a specific key
```

### Notes and gotchas

- **Kind is fixed.** You can only toggle a column on/off, not change whether it
  is grouped, summed, or passed through — this prevents e.g. summing a text field.
- **Group cardinality.** Adding a high-cardinality field (e.g. `ResourceId`) as
  a group key will multiply the number of output rows.
- **SP Deduction fix.** `SP Deduction` now maps to the API's `SPDeduction`
  (actual savings-plan deduction) and `SP Deduction(Cost)` maps to
  `OriginalCostWithSP` — previously both were bound to the same field.

## Files

| File | Purpose |
|------|---------|
| `index.py` | SCF handler — API fetch, flatten, aggregate, COS upload |
| `columns.example` | Template listing every configurable column |
| `requirements.txt` | Dependencies (tencentcloud-sdk-python) |
| `policy.json` | CAM custom policy for the SCF execution role |
| `deployment.md` | Full step-by-step setup guide |
| `package.sh` | Build the deployment zip with all dependencies |
| `.gitignore` | Ignores zips, build artifacts, Python/IDE files |

## Output CSV columns

The default output (no config) is:

**Group keys** (one row per unique combination): Payer Account ID,
Owner Account ID, BillingMode, ProductName, Region, InstanceName,
TransactionType, StartDay, EndDay.

**Pass-through** (first value per group): Component Contracted Price,
Component Price Measurement Unit, Component Usage Unit, SP Deduction Rate,
Discount Multiplier, Blended Discount Multiplier, Currency, TaxRate,
Product Code, Bill Month, plus `tag_key:Country`, `tag_key:GroupName`,
`tag_key:Type`.

**Aggregated (summed)**: Component Usage, OriginalCost, RI Deduction (Duration),
RI Deduction (Cost), SP Deduction, SP Deduction(Cost),
Total Amount After Discount (Excluding Tax), Voucher Deduction,
Amount Before Tax, TaxAmount, Total Cost (Including Tax).

See [`columns.example`](columns.example) for the full list of opt-in columns.

## License

See [LICENSE](LICENSE).
