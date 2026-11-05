# Design: evidence-check

Approved scope: name **evidence-check** · domains **insurance claims + expense reports** ·
evidence types **images, PDFs and audio** · free local models from Hugging Face, CPU-friendly.

## Architecture

```
  Case bundle: case.json (amounts, dates, location, policy, card transactions) + evidence files
        │
  1. INGEST ── identify type (image / PDF / audio), hash, store, keep originals untouched
        │
  2. EXTRACT (multimodal models, swappable)
        │   receipts & invoices ─ Florence-2 OCR with regions ─► code parses merchant, date, items, tax, total
        │   photos ───────────── Florence-2 caption + CLIP zero-shot labels (loss type, "photo of a screen")
        │   PDFs ─────────────── text layer (pypdf); scanned pages go through OCR
        │   audio ────────────── Whisper transcript ─► code extracts amounts, dates, places, items
        │   every image ──────── CLIP embedding + perceptual hashes
        ▼
  3. CHECK (plugins; each emits Findings with an evidence pointer)
        │   item forensics ─ metadata (EXIF, PDF producer/edits), C2PA content credentials, generator tags,
        │                    error-level analysis, noise inconsistency, copy-move, AI-image classifier
        │   case consistency ─ arithmetic, tax tables, amount vs claim, date windows, card-transaction match,
        │                      photo content vs claimed loss, statement vs case facts, GPS vs incident location
        │   history ─ near-duplicate evidence across all past cases and customers (hash + embedding index)
        ▼
  4. SCORE ── domain policy (YAML): finding weights → verdict CLEAR / REVIEW / INVESTIGATE
        ▼
  5. OUTPUT ── explainable report (boxes and heatmaps on images, quoted text) · reviewer UI ·
               REST API · MCP tools (check_case, check_file, find_similar) · feedback store
```

## Key design choices

| Choice | Why |
|---|---|
| **Models read; code decides.** Models only turn pixels and audio into text, labels and embeddings. Arithmetic, dates, matching, forensics and scoring are deterministic code | Lesson from projects 1 to 3: small models are unreliable reasoners, competent perceivers. Every verdict is reproducible and explainable |
| **Every extracted value is verified against its source.** A receipt total must appear in the OCR text at a known box | Prevents hallucinated values; gives each finding a highlight |
| **Layered evidence, not one detector** | AI fakes beat single-image detectors; they rarely survive cross-checks (no card transaction, impossible tax, reused photo, metadata contradictions) |
| **Plugins with a common Finding format** | New checks or domains without touching the core |
| **History index** | Repeat fraud is invisible case by case: the same photo across customers is the signal |
| **Everything local** | Evidence is sensitive; self-hosting is the main reason to choose this over SaaS |

## Data model

- **Case:** id, domain, claimant, amounts, dates (incident, filed, policy start/end), location (lat/lon,
  address), description, card transactions, evidence list
- **Evidence:** file, type, hashes, metadata, extraction (text lines with boxes, parsed fields, caption,
  labels, transcript), embedding
- **Finding:** check id, severity (info / low / medium / high / critical), confidence, message, pointer
  (file + box, heatmap path, text span or case field), raw details
- **Verdict:** score, level, top findings, version of the policy and models used

## Checks in v1

| Layer | Check | Signal |
|---|---|---|
| Item | Metadata | Editing software in EXIF/XMP, capture date vs file date, stripped metadata on a "phone photo", PDF producer, multiple incremental saves |
| Item | Content credentials | C2PA manifest present? Signed by an AI generator (`trainedAlgorithmicMedia`)? Edits recorded? |
| Item | Generator traces | PNG text chunks and parameters from image generators, AI-image classifier score |
| Item | Error-level analysis / noise | Regions recompressed differently from the rest (e.g. an edited total), with a heatmap |
| Item | Copy-move | Duplicated regions inside one image (cloned digits or damage) |
| Case | Receipt arithmetic | Line items + tax ≠ total; tax rate not valid for the jurisdiction (tax table in YAML) |
| Case | Amount | Evidence total vs claimed or reimbursed amount |
| Case | Dates | Receipt / photo capture / incident / policy period / filing date are in an impossible order |
| Case | Card match | No card transaction matches the receipt (amount, date ± window, merchant) |
| Case | Photo vs claim | Photo labels contradict the claimed loss (fire photo for a water claim); re-photographed screen |
| Case | Statement | Amounts, dates, places or items in the voice statement contradict the written claim |
| Case | Location | Photo GPS far from the incident location |
| History | Near-duplicates | Same or near-same image (pHash + CLIP) in another case, especially another customer's |
| History | Fingerprints | Same device serial or receipt template across unrelated claimants |

