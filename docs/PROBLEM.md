# Problem Statement: evidence-check

## The problem

Every business that pays out money on the strength of evidence (insurers paying claims, companies
reimbursing expenses, lenders approving loans, marketplaces granting refunds) now faces the same new
threat: **evidence that looks perfect and is fake.**

- AI-generated receipts went from **0% to about 71% of the fake receipts** flagged by one large
  expense-audit platform between March 2025 and May 2026
  ([PYMNTS](https://www.pymnts.com/news/artificial-intelligence/2026/ai-generated-fake-receipts-now-make-up-71percent-of-expense-fraud/)).
  A survey found that **4 in 10 U.S. employees** have used AI to generate a fake receipt.
- In insurance, AI-related fraud cases have **nearly tripled in two years**, and AI-generated receipts
  are now the most common form, ahead of altered medical documents and fake damage photos
  ([ClaimsPages](https://www.claimspages.com/news/ai-insurance-fraud-cases-nearly-triple-as-deepfakes-and-fake-documents-challenge-claims-adjusters-20260723/)).
  **99% of insurers** say they have already seen manipulated or AI-altered documents
  ([InsurTech Amsterdam, citing Verisk](https://insurtechamsterdam.com/blog/ai-detect-manipulated-images-fake-documents-insurance)).

Human reviewers can no longer tell by looking. And checking one image at a time is not enough:
a generated receipt can be pixel-perfect.

## Why existing answers fall short

| Option | Problem |
|---|---|
| Visual review by staff | AI fakes are indistinguishable by eye |
| Single-image "AI detector" models | Easy to evade, and silent on the real question: *does this evidence fit the rest of the case?* The ones on Hugging Face are small hobby models |
| Commercial tools (AppZen, Verisk, Five Sigma, SAS) | Closed, expensive, per-document pricing, can't be audited or self-hosted |
| Open-source projects | Research prototypes for single ID documents. Nothing open checks a **whole case**: receipts, photos, documents and statements together |

What vendors themselves say works is **layered evidence checking**: authenticity signals *plus*
consistency with everything else (card transactions, dates, locations, the claim itself, the customer's
history). An AI-generated receipt has no matching card transaction. Its tax math may be impossible.
The "damage photo" may have been taken before the policy began, or already used in someone else's claim.

## What we build

An open-source, self-hosted **evidence integrity engine**. Give it a case (a claim, an expense report, a
loan file) made of mixed evidence: receipt and invoice images, PDFs, photos, voice statements, and the
case's own data. It answers three questions, with the evidence for each:

1. **Is each item authentic?** File forensics (metadata, edit traces, content credentials such as C2PA),
   image forensics (compression and noise inconsistencies, copy-paste regions), and AI-generation detection.
2. **Does the evidence agree with itself and with the case?** Receipt arithmetic and tax rates, totals vs
   the claimed amount, dates across receipt, photo metadata, incident and policy, merchant vs card
   transaction, photo content vs the described damage, written vs spoken statements.
3. **Have we seen it before?** Near-duplicate photos and receipts across all past cases, even when cropped,
   resized or re-photographed: the same damage photo reused across customers.

The result is an **explainable risk report**: every flag points at the exact region of the image or line
of text that triggered it. It feeds a reviewer queue and is exposed as tools for AI agents (MCP).

**Multimodal models from Hugging Face**, run locally and free:

| Model type | Job |
|---|---|
| Vision-language model | Read receipts and documents; describe photos |
| Image-text embeddings | Check that photos match their descriptions; find near-duplicates |
| Speech-to-text | Transcribe voice statements |
| Detection models | AI-generation signals |

Deterministic code does the forensics, arithmetic and cross-checks: the lesson from the first three projects.

## Why people would clone it

- Insurers, expense-management teams, lenders, auditors and marketplaces all have this problem *now*.
- It is self-hosted (sensitive evidence never leaves the company), auditable, and free.
- It's configurable per domain (claims, expenses, lending), and every check is a plugin.

## Portfolio fit

It shows multimodal AI, system design for an adversarial business problem, explainability, and honest
measurement. It also connects to the other projects:
- Its verdicts can be graded with **shadow-eval**.
- Its policies can come from **sop-to-agent**.
- It can read case data from legacy systems through **legacy-bridge**.

## Success criteria (proposed)

- A reproducible benchmark of cases with planted manipulations: altered totals, AI-generated receipts,
  reused photos, date and location mismatches, impossible tax math, and statement contradictions.
  Detection is reported per manipulation type and per layer.
- Show what **cross-evidence checks** catch that single-image detectors miss.
- Every flag is explainable (highlighted region or quoted text).
- Runs on a laptop CPU, free; the models are swappable for bigger ones on a GPU.
