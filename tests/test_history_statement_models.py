"""History index, statement check and the model-free parts of the model extractors."""

import math
from datetime import date

import pytest

from evidence_check.bench.cases import Builder
from evidence_check.checks.base import Context
from evidence_check.checks.history_checks import DeviceRingCheck
from evidence_check.checks.statement import StatementCheck, amounts, dates, loss_types
from evidence_check.engine import run_case
from evidence_check.extract.models import ModelRegistry, merge_rows, text_angle
from evidence_check.extract.oracle import OracleModels
from evidence_check.history import HistoryIndex, text_similarity
from evidence_check.models import Case, Evidence, Extraction, Level, Severity
from evidence_check.policy import load_policy


def make_ev(id="e", kind="image", role="damage_photo", **kw):
    return Evidence(id=id, case_id="", file="", name=f"{id}.bin", role=role, kind=kind, mime="", sha256=id, size=0, **kw)


def make_case(case_id, **kw):
    return Case(case_id=case_id, domain="insurance_claims", **kw)


# -- history -----------------------------------------------------------------

@pytest.fixture()
def history_run(tmp_path):
    b = Builder(tmp_path / "cases", seed=5)
    hist = HistoryIndex(tmp_path / "history.jsonl")

    def run(d):
        return run_case(d, models=OracleModels(), history=hist, work_dir=tmp_path / "work")[2]

    return b, hist, run


def test_clean_cases_do_not_match_each_other(history_run):
    b, hist, run = history_run
    for d in [b.claim() for _ in range(4)] + [b.expense() for _ in range(4)]:
        v = run(d)
        assert not [f for f in v.findings if f.layer == "history"], [(f.check, f.title) for f in v.findings]
    assert hist.stats()["cases"] == 8


def test_reused_photo_from_another_customer_is_critical(history_run):
    b, _, run = history_run
    for _ in range(3):
        run(b.claim())
    v = run(b.claim("reused_photo"))  # a cropped, resized copy of an earlier customer's photo
    dup = [f for f in v.findings if f.check == "duplicate_evidence"]
    assert dup and dup[0].severity is Severity.CRITICAL and "different customer" in dup[0].title
    assert v.level is not Level.CLEAR


def test_reused_receipt_is_flagged(history_run):
    b, _, run = history_run
    for _ in range(3):
        run(b.expense())
    v = run(b.expense("reused_receipt"))
    assert any(f.check == "duplicate_evidence" and "receipt" in f.title for f in v.findings)


def test_history_persists_across_restarts(history_run, tmp_path):
    b, hist, run = history_run
    run(b.claim())
    again = HistoryIndex(tmp_path / "history.jsonl")
    assert len(again.entries) == len(hist.entries) > 0


def test_text_similarity():
    assert text_similarity("A B C", "A B C") == 1.0
    assert text_similarity("A B C", "D E F") == 0.0
    assert text_similarity("", "A") == 0.0


def test_device_ring_needs_several_other_customers(tmp_path):
    hist = HistoryIndex()
    exif = {"Make": "Acme", "Model": "P1", "BodySerialNumber": "SN1"}
    for i in range(3):
        hist.add_case(make_case(f"C{i}", claimant_id=f"P{i}"), [make_ev("e", kind="audio", metadata={"exif": exif})])
    ev = make_ev("x", kind="audio", metadata={"exif": exif})
    ctx = Context(case=make_case("NEW", claimant_id="P9"), evidence=[ev], policy=load_policy("insurance_claims"),
                  work_dir=tmp_path, history=hist)
    out = DeviceRingCheck().run(ctx)
    assert len(out) == 1 and out[0].severity is Severity.HIGH and "3 other customers" in out[0].title
    ctx.case = make_case("NEW", claimant_id="P0")  # same person as one of them: only 2 others
    assert DeviceRingCheck().run(ctx)[0].severity is Severity.MEDIUM


# -- statement ---------------------------------------------------------------

def test_amounts_in_digits_and_words():
    assert 1200.0 in amounts("it cost about $1,200 to fix")
    assert 450.0 in amounts("they quoted four hundred and fifty dollars")
    assert 2500.0 in amounts("roughly two thousand five hundred dollars")
    assert 80.0 in amounts("maybe 80 bucks")


def test_dates_and_loss_types():
    assert dates("it happened on March the third", 2026) == [date(2026, 3, 3)]
    assert dates("on september 14th", 2026) == [date(2026, 9, 14)]
    assert dates("on September 20th, 8th another car hit my car", 2026) == [date(2026, 9, 28)]  # Whisper's split
    assert dates("on may 30th, 1st", 2026) == [date(2026, 5, 31)]
    assert loss_types("a pipe burst and the floor was soaked") == {"water_damage"}
    assert "theft" in loss_types("someone broke in and stole the laptop")


def statement_ctx(text, **case):
    ev = make_ev("s", kind="audio", role="statement", extraction=Extraction(transcript=text))
    return Context(case=make_case("C", **case), evidence=[ev], policy=load_policy("insurance_claims"), work_dir=None)