## Benchmark

A generator builds realistic cases, then plants manipulations with known ground truth:
- **Evidence:** synthetic receipts rendered as phone photos with realistic metadata; damage photos
  from public datasets; voice statements synthesised with an open text-to-speech model and transcribed
  back with Whisper.
- **Manipulations:**
  - altered total (pixel edit)
  - "AI-generated" receipt (generator metadata, or a C2PA manifest marking AI creation, and no card transaction)
  - impossible tax
  - reused photo (cropped, resized, recompressed) across customers
  - photo taken before the policy started
  - wrong loss type in the photo
  - GPS mismatch
  - statement contradiction
- **Metrics:** detection recall and precision per manipulation type, false-alarm rate on clean cases, and a
  **layer ablation**: single-image detectors only → + metadata → + cross-evidence → + history.

## Production layer

```
  intake: REST / webhook · watched folder · connector · bulk backfill
        │
        ▼
  API (FastAPI) ──► job queue (SQLite-backed; Redis optional) ──► workers (N, CPU) ──► results DB
        │                                                                  │
        ├── auth: API keys + roles (reviewer · investigator · admin)        ├── webhook back to the source system
        ├── PII masking in logs, UI, exports                               └── metrics: flag rate per check/day
        └── audit log (hash-chained): checks, views, decisions                   └── drift alerts
```

| Area | How |
|---|---|
| Deployment | `docker compose up`: api, worker(s), volume for data and models; `evcheck models download` builds an offline bundle; `/healthz`, `/readyz` |
| Volume | Background workers pull jobs from a durable queue; models load once per worker; batch backfill command |
| Integration | `POST /cases` (JSON + files) and signed webhooks out; folder watcher; example connector that pulls from a mock claims API and writes verdicts back |
| Calibration | `evcheck calibrate past_cases/`: per-check precision/recall, a threshold sweep, the threshold for a given daily review capacity, an ROI table (inputs: average fraud loss, review cost per case, cases per day) |
| Security | Role checks on every endpoint; masking rules in the domain YAML (names, card numbers, addresses); audit of who viewed or decided what; retention job deletes expired evidence |
| Monitoring | Daily flag rates per check stored; a drift alert when a rate moves beyond a control band (e.g. 3σ over a 30-day baseline); dashboard page |
| Robustness | Adversarial transforms in the benchmark (screenshot, re-photo simulation, metadata strip, crop/resize, recompress); a table of which checks still fire |
| Responsible use | Verdicts are recommendations; no automatic denial path in the API; fairness report of flag rates by segment (region, channel, claim size); decision record export |
| Playbook | `docs/playbook/`: discovery questionnaire, data checklist, pilot plan (shadow mode, graded with shadow-eval), success metrics, runbook |

## AI evaluation

```
  per-model golden sets ──► model swap gate (config change or CI) ──► model card updated
  benchmark (planted fakes) ──► per-type recall · false alarms · layer ablation
  reviewer decisions ──► live precision per check ──► drift alert
  verdicts vs reviewer decisions ──► shadow-eval readiness report
```

- `evcheck eval models`: OCR field accuracy, label accuracy, word error rate, detector ROC, on versioned sets under `evals/`.
- `evcheck eval system`: the benchmark and the layer ablation.
- **The gate:** `models.yaml` changes fail CI if any metric drops more than its tolerance.

## Threat model (summary; full version in `docs/THREAT_MODEL.md`)

| Threat | Mitigation |
|---|---|
| Prompt injection in evidence text | Models only extract; verdicts come from code; text from evidence is never used as an instruction. A test plants injection text in receipts and photos |
| Evading the AI-image detector | Detector is one weak signal among many; screenshots and re-photos are in the adversarial suite; no check can clear a case alone |
| Poisoned or swapped model weights | Pinned revisions + SHA-256; safetensors only; no remote code; verified offline bundle |
| Malicious files (bombs, scripted PDFs, polyglots) | Size/pixel limits, content sniffing, no script execution, worker isolation and timeouts |
| Data leakage | No network calls at inference; masking; audit of views; retention |
| Insider misuse | Roles; every view and decision audited and hash-chained |
| Automated harm to honest customers | No auto-deny path; humans decide; fairness report; appeal-ready decision record |

## Product and UX

- **First run:** `evcheck demo` seeds sample claims and expense reports, starts the UI and opens the highest-risk case.
- **Case page layout:** a one-line summary of why it was flagged; evidence viewer with toggleable boxes and
  heatmaps; reasons ranked; each reason has a plain sentence, a "show details" for the technical evidence,
  and confirm/dismiss with a reason. Duplicates open side by side with the earlier case.
