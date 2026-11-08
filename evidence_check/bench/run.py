"""Run the benchmark: detection per manipulation, false alarms on clean cases, and a layer ablation.

Each case is checked once with every check on. Because checks are independent, the verdict any subset of layers
would have produced is recomputed exactly by re-scoring that subset of findings, so the ablation costs nothing extra
(important when real models take seconds per file).

    python -m evidence_check.bench.run --out bench-out --models oracle
    python -m evidence_check.bench.run --out bench-out --models real --real-photos --voice tts
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections import defaultdict
from pathlib import Path

from ..engine import run_case
from ..history import HistoryIndex
from ..models import Finding, Level
from ..policy import load_policy, score
from .cases import build_suite

PIXEL = {"error_level", "copy_move", "text_consistency", "ai_image_model"}

# Each layer adds to the ones before it, in the order a buyer would compare them against.
LAYERS = [
    ("Pixel forensics only (typical single-image detector)", lambda f: f.check in PIXEL),
    ("+ file metadata and provenance", lambda f: f.layer == "item"),
    ("+ cross-evidence consistency (receipts, card, dates, GPS, photo content, statement)", lambda f: f.layer in ("item", "case")),
    ("+ history (reuse across cases and customers)", lambda f: True),
]

# The check that should explain each manipulation (for attribution: flagged for the right reason).
EXPECTED = {
    "edited_total": {"receipt_math", "card_match"},
    "generator_receipt": {"generator_traces", "content_credentials"},
    "flat_receipt_no_card": {"card_match", "metadata"},
    "wrong_tax": {"tax_rate"},
    "no_card_txn": {"card_match"},
    "photo_before_policy": {"dates"},
    "photo_before_incident": {"dates"},
    "gps_mismatch": {"gps_location"},
    "amount_inflated": {"amount_match"},
    "copy_move_photo": {"copy_move"},
    "edited_photo_software": {"metadata"},
    "reused_photo": {"duplicate_evidence"},
    "reused_receipt": {"duplicate_evidence"},
    "wrong_loss_type": {"photo_vs_claim", "statement"},
    "statement_contradiction": {"statement"},
    "receipt_date_mismatch": {"dates"},
}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% confidence interval for a rate measured on a small sample."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def make_models(kind: str):
    if kind == "oracle":
        from ..extract.oracle import OracleModels

        return OracleModels()
    if kind == "real":
        from ..extract.models import ModelRegistry

        return ModelRegistry()
    if kind == "none":
        return None
    raise ValueError(kind)


def run_suite(case_dirs: list[Path], models, work_dir: Path) -> list[dict]:
    hist = HistoryIndex()  # fresh: only this suite's earlier cases count as history
    rows = []
    for d in case_dirs:
        truth = json.loads((d / "truth.json").read_text())
        t0 = time.perf_counter()
        case, _, v = run_case(d, models=models, history=hist, work_dir=work_dir)
        rows.append({"case": case.case_id, "domain": case.domain, "fraud": truth["fraud"],
                     "manipulation": (truth["manipulations"] or [None])[0], "segment": case.segment, "seconds": round(time.perf_counter() - t0, 2),
                     "findings": [f.model_dump(mode="json") for f in v.findings], "skipped": v.skipped})
    return rows


def evaluate(rows: list[dict]) -> dict:
    policies = {dom: load_policy(dom) for dom in {r["domain"] for r in rows}}
    layers = []
    for name, keep in LAYERS:
        clean_alarm, fraud_hit, per = 0, 0, []
        for r in rows:
            fs = [f for f in map(Finding.model_validate, r["findings"]) if keep(f)]
            s, level = score(fs, policies[r["domain"]])
            flagged = level is not Level.CLEAR
            if r["fraud"]:
                fraud_hit += flagged
                right = bool(EXPECTED.get(r["manipulation"], set()) & {f.check for f in fs})
                per.append({"domain": r["domain"], "manipulation": r["manipulation"], "flagged": flagged,
                            "right_reason": flagged and right, "level": level.value, "score": s})
            else:
                clean_alarm += flagged
        n_clean = sum(not r["fraud"] for r in rows)
        n_fraud = len(rows) - n_clean
        layers.append({"layer": name, "recall": fraud_hit / max(1, n_fraud), "recall_ci": wilson(fraud_hit, n_fraud),
                       "false_alarm_rate": clean_alarm / max(1, n_clean), "false_alarm_ci": wilson(clean_alarm, n_clean),
                       "flagged": fraud_hit, "n_fraud": n_fraud, "false_alarms": clean_alarm, "n_clean": n_clean,
                       "right_reason": sum(p["right_reason"] for p in per), "cases": per})
    # which checks fire, on clean vs manipulated cases
    fires = defaultdict(lambda: [0, 0])
    for r in rows:
        for c in {f["check"] for f in r["findings"] if f["severity"] != "info"}:
            fires[c][int(r["fraud"])] += 1
    secs = sorted(r["seconds"] for r in rows)
    return {"layers": layers, "checks": {c: {"clean": v[0], "manipulated": v[1]} for c, v in sorted(fires.items())},
            "latency": {"median_s": secs[len(secs) // 2], "p95_s": secs[min(len(secs) - 1, int(0.95 * len(secs)))]},
            "cases": len(rows)}


def pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def report(res: dict, title: str) -> str:
    full = res["layers"][-1]
    out = [f"# {title}", "", f"{res['cases']} cases ({full['n_clean']} clean, {full['n_fraud']} manipulated). "
           f"Median {res['latency']['median_s']} s per case, p95 {res['latency']['p95_s']} s.", "",
           "## Layer ablation", "", "| Layers | Manipulations caught | False alarms on clean cases |", "|---|---|---|"]
    for L in res["layers"]:
        lo, hi = L["recall_ci"]
        flo, fhi = L["false_alarm_ci"]
        out.append(f"| {L['layer']} | {L['flagged']}/{L['n_fraud']} = **{pct(L['recall'])}** (95% CI {pct(lo)}–{pct(hi)}) "
                   f"| {L['false_alarms']}/{L['n_clean']} = {pct(L['false_alarm_rate'])} (95% CI {pct(flo)}–{pct(fhi)}) |")
    out += ["", "## Per manipulation (all layers)", "", "| Domain | Manipulation | Caught | For the right reason | Mean score |",
            "|---|---|---|---|---|"]
    groups = defaultdict(list)
    for p in full["cases"]:
        groups[(p["domain"], p["manipulation"])].append(p)
    for (dom, man), ps in groups.items():
        n, hit, right = len(ps), sum(p["flagged"] for p in ps), sum(p["right_reason"] for p in ps)
        mark = "" if hit == n else " **(missed some)**"
        out.append(f"| {dom.replace('_', ' ')} | {man.replace('_', ' ')} | {hit}/{n}{mark} | {right}/{n} | "
                   f"{sum(p['score'] for p in ps) / n:.2f} |")
    out += ["", "## Which checks fired", "", "| Check | Clean cases | Manipulated cases |", "|---|---|---|"]
    out += [f"| {c} | {v['clean']} | {v['manipulated']} |" for c, v in res["checks"].items()]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="bench-out")
    ap.add_argument("--models", default="oracle", choices=["oracle", "real", "none"])
    ap.add_argument("--clean", type=int, default=12, help="clean cases per domain")
    ap.add_argument("--seeds", default="11", help="comma-separated; each seed builds and runs its own suite")
    ap.add_argument("--real-photos", action="store_true", help="use real car-damage photos for claims")
    ap.add_argument("--voice", default="silent", choices=["silent", "tts"])
    a = ap.parse_args(argv)
    out = Path(a.out)
    photos = None
    if a.real_photos:
        from .photos import fetch_car_photos

        photos = fetch_car_photos()
    models, rows = make_models(a.models), []
    for seed in [int(x) for x in a.seeds.split(",")]:
        dirs = build_suite(out / "cases" / f"seed-{seed}", a.clean, seed, photos, voice=a.voice)
        rows += run_suite(dirs, models, out / "work")
    res = evaluate(rows)
    res["config"] = vars(a)
    (out / "results.json").write_text(json.dumps({"summary": res, "rows": rows}, indent=1, default=str))
    md = report(res, f"Benchmark: {a.models} perception, seeds {a.seeds}")
    (out / "report.md").write_text(md)
    print(md)
    return res


if __name__ == "__main__":
    main()
