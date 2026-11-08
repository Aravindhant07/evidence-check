# Data checklist (weeks 1–2)

## Case bundle format

One folder per case: `case.json` and the evidence files it lists. See any case under `bench-out/cases/` for a full example.

| Field in case.json | Needed for | If missing |
|---|---|---|
| `case_id`, `domain`, `claimant_id` | everything; history across cases | required |
| `claimed_amount` | amount match, statement amount | amount checks skipped |
| `incident_date`, `filed_date` | photo and receipt date checks | date checks skipped |
| `policy.start`, `policy.end` (claims) | photos older than the policy | that check skipped |
| `location.lat/lon` | photo GPS vs incident location | GPS check skipped |
| `jurisdiction` (e.g. `US-NY`) | tax rate check | tax check skipped |
| `card_transactions` | receipt vs payment match (strongest expense signal) | card check skipped |
| `loss_type` / `category` | photo content vs claim, statement vs claim | content checks skipped |
| `segment` (region, channel, ...) | fairness monitoring | fairness report empty |
| `evidence[].role` | which checks run on each file | treated as `other` |

Skipped checks are listed on every verdict ("Not checked"), so gaps are visible, not silent.

## Files

- Original files, not thumbnails or re-encoded copies: re-encoding strips the metadata several checks rely on.
  Ask specifically whether their document system recompresses uploads.
- Accepted: JPEG, PNG, WebP, HEIC (if converted), PDF, WAV/MP3/M4A/OGG/FLAC. Limits: 25 MB per file, 40 MP per image.

## Labels for calibration

- Outcome per past case: `fraud` (confirmed), `legitimate` (paid without issue), or unknown. Unknowns are left out.
- Prefer cases closed at least 90 days ago, so late-discovered fraud is labelled.
- A few hundred legitimate cases measure false alarms well; confirmed fraud is rarer, and every one counts.

## Privacy

- Agree what reviewers may see. By default reviewers see initials instead of names, no street addresses, GPS rounded
  to about 1 km, and card numbers cut to the last four digits; investigators and admins see everything.
- Agree retention: `evcheck retention --days N` deletes evidence files of closed cases and keeps verdicts, decisions
  and the audit log.
