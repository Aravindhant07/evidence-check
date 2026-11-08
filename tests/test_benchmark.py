"""The benchmark itself, as a regression gate: every manipulation caught for the right reason, no false alarms."""

from evidence_check.bench.cases import build_suite
from evidence_check.bench.run import evaluate, report, run_suite, wilson
from evidence_check.extract.oracle import OracleModels


def test_oracle_benchmark_gate(tmp_path):
    rows = run_suite(build_suite(tmp_path / "cases", clean_per_domain=4, seed=21), OracleModels(), tmp_path / "work")
    res = evaluate(rows)
    full = res["layers"][-1]
    assert full["false_alarms"] == 0
    missed = [p["manipulation"] for p in full["cases"] if not p["right_reason"]]
    assert not missed, missed
    recalls = [L["recall"] for L in res["layers"]]
    assert recalls == sorted(recalls) and recalls[0] < 0.2  # each layer adds; pixels alone catch little
    assert "Layer ablation" in report(res, "t")


def test_wilson_interval():
    lo, hi = wilson(0, 240)
    assert lo == 0 and 0.01 < hi < 0.02
    lo, hi = wilson(10, 10)
    assert hi == 1.0 and 0.6 < lo < 0.75
