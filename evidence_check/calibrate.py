"""Calibration for a deployment: threshold sweep, threshold for a review capacity, ROI, and a fairness check.

Inputs are labelled verdicts: a benchmark results.json, or cases people have closed in the service
(closed:fraud = positive, closed:legitimate = negative).

Benchmarks contain far more fraud than real life, so precision is never read off the sample. It is recomputed
at the deployment's own fraud rate from recall (TPR) and false-alarm rate (FPR):
    precision = TPR * p / (TPR * p + FPR * (1 - p))
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .bench.run import wilson
from .models import Finding
from .policy import load_policy, score


@dataclass
class Labelled:
    domain: str
    fraud: bool
    findings: list[dict]
    segment: dict


@dataclass
class Economics:
    cases_per_day: float = 1000
    review_capacity_per_day: float = 60
    fraud_rate: float = 0.03  # share of cases that are fraudulent in production
    avg_fraud_loss: float = 2500.0  # paid out per undetected fraudulent case
    review_cost: float = 25.0  # cost of one human review


def from_benchmark(path: str | Path) -> list[Labelled]:
    rows = json.loads(Path(path).read_text())["rows"]
    return [Labelled(r["domain"], r["fraud"], r["findings"], r.get("segment", {})) for r in rows]


def from_store(store) -> list[Labelled]:
    out = []
    for r in store.cases():
        if r["status"] in ("closed:fraud", "closed:legitimate") and r["verdict"]:
            out.append(Labelled(r["domain"], r["status"] == "closed:fraud", r["verdict"]["findings"], r["segment"]))
    return out


def scores(data: list[Labelled]) -> list[tuple[float, bool, Labelled]]:
    pols = {d: load_policy(d) for d in {x.domain for x in data}}
    return [(score([Finding.model_validate(f) for f in x.findings], pols[x.domain])[0], x.fraud, x) for x in data]


def sweep(scored: list[tuple[float, bool, Labelled]], econ: Economics, steps: int = 19) -> list[dict]:
    pos = sum(1 for _, y, _ in scored if y)
    neg = len(scored) - pos
    rows = []
    for i in range(1, steps + 1):
        t = round(i / (steps + 1), 3)
        tp = sum(1 for s, y, _ in scored if y and s >= t)
        fp = sum(1 for s, y, _ in scored if not y and s >= t)
        tpr, fpr = tp / max(1, pos), fp / max(1, neg)
        p = econ.fraud_rate
        flag_rate = tpr * p + fpr * (1 - p)  # share of production cases this threshold would flag
        precision = tpr * p / flag_rate if flag_rate else None
        flagged_day = flag_rate * econ.cases_per_day
        caught_day = tpr * p * econ.cases_per_day
        saved = caught_day * econ.avg_fraud_loss
        cost = flagged_day * econ.review_cost
        rows.append({"threshold": t, "recall": round(tpr, 3), "false_alarm_rate": round(fpr, 4),
                     "recall_ci": wilson(tp, pos), "false_alarm_ci": wilson(fp, neg),
                     "precision_at_prevalence": round(precision, 3) if precision is not None else None,
                     "flagged_per_day": round(flagged_day, 1), "fraud_caught_per_day": round(caught_day, 2),
                     "net_value_per_day": round(saved - cost, 0), "within_capacity": flagged_day <= econ.review_capacity_per_day})
    return rows


def pick_threshold(rows: list[dict]) -> dict | None:
    """Best net value among thresholds the review team can keep up with."""
    ok = [r for r in rows if r["within_capacity"]]
    return max(ok, key=lambda r: (r["net_value_per_day"], r["threshold"])) if ok else None


def per_check(data: list[Labelled]) -> dict[str, dict]:
    pos = sum(x.fraud for x in data)
    neg = len(data) - pos
    stats: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for x in data:
        for c in {f["check"] for f in x.findings if f["severity"] != "info"}:
            stats[c][int(x.fraud)] += 1
    return {c: {"fires_on_fraud": v[1], "fires_on_legit": v[0], "recall": round(v[1] / max(1, pos), 3),
                "false_alarm_rate": round(v[0] / max(1, neg), 4), "precision_in_sample": round(v[1] / max(1, sum(v)), 3)}
            for c, v in sorted(stats.items())}


def fairness(scored: list[tuple[float, bool, Labelled]], threshold: float, key: str) -> list[dict]:
    """False-alarm rate per segment among legitimate cases: honest customers should not be flagged more in one group."""
    groups: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for s, y, x in scored:
        if not y:
            g = str(x.segment.get(key, "unknown"))
            groups[g][0] += 1
            groups[g][1] += s >= threshold
    out = [{"segment": g, "legit_cases": n, "false_alarms": k, "rate": round(k / n, 4) if n else None, "ci": wilson(k, n)}
           for g, (n, k) in sorted(groups.items())]
    return out


def report(data: list[Labelled], econ: Economics, segment_keys: tuple[str, ...] = ("region", "channel")) -> tuple[str, dict]:
    sc = scores(data)
    rows = sweep(sc, econ)
    best = pick_threshold(rows)
    pc = per_check(data)
    pos = sum(x.fraud for x in data)
    out = ["# Calibration", "",
           f"{len(data)} labelled cases ({pos} fraud, {len(data) - pos} legitimate). Assumptions: {econ.cases_per_day:g} cases/day, "
           f"review capacity {econ.review_capacity_per_day:g}/day, fraud rate {econ.fraud_rate:.1%}, average fraud loss "
           f"${econ.avg_fraud_loss:,.0f}, review cost ${econ.review_cost:,.0f}.", ""]
    if best:
        out += [f"**Recommended threshold: {best['threshold']}**: flags about {best['flagged_per_day']:g} cases a day (capacity "
                f"{econ.review_capacity_per_day:g}), catches {best['recall']:.0%} of fraud, about {best['fraud_caught_per_day']:g} cases "
                f"a day; {best['precision_at_prevalence']:.0%} of flagged cases would be fraud at a {econ.fraud_rate:.1%} fraud rate; "
                f"net value about ${best['net_value_per_day']:,.0f} a day.", ""]
    else:
        out += ["**No threshold fits the review capacity**: every threshold flags more cases than the team can review. "
                "Add reviewers or route only `investigate` cases.", ""]
    out += ["## Threshold sweep", "",
            "| Threshold | Recall | False alarms | Precision at your fraud rate | Flagged/day | Fraud caught/day | Net value/day | Fits capacity |",
            "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        prec = f"{r['precision_at_prevalence']:.0%}" if r["precision_at_prevalence"] is not None else "—"
        out.append(f"| {r['threshold']} | {r['recall']:.0%} | {r['false_alarm_rate']:.1%} | {prec} | {r['flagged_per_day']:g} | "
                   f"{r['fraud_caught_per_day']:g} | ${r['net_value_per_day']:,.0f} | {'yes' if r['within_capacity'] else 'no'} |")
    out += ["", "## Per check", "", "| Check | Fires on fraud | Fires on legitimate | Recall | False-alarm rate |", "|---|---|---|---|---|"]
    out += [f"| {c} | {v['fires_on_fraud']} | {v['fires_on_legit']} | {v['recall']:.0%} | {v['false_alarm_rate']:.1%} |" for c, v in pc.items()]
    fair = {}
    t = best["threshold"] if best else 0.35
    for key in segment_keys:
        fr = fairness(sc, t, key)
        if len(fr) > 1:
            fair[key] = fr
            out += ["", f"## Fairness: false alarms on legitimate cases by {key} (threshold {t})", "",
                    "| Segment | Legitimate cases | False alarms | Rate (95% CI) |", "|---|---|---|---|"]
            out += [f"| {g['segment']} | {g['legit_cases']} | {g['false_alarms']} | {g['rate']:.1%} ({g['ci'][0]:.1%}–{g['ci'][1]:.1%}) |"
                    for g in fr]
    return "\n".join(out) + "\n", {"sweep": rows, "recommended": best, "per_check": pc, "fairness": fair}
