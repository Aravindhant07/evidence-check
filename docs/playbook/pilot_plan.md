# Shadow pilot (weeks 3–6)

The system checks every new case in parallel with the current process. Nobody acts on its verdicts during the pilot.
At the end, its verdicts are compared with what the team decided and with outcomes known by then.

## Setup

1. Deploy (see [runbook.md](runbook.md)) inside the client's network. Connect intake: webhook from their system,
   or the folder watcher on an export directory.
2. Load history: submit the last 6–12 months of cases (`evcheck submit` in a loop, or the watcher). This lets reuse
   checks see earlier photos and receipts from the first day.
3. Apply the policy and threshold from calibration.
4. Give two or three reviewers access to the queue with the `reviewer` role. Ask them to confirm or dismiss each reason
   on flagged cases, without changing how cases are handled.

## Weekly

- Flag rate per check and drift alerts (dashboard).
- Reviewer agreement per check: a check with low agreement is tuned or switched off before go-live.
- Median time to decide a reason: should be under a minute.
- Cases the current process flagged that the system did not, and why.

## Readiness report (end of week 6)

Grade the pilot with [shadow-eval](https://github.com/Aravindhant07/shadow-eval): it compares the system's verdicts with
the team's decisions and outcomes and reports agreement, critical misses, and per-segment behaviour.

## Success criteria (agree these before the pilot starts)

| Metric | Typical target |
|---|---|
| Recall on confirmed fraud in the pilot period | ≥ current process, with fewer cases reviewed |
| Flagged cases per day | within review capacity |
| Reviewer agreement on flags | ≥ 60% of flagged cases have at least one confirmed reason |
| False-alarm rate gap between segments | no segment more than 1.5× the overall rate (with confidence intervals) |
| Time to decide a flagged case | median under 3 minutes |
| Availability during business hours | ≥ 99.5% |

## Go-live decision

Go live with the checks that met their targets; leave the others in shadow. Agreeing this in advance stops
the decision from becoming a debate about a single striking case.
