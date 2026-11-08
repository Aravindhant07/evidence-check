> **What this measures.** The same suite as `benchmark_oracle.md` (2 seeds, 92 cases), but read by the real models:
> Florence-2 OCR on photographed receipts, CLIP and captions on real car-damage photos (MIT-licensed dataset), and spoken
> statements generated with an open TTS model and transcribed by Whisper. CPU only (4 cores).
> Run on commit ec4c966, after fixing the two problems found by run 1 (`benchmark_real_run1.md`). Two later fixes are
> not reflected here: copy-move's second look for dark photos, and Whisper's split ordinals ("20th, 8th"), which caused
> the one `statement` signal on a clean case below (it stayed under the review threshold).
> The manipulations are planted by the generator, so this shows the system works end to end with real perception;
> it is not a measurement of real-world fraud recall.
> Reproduce: `python -m evidence_check.bench.run --models real --real-photos --voice tts --seeds 11,12`

# Benchmark: real perception, seeds 11,12

92 cases (48 clean, 44 manipulated). Median 11.19 s per case, p95 136.29 s.

## Layer ablation

| Layers | Manipulations caught | False alarms on clean cases |
|---|---|---|
| Pixel forensics only (typical single-image detector) | 2/44 = **5%** (95% CI 1%–15%) | 0/48 = 0% (95% CI 0%–7%) |
| + file metadata and provenance | 8/44 = **18%** (95% CI 10%–32%) | 0/48 = 0% (95% CI 0%–7%) |
| + cross-evidence consistency (receipts, card, dates, GPS, photo content, statement) | 42/44 = **95%** (95% CI 85%–99%) | 0/48 = 0% (95% CI 0%–7%) |
| + history (reuse across cases and customers) | 44/44 = **100%** (95% CI 92%–100%) | 0/48 = 0% (95% CI 0%–7%) |

## Per manipulation (all layers)

| Domain | Manipulation | Caught | For the right reason | Mean score |
|---|---|---|---|---|
| insurance claims | edited total | 2/2 | 2/2 | 0.78 |
| insurance claims | generator receipt | 2/2 | 2/2 | 0.92 |
| insurance claims | flat receipt no card | 2/2 | 2/2 | 0.54 |
| insurance claims | wrong tax | 2/2 | 2/2 | 0.51 |
| insurance claims | no card txn | 2/2 | 2/2 | 0.56 |
| insurance claims | photo before policy | 2/2 | 2/2 | 0.82 |
| insurance claims | photo before incident | 2/2 | 2/2 | 0.53 |
| insurance claims | gps mismatch | 2/2 | 2/2 | 0.52 |
| insurance claims | amount inflated | 2/2 | 2/2 | 0.76 |
| insurance claims | copy move photo | 2/2 | 2/2 | 0.60 |
| insurance claims | edited photo software | 2/2 | 2/2 | 0.50 |
| insurance claims | reused photo | 2/2 | 2/2 | 0.87 |
| insurance claims | wrong loss type | 2/2 | 2/2 | 0.79 |
| insurance claims | statement contradiction | 2/2 | 2/2 | 0.58 |
| expense reports | edited total | 2/2 | 2/2 | 0.83 |
| expense reports | generator receipt | 2/2 | 2/2 | 0.94 |
| expense reports | flat receipt no card | 2/2 | 2/2 | 0.68 |
| expense reports | wrong tax | 2/2 | 2/2 | 0.45 |
| expense reports | no card txn | 2/2 | 2/2 | 0.66 |
| expense reports | receipt date mismatch | 2/2 | 2/2 | 0.48 |
| expense reports | amount inflated | 2/2 | 2/2 | 0.54 |
| expense reports | reused receipt | 2/2 | 2/2 | 0.94 |

## Which checks fired

| Check | Clean cases | Manipulated cases |
|---|---|---|
| amount_match | 0 | 4 |
| card_match | 0 | 16 |
| copy_move | 0 | 2 |
| dates | 0 | 7 |
| duplicate_evidence | 0 | 4 |
| error_level | 10 | 12 |
| generator_traces | 0 | 4 |
| gps_location | 0 | 2 |
| metadata | 0 | 10 |
| photo_vs_claim | 0 | 2 |
| receipt_math | 0 | 4 |
| statement | 1 | 8 |
| tax_rate | 0 | 6 |
