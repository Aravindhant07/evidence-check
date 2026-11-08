# Investigator agent: first run with a real model

Qwen2.5-3B-Instruct (pinned, local, CPU) wrote briefs for the four riskiest demo cases through the read-only MCP tools.
Every sentence then went through the citation checker (code). 5 sentences kept, 6 dropped; 46–181 s per brief.

| Case | Outcome | Sentence | Cites / reason |
|---|---|---|---|
| EXP-5016 | kept | Receipt E:20f077acfd1a5b54 was previously submitted in case EXP-5009 by a different customer, indicating potential duplicate submission (F0). | F0, E:20f077acfd1a5b54 |
| EXP-5016 | **dropped** | The tax rate on the receipt (8.87%) is not a valid rate in US-WA, suggesting possible fraudulent activity (F1). | announces a decision; the reviewer decides |
| CLM-2015 | kept | The file contains traces of an AI image generator (sdxl), suggesting the receipt might be manipulated. | F0 |
| CLM-2015 | kept | The receipt lacks camera metadata, which is unusual for a genuine photo. | F2 |
| CLM-2015 | **dropped** | There are no card transactions matching the receipt amount, indicating the receipt may be fraudulent. | announces a decision; the reviewer decides |
| CLM-2012 | kept | The photo in CLM-2012 was previously submitted in CLM-2005 by a different customer. | F0 |
| CLM-2012 | **dropped** | The receipt in CLM-2012 matches a receipt from SAFELITE AUTOGLASS dated April 14, 2026, which was also submitted in CLM-2005. | links to CLM-2005 without citing what connects them |
| CLM-2012 | **dropped** | The incident date and location are consistent with both claims. | cites nothing that exists in this case |
| CLM-2012 | **dropped** | The theft claim in CLM-2012 is similar to the theft claim in CLM-2005. | cites nothing that exists in this case |
| CLM-2011 | kept | The receipt's total amount ($3,164.60) does not align with the subtotal and tax ($289.00 + $27.46 = $316.46). | F1, E:3eebf7e847ca84d6 |
| CLM-2011 | **dropped** | The receipt does not match any known card transaction, suggesting it may be fraudulent or altered. | announces a decision; the reviewer decides |

## What this run changed in the checker

- **False drops fixed.** The first version read digits inside identifiers (`E:20f077…`, `CLM-2012`) as numbers the model
  had invented and dropped true sentences. Identifiers are now ignored by the number check.
- **Hedged accusations.** "indicating the receipt may be fraudulent" got through the first version. Any wording that calls
  the claim or the person fraudulent, hedged or not, is now dropped: that judgment belongs to the reviewer.
- **Cross-case hallucination.** On CLM-2012 the model wrote that the *receipt* was also submitted in CLM-2005; only the
  *photo* was. Its date was real, so the number check passed it. Sentences that name another case must now cite what
  connects them.

## Limits

The checker verifies that sentences are grounded (citations exist, numbers and linked cases appear in what is cited) and
that they do not judge. It does not prove that a sentence follows from its sources; a fluent paraphrase that changes
the meaning can still pass. That is why briefs are shown as an aid next to the findings, never instead of them.

Reproduce: `evcheck investigate CASE_ID --data demo-data` (downloads the model on first use).
