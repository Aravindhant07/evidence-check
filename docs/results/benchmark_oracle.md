# Benchmark: oracle perception, seeds 11,12,13,14,15,16,17,18,19,20

> **What this measures.** Checks only, with perfect perception: OCR text and statement transcripts come from the
> generator's ground truth ("oracle perception"). It shows that each check catches the manipulation it targets and
> stays quiet on clean cases, and how much each layer adds. It does **not** measure real-world recall: the
> manipulations are ones we planted. See `benchmark_real.md` for the same suite read by the real models.
> Reproduce: `python -m evidence_check.bench.run --models oracle --seeds 11,12,13,14,15,16,17,18,19,20`

460 cases (240 clean, 220 manipulated). Median 0.19 s per case, p95 0.46 s.

## Layer ablation

| Layers | Manipulations caught | False alarms on clean cases |
|---|---|---|
| Pixel forensics only (typical single-image detector) | 10/220 = **5%** (95% CI 2%–8%) | 0/240 = 0% (95% CI 0%–2%) |
| + file metadata and provenance | 40/220 = **18%** (95% CI 14%–24%) | 0/240 = 0% (95% CI 0%–2%) |
| + cross-evidence consistency (receipts, card, dates, GPS, photo content, statement) | 208/220 = **95%** (95% CI 91%–97%) | 0/240 = 0% (95% CI 0%–2%) |
| + history (reuse across cases and customers) | 220/220 = **100%** (95% CI 98%–100%) | 0/240 = 0% (95% CI 0%–2%) |

## Per manipulation (all layers)

| Domain | Manipulation | Caught | For the right reason | Mean score |
|---|---|---|---|---|
| insurance claims | edited total | 10/10 | 10/10 | 0.76 |
| insurance claims | generator receipt | 10/10 | 10/10 | 0.91 |
| insurance claims | flat receipt no card | 10/10 | 10/10 | 0.54 |
| insurance claims | wrong tax | 10/10 | 10/10 | 0.45 |
| insurance claims | no card txn | 10/10 | 10/10 | 0.52 |
| insurance claims | photo before policy | 10/10 | 10/10 | 0.81 |
| insurance claims | photo before incident | 10/10 | 10/10 | 0.51 |
| insurance claims | gps mismatch | 10/10 | 10/10 | 0.48 |
| insurance claims | amount inflated | 10/10 | 10/10 | 0.73 |
| insurance claims | copy move photo | 10/10 | 10/10 | 0.51 |
| insurance claims | edited photo software | 10/10 | 10/10 | 0.48 |
| insurance claims | reused photo | 10/10 | 10/10 | 0.88 |
| insurance claims | wrong loss type | 10/10 | 10/10 | 0.42 |
| insurance claims | statement contradiction | 10/10 | 10/10 | 0.57 |
| expense reports | edited total | 10/10 | 10/10 | 0.83 |
| expense reports | generator receipt | 10/10 | 10/10 | 0.94 |
| expense reports | flat receipt no card | 10/10 | 10/10 | 0.68 |
| expense reports | wrong tax | 10/10 | 10/10 | 0.45 |
| expense reports | no card txn | 10/10 | 10/10 | 0.66 |
| expense reports | receipt date mismatch | 10/10 | 10/10 | 0.48 |
| expense reports | amount inflated | 10/10 | 10/10 | 0.54 |
| expense reports | reused receipt | 10/10 | 10/10 | 0.93 |

## Which checks fired

| Check | Clean cases | Manipulated cases |
|---|---|---|
| amount_match | 0 | 20 |
| card_match | 0 | 80 |
| copy_move | 0 | 10 |
| dates | 0 | 34 |
| duplicate_evidence | 0 | 20 |
| error_level | 5 | 8 |
| generator_traces | 0 | 20 |
| gps_location | 0 | 10 |
| metadata | 0 | 50 |
| receipt_math | 0 | 20 |
| statement | 0 | 29 |
| tax_rate | 0 | 28 |