def test_statement_consistent_with_claim_is_silent():
    ctx = statement_ctx("A pipe burst on March 3rd and repairs were about 1,850 dollars.",
                        claimed_amount=1800.0, loss_type="water_damage", incident_date=date(2026, 3, 3))
    assert StatementCheck().run(ctx) == []


def test_statement_contradictions_are_flagged():
    ctx = statement_ctx("Someone broke in on March 20th and stole my TV, it was about 600 dollars.",
                        claimed_amount=4200.0, loss_type="water_damage", incident_date=date(2026, 3, 3))
    titles = " | ".join(f.title for f in StatementCheck().run(ctx))
    assert "amount" in titles and "theft" in titles and "date" in titles


# -- model extractor helpers (no model download) ----------------------------

def test_merge_rows_joins_label_and_amount_on_a_tilted_receipt():
    angle = math.radians(3)
    # the amount sits 300px to the right, so a 3 degree tilt drops it ~16px: more than half a line height
    regions = [("SUBTOTAL", (10, 100, 120, 120)), ("21.45", (310, 116, 370, 136)), ("TAX", (10, 130, 60, 150))]
    straight = [l.text for l in merge_rows(regions, 0.0)]
    tilted = [l.text for l in merge_rows(regions, angle)]
    assert "SUBTOTAL  21.45" not in straight
    assert "SUBTOTAL  21.45" in tilted and "TAX" in tilted


def test_text_angle_uses_wide_regions_only():
    quads = [[0, 0, 200, 10, 200, 30, 0, 20], [0, 0, 300, 16, 300, 36, 0, 20], [0, 0, 10, 9, 10, 20, 0, 11]]
    assert 2.5 < math.degrees(text_angle(quads)) < 3.2
    assert text_angle([]) == 0.0


class _Logits:
    def __init__(self, v):
        import torch
        self.logits = torch.tensor([v])


class _Model:
    def __init__(self, labels, logits):
        self.config = type("C", (), {"id2label": labels})()
        self._l = logits

    def __call__(self, **_):
        return _Logits(self._l)


@pytest.mark.parametrize("labels,logits,expected", [
    ({0: "dalle", 1: "real", 2: "sd"}, [0.0, 0.0, 0.0], 2 / 3),  # P(generated) = 1 - P(real)
    ({0: "artificial", 1: "human"}, [0.0, 0.0], 0.5),
    ({0: "fake", 1: "other"}, [0.0, 0.0], 0.5),
])
def test_ai_score_label_mapping(labels, logits, expected):
    pytest.importorskip("torch")
    from PIL import Image

    reg = ModelRegistry.__new__(ModelRegistry)
    reg._m = {"ai_image": (_Model(labels, logits), lambda images, return_tensors: {})}
    reg._lock = __import__("threading").Lock()
    assert reg.ai_score(Image.new("RGB", (8, 8))) == pytest.approx(expected, abs=1e-6)


def test_models_are_pinned_to_full_commits():
    reg = ModelRegistry()
    for job, c in reg.cfg.items():
        assert len(c["revision"]) == 40, job


# -- unreadable receipts and the OCR second look --------------------------------

def test_unreadable_receipt_goes_to_review(tmp_path):
    import json

    b = Builder(tmp_path / "c", seed=9)
    d = b.expense()
    truth = json.loads((d / "truth.json").read_text())
    for name in truth["ocr"]:
        truth["ocr"][name] = [l for l in truth["ocr"][name] if "TOTAL" not in l[0]]  # OCR lost the totals
    (d / "truth.json").write_text(json.dumps(truth))
    v = run_case(d, models=OracleModels(), work_dir=tmp_path / "w")[2]
    assert any(f.check == "unreadable" for f in v.findings) and v.level is not Level.CLEAR
    v = run_case(d, models=None, work_dir=tmp_path / "w")[2]  # no OCR at all: nothing to report as unreadable
    assert not any(f.check == "unreadable" for f in v.findings)


def test_read_receipt_second_look():
    from PIL import Image

    from evidence_check.models import TextLine

    calls = []

    class Stub(ModelRegistry):
        def __init__(self):
            pass

        def ocr(self, img, offset=(0, 0), **gen):
            calls.append(gen)
            if gen:  # the retry escapes the repetition loop
                return [TextLine(text="SUBTOTAL 17.95", box=(0, 0, 9, 9)), TextLine(text="TOTAL 19.81", box=(0, 10, 9, 19))]
            return [TextLine(text="- - - - - - - -", box=(0, 0, 9, 9))]

    lines, how = Stub().read_receipt(Image.new("RGB", (100, 200)))
    assert how == "retried with repetition penalty" and calls[1] == {"repetition_penalty": 1.2}
    assert any("19.81" in l.text for l in lines)


def test_model_swap_gate():
    from evidence_check import evals

    base = evals.load_baseline()
    assert base and evals.compare(base, base) == (True, [])
    worse = json_copy = __import__("json").loads(__import__("json").dumps(base))
    worse["ocr"]["total_accuracy"] -= 0.2           # a new OCR model that misreads totals
    worse["ai_image"]["false_positive_rate"] += 0.1  # and a noisier detector
    ok, problems = evals.compare(worse, base)
    assert not ok and len(problems) == 2
    assert evals.wer("the repairs came to about", "the repair came to about") == 0.2
