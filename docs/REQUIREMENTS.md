# Requirements: evidence-check

## Users

| User | Needs |
|---|---|
| **Claims adjuster / expense auditor / underwriter** | A ranked queue of risky cases, with the exact reason for each flag highlighted |
| **Fraud / special investigations team** | Links between cases: the same photo, receipt template, device or merchant across customers |
| **Engineer / FDE** | Self-hosted, configurable per domain, an API and MCP tools, swappable models |
| **Compliance / audit** | Explainable, reproducible decisions; audit trail; no evidence leaves the premises |

## Functional requirements (proposed)

| ID | Requirement |
|---|---|
| R1 | **Case bundle input:** case data (JSON: amounts, dates, location, policy, card transactions) plus evidence files (JPEG/PNG/PDF/audio) |
| R2 | **Extraction (multimodal models):** receipts/invoices → merchant, date, line items, tax, total; photos → description and tags; audio → transcript; PDFs → text and structure |
| R3 | **File forensics:** EXIF/XMP (capture time, device, software, GPS), PDF producer and incremental edits, C2PA content credentials, stripped or contradictory metadata |
| R4 | **Image forensics:** error-level analysis, noise and compression inconsistency maps, copy-move detection, AI-generation detection model, with heatmaps |
| R5 | **Cross-evidence checks:** line items + tax = total; tax rate valid for the jurisdiction; total vs claimed amount; date consistency (receipt, photo, incident, policy period, filing); receipt vs card transaction; photo content vs claimed loss type; written vs spoken statement contradictions; GPS vs incident location |
| R6 | **History checks:** perceptual-hash and embedding index of all evidence; near-duplicates across cases and customers; repeated device or template fingerprints |
| R7 | **Risk scoring:** each check produces a finding (severity, confidence, evidence pointer); a configurable scoring policy gives the case verdict: clear / review / investigate |
| R8 | **Explainability:** every finding points at an image region (box or heatmap) or a quoted text span; the report shows them |
| R9 | **Domain configs (YAML):** insurance claims, expense reports, lending documents, with checks, thresholds and tax tables |
| R10 | **Reviewer UI:** queue, case view with evidence and highlights, confirm or dismiss findings (feedback stored for calibration) |
| R11 | **API + MCP server:** `check_case`, `check_evidence`, `find_similar` for agents and other systems |
| R12 | **Benchmark:** generator of realistic cases with planted manipulations (from public receipt datasets plus programmatic edits) with per-type detection metrics and a layer ablation |

## Non-functional

- Runs on CPU, free, offline after model download; GPU optional
- Evidence never leaves the machine; no paid APIs by default
- Every model is swappable through configuration
- Tests run offline (models stubbed where needed)

## Out of scope (v1)

- Video evidence
- Identity-document (passport/ID) verification and face matching
- Training new detection models (we use and compose existing ones)

## Confirmed decisions

| Question | Decision |
|---|---|
| Name | **evidence-check** |
| Domains | **Insurance claims + expense reports** (one engine, two domain configs) |
| Evidence types in v1 | **Images, PDFs and audio** |
| Models | Free local open models from Hugging Face; CPU by default, GPU optional |
