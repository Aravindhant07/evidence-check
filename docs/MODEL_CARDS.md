# Model cards

Every model runs locally, is pinned to a full commit in `evidence_check/models.yaml`, loads safetensors only and never runs
remote code. Models only perceive (read text, describe, label, transcribe). Code makes every decision.

Numbers below were measured on this repository's data on CPU. They are rerun by the evaluation suite whenever a
model or its revision changes (see milestone 10 in DESIGN.md).

| Job | Model | Used for | What we measured | How the system uses it |
|---|---|---|---|---|
| `ocr_caption` | florence-community/Florence-2-base | Receipt OCR with boxes; photo captions | 3 of 4 photographed receipts parsed fully (total, subtotal, tax, items); the 4th lost its subtotal to an OCR glitch ("219.000"), now tolerated by the parser. 3.5–5 s per receipt warm, ~55 s cold load. | Every extracted value is re-located in the OCR text and boxed; arithmetic, tax and card checks run on the parsed numbers. |
| `image_text` | laion/CLIP-ViT-B-32-laion2B-s34B-b79K | Zero-shot labels (loss type, "photo of a screen"); image embeddings for reuse search | Correct loss labels on real car-damage photos; labels synthetic rendered scenes as "screen", so benchmarks use real photos for claims. | Labels feed `photo_vs_claim` only above a margin; embeddings feed the history index together with perceptual hashes and geometric verification. |
| `speech` | openai/whisper-base | Transcribing voice statements | English transcription on CPU. | Amounts, dates and loss words are extracted from the transcript by code (`checks/statement.py`). |
| `ai_image` | NYUAD-ComNets/NYUAD_AI-generated_images_detector | "Was this image generated?" | **On 60 genuine car-damage photos it scores 22% at ≥ 0.8 and 13% at ≥ 0.99** (median 0.03). Receipts score ~0.002. | **Advisory only.** Reported at INFO severity with zero score weight; it never changes a verdict. AI generation is caught by content credentials, generator metadata and cross-evidence checks instead. |

## Known weaknesses

- AI-image detectors do not transfer across cameras, compression and content; the one above is a clear example.
  Treat any single-image detector as a weak signal.
- OCR quality drops on crumpled, low-light or thermal-faded receipts. When totals cannot be read, the receipt
  checks report that they were skipped instead of guessing.
- CLIP labels are zero-shot and depend on the prompt wording in each domain policy (`loss_labels`).
