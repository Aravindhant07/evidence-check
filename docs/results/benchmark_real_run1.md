> **Run 1 with real models, kept for the record.** It surfaced two problems that the oracle run could not:
> 1. The 7 "false alarms" were real: the generator reused the same genuine car photo in different clean cases
>    (perceptual-hash distance 0/64). The duplicate detector was right and the ground truth was wrong. Fixed by dealing photos
>    without replacement.
> 2. One missed expense: Florence-2 fell into a repetition loop on the dashed separator line and never read the totals,
>    so the payment check could not run, silently. Fixed with an OCR second look (repetition penalty, then bottom-half crop)
>    and an `unreadable` check that sends a receipt with no readable total to a person.
> See `benchmark_real.md` for the run after the fixes.

# Benchmark: real perception, seeds 11,12

92 cases (48 clean, 44 manipulated). Median 9.91 s per case, p95 43.42 s.

## Layer ablation

| Layers | Manipulations caught | False alarms on clean cases |
|---|---|---|
| Pixel forensics only (typical single-image detector) | 3/44 = **7%** (95% CI 2%–18%) | 0/48 = 0% (95% CI 0%–7%) |
| + file metadata and provenance | 9/44 = **20%** (95% CI 11%–35%) | 0/48 = 0% (95% CI 0%–7%) |
| + cross-evidence consistency (receipts, card, dates, GPS, photo content, statement) | 42/44 = **95%** (95% CI 85%–99%) | 0/48 = 0% (95% CI 0%–7%) |
| + history (reuse across cases and customers) | 43/44 = **98%** (95% CI 88%–100%) | 7/48 = 15% (95% CI 7%–27%) |

## Per manipulation (all layers)

| Domain | Manipulation | Caught | For the right reason | Mean score |
|---|---|---|---|---|
| insurance claims | edited total | 2/2 | 2/2 | 0.76 |
| insurance claims | generator receipt | 2/2 | 2/2 | 0.99 |
| insurance claims | flat receipt no card | 2/2 | 2/2 | 0.76 |
| insurance claims | wrong tax | 2/2 | 2/2 | 0.72 |
| insurance claims | no card txn | 2/2 | 2/2 | 0.51 |
| insurance claims | photo before policy | 2/2 | 2/2 | 0.89 |
| insurance claims | photo before incident | 2/2 | 2/2 | 0.53 |
| insurance claims | gps mismatch | 2/2 | 2/2 | 0.57 |
| insurance claims | amount inflated | 2/2 | 2/2 | 0.64 |
| insurance claims | copy move photo | 2/2 | 2/2 | 0.83 |
| insurance claims | edited photo software | 2/2 | 2/2 | 0.50 |
| insurance claims | reused photo | 2/2 | 2/2 | 0.99 |
| insurance claims | wrong loss type | 2/2 | 2/2 | 0.97 |
| insurance claims | statement contradiction | 2/2 | 2/2 | 0.98 |
| expense reports | edited total | 2/2 | 2/2 | 0.83 |
| expense reports | generator receipt | 2/2 | 2/2 | 0.82 |
| expense reports | flat receipt no card | 1/2 **(missed some)** | 1/2 | 0.37 |
| expense reports | wrong tax | 2/2 | 2/2 | 0.45 |
| expense reports | no card txn | 2/2 | 2/2 | 0.66 |
| expense reports | receipt date mismatch | 2/2 | 2/2 | 0.48 |
| expense reports | amount inflated | 2/2 | 2/2 | 0.54 |
| expense reports | reused receipt | 2/2 | 2/2 | 0.94 |

## Which checks fired

| Check | Clean cases | Manipulated cases |
|---|---|---|
| amount_match | 0 | 4 |
| card_match | 0 | 13 |
| copy_move | 0 | 3 |
| dates | 0 | 6 |
| duplicate_evidence | 7 | 14 |
| error_level | 6 | 13 |
| generator_traces | 0 | 4 |
| gps_location | 0 | 2 |
| metadata | 0 | 10 |
| photo_vs_claim | 0 | 2 |
| receipt_math | 0 | 4 |
| statement | 0 | 7 |
| tax_rate | 0 | 6 |
