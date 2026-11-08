# Robustness to laundering

Every image in the suite was transformed before checking (clean cases too, so earlier photos in the history were laundered as well). Perception is oracle (ground-truth text), so this isolates what the checks themselves survive.

| Transform | Manipulations caught | False alarms on clean cases |
|---|---|---|
| none | 66/66 (100%) | 0/48 |
| recompress | 66/66 (100%) | 0/48 |
| resize | 65/66 (98%) | 0/48 |
| crop | 66/66 (100%) | 0/48 |
| strip metadata | 54/66 (82%) | 0/48 |
| screenshot | 57/66 (86%) | 0/48 |
| rephoto | 53/66 (80%) | 1/48 |

## Caught, per manipulation and transform

| Manipulation | none | recompress | resize | crop | strip metadata | screenshot | rephoto |
|---|---|---|---|---|---|---|---|
| expense: amount inflated | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| expense: edited total | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| expense: flat receipt no card | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| expense: generator receipt | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| expense: no card txn | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| expense: receipt date mismatch | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| expense: reused receipt | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| expense: wrong tax | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| insurance: amount inflated | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| insurance: copy move photo | ✓ | ✓ | 2/3 | ✓ | ✓ | ✓ | 1/3 |
| insurance: edited photo software | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ | 1/3 |
| insurance: edited total | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| insurance: flat receipt no card | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| insurance: generator receipt | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| insurance: gps mismatch | ✓ | ✓ | ✓ | ✓ | ✗ | 1/3 | ✗ |
| insurance: no card txn | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| insurance: photo before incident | ✓ | ✓ | ✓ | ✓ | ✗ | 1/3 | ✗ |
| insurance: photo before policy | ✓ | ✓ | ✓ | ✓ | ✗ | 1/3 | ✗ |
| insurance: reused photo | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| insurance: statement contradiction | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| insurance: wrong loss type | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| insurance: wrong tax | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

## What this means

- **Text, payments and history survive laundering.** Edited totals, wrong tax, missing card payments, reused receipts,
  reused photos and statement contradictions are caught under every transform, because they rest on what the documents
  say and what else is known, not on pixels.
- **Metadata checks do not.** Photo dates, GPS and editing-software traces disappear when metadata is stripped or the image
  is screenshotted or re-photographed. That is expected, and it is why no single layer decides a case.
- **Deployment lever.** Where the client controls capture (their own app), require in-app capture with content credentials
  (C2PA). The `content_credentials` check then turns a stripped or re-photographed image from "no signal" into a finding.
- Copy-move survives everything except heavy resizing and re-photographing, which destroy the duplicated texture.

Reproduce: `python -m evidence_check.bench.robust --seeds 11,12,13 --clean 8`