- **Reviewer flow:** `J/K` next/previous, `C` confirm, `D` dismiss, `?` shortcuts; time-to-decision recorded.
- **Admin settings:** thresholds and checks with an impact preview ("this would flag 31 of the last 500 cases, about 6 a day").
- **Team lead dashboard:** capacity vs queue, flag-rate trends, per-check precision, drift alerts.
- **Standards:** WCAG 2.1 AA, responsive, light/dark; helpful empty and error states.

## Agents and safe self-improvement

```
                 ┌──────────── deterministic verdict engine (unchanged by agents at run time) ────────────┐
                 │                                                                                       │
  flagged case ─►│ INVESTIGATOR AGENT ── read-only tools: find_similar · related_cases · card_transactions │
                 │   └► investigation brief; every sentence must cite evidence (citation check in code)  │
                 └───────────────────────────────────────────────────────────────────────────────────────┘

  RED-TEAM AGENT ── tools: transform evidence, run engine ──► evasions ─┐
  reviewer outcomes (confirmed only) ──────────────────────────────────┼─► IMPROVEMENT AGENT ─► proposal (YAML diff)
  missed fraud found later ────────────────────────────────────────────┘                          │
                                                                                                   ▼
                    EVALUATION GATE (code): benchmark · past outcomes · golden sets · fairness · adversarial suite
                                                                                                   │ pass
                                                                                                   ▼
                          human approval (UI) ─► shadow mode ─► live with monitoring ─► rollback in one click
```

| Piece | How |
|---|---|
| Agent runtime | Small tool-calling loop (as in legacy-bridge); local open model by default; tools exposed over MCP so any agent client can be used |
| Investigator | Read-only tools only; output is a brief with evidence links; a code check drops sentences whose cited evidence does not support them |
| Red team | Composes transforms (crop, rescale, recompress, screenshot frame, re-photo blur/perspective, metadata strip/forge, layout change, injected instructions) guided by which checks fired; keeps variants that evade |
| Improvement proposals | Typed and limited: add/adjust rule, threshold, weight, suppression, new red-team case for the eval set. Free-form code changes are not allowed |
| Search before creativity | Threshold and weight proposals come from a systematic search over past outcomes; the agent explains and packages them, and proposes new rules where search cannot |
| Gate | Rejects any proposal that lowers recall on any manipulation type, raises false alarms beyond tolerance, worsens fairness, or fails the adversarial suite |
| Governance | Proposals are versioned; approvals are audited; rollback restores the previous policy version instantly |

## Interfaces

- **CLI:** `evcheck check CASE_DIR`, `evcheck serve` (UI + API), `evcheck mcp`, `evcheck bench`, `evcheck demo`
- **Reviewer UI (FastAPI + Jinja):**
  - a queue sorted by risk
  - a case page with evidence thumbnails, highlighted regions and heatmaps
  - findings to confirm or dismiss; feedback is stored and per-check precision shown
- **REST API and MCP server**, for AI agents.

## Tech

Python 3.11+, Pillow, OpenCV (headless), ImageHash, c2pa-python, pypdf, NumPy, transformers + torch (CPU),
FastAPI, Typer, SQLite. Default models are small enough for a laptop CPU and configurable:
Florence-2-base (OCR and captions), CLIP ViT-B/32 (embeddings and zero-shot labels), Whisper-base
(speech), an open AI-image classifier, and an open text-to-speech model (benchmark only).

## Milestones

1. Core data model, ingest, case bundles, plugin registry, Finding/Verdict, scoring policy + domain YAML
2. Deterministic checks: metadata, C2PA, generator traces, error-level analysis, noise, copy-move, arithmetic, dates, card match, GPS
3. Model extractors: Florence-2 OCR → receipt parser with box verification; captions; CLIP labels and embeddings; Whisper
4. History index (near-duplicates, fingerprints) + statement check
5. Benchmark generator + metrics + layer ablation
6. Report with highlights, reviewer UI, REST API, MCP server
7. Production layer I: job queue + workers, intake (REST/webhook/folder), connector, backfill, Docker Compose, offline model bundle
8. Production layer II: auth and roles, PII masking, audit log, retention; monitoring and drift alerts
9. Production layer III: calibration + threshold-for-capacity + ROI; adversarial robustness suite; fairness report
10. AI evaluation (model golden sets, swap gate, model cards) and AI security (threat model, injection and file-safety tests, model pinning)
11. Agents: investigator (with citation check), red team, improvement proposals, evaluation gate, approval + shadow + rollback
12. Product polish: first-run demo, plain-language explanations, keyboard review flow, settings with impact preview, team lead dashboard, accessibility pass
13. Playbook docs; real-model benchmark and agent runs; README with results; push
