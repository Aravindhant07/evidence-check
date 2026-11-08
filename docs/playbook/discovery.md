# Discovery (week 1)

Goal: understand the client's problem well enough to say whether this system will pay for itself, and what has to be
adapted. Run as two or three working sessions with the claims or expense operations lead, an investigator, someone from
IT, and someone from legal or compliance.

## Questions

**The problem**
- How many claims or expense reports a day? What share have photos, receipts, PDFs, voice statements?
- What fraud do you see today? Ask for five recent confirmed cases and walk through how each was caught (or found late).
- What does a missed case cost on average? What does a review cost (time × loaded rate)?
- What share of cases is fraudulent, by your best estimate? (Industry figures are often 5–10% for claims with some
  exaggeration, far lower for outright fabrication. Use theirs.)

**The current process**
- Who looks at evidence today, how long per case, and with what tools?
- How many cases a day can the review or special-investigations team take on? This sets the threshold.
- What happens after a flag: who decides, how is the customer told, how are appeals handled?

**Systems and data**
- Where do cases and files live (claims system, expense tool, document store)? Is there an API, a webhook, an export?
- Are card transactions or payment records available for matching receipts? Policy start/end dates? Incident location?
- Are past outcomes recorded (confirmed fraud, paid, denied, withdrawn)? For how many cases?

**Constraints**
- Can data leave their network? (evidence-check runs fully on-premises on CPU; no external calls at inference.)
- Data retention rules, regions, PII handling, who may see what.
- Regulatory requirements on automated decisions and explanations (e.g. GDPR Art. 22, state insurance rules).
- Fairness concerns: which customer segments must be monitored?

## Go / no-go

Go when all of these hold:
- At least a few hundred past cases with files can be exported, ideally with outcomes.
- The review team has capacity for the expected flag volume (rough check: volume × 3–6%).
- There is a clear owner on the client side for the pilot.
- Legal agrees that flags are recommendations reviewed by people.

## Output

A two-page note: their fraud patterns mapped to the checks that address them, gaps (patterns no check covers yet), the
integration route, the expected review volume, a first ROI range, and the pilot plan with dates.
