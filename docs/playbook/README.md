# Deployment playbook

How to take evidence-check from this repository to a working deployment at an insurer or a company's expense team.
Written for the engineer doing the deployment and for the client's project lead.

| Step | Weeks | Output | Document |
|---|---|---|---|
| 1. Discovery | 1 | The client's fraud patterns, volumes, systems, constraints; a go/no-go | [discovery.md](discovery.md) |
| 2. Data | 1–2 | A labelled sample of past cases in the case-bundle format; a connector plan | [data_checklist.md](data_checklist.md) |
| 3. Calibrate | 2 | Domain policy tuned on their cases; threshold for their review capacity; ROI estimate | `evcheck calibrate` |
| 4. Shadow pilot | 3–6 | Verdicts recorded next to the current process, no one acts on them; readiness report | [pilot_plan.md](pilot_plan.md) |
| 5. Go live | 7+ | Reviewers work the queue; monitoring and drift alerts on | [runbook.md](runbook.md) |

## Principles that hold throughout

1. **Recommendations, never decisions.** There is no automatic denial path in the product. Every flagged case goes to a
   person, every reason can be confirmed or dismissed, and the decision record is exportable for appeals.
2. **Measure on their data before promising anything.** The benchmark numbers in this repository are on generated cases.
   The first real number is the calibration on the client's labelled past cases; the second is the shadow pilot.
3. **Explain every flag.** A reviewer must be able to see in under a minute why a case was flagged and where in the file.
   If they cannot, the check is not ready for them.
4. **Watch false alarms per segment.** Honest customers should not be flagged more often in one region, channel or claim
   size. The calibration and the pilot both report this.
5. **Small, typed changes.** Policy changes (thresholds, weights, rules) go through the evaluation gate and an approval,
   and can be rolled back in one step.
