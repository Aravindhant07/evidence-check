"""Core pipeline and deterministic checks, on generated benchmark cases with oracle OCR."""

import json
from pathlib import Path

import pytest
from PIL import Image

from evidence_check.bench.cases import Builder
from evidence_check.engine import run_case
from evidence_check.extract.oracle import OracleModels
from evidence_check.extract.receipt import parse_date, parse_lines
from evidence_check.ingest import RejectedFile, load_evidence, sniff
from evidence_check.models import Finding, Level, Severity, TextLine
from evidence_check.policy import load_policy, score


@pytest.fixture(scope="module")
def suite(tmp_path_factory):
    b = Builder(tmp_path_factory.mktemp("cases"), seed=11)
    clean = [b.claim() for _ in range(4)] + [b.expense() for _ in range(4)]
    return b, clean


def run(d, tmp_path):
    return run_case(d, models=OracleModels(), work_dir=tmp_path)


def checks_of(v):
    return {f.check for f in v.findings}


def test_clean_cases_are_clear(suite, tmp_path):
    _, clean = suite
    for d in clean:
        _, _, v = run(d, tmp_path)
        assert v.level is Level.CLEAR, (d.name, [(f.check, f.title) for f in v.findings])


@pytest.mark.parametrize("domain,manipulation,expected", [
    ("claim", "edited_total", {"receipt_math", "card_match"}),
    ("claim", "generator_receipt", {"generator_traces", "card_match"}),
    ("claim", "wrong_tax", {"tax_rate"}),
    ("claim", "no_card_txn", {"card_match"}),
    ("claim", "photo_before_policy", {"dates"}),
    ("claim", "photo_before_incident", {"dates"}),
    ("claim", "gps_mismatch", {"gps_location"}),
    ("claim", "amount_inflated", {"amount_match"}),
    ("claim", "copy_move_photo", {"copy_move"}),
    ("claim", "edited_photo_software", {"metadata"}),
    ("expense", "edited_total", {"receipt_math", "card_match"}),
    ("expense", "flat_receipt_no_card", {"card_match", "metadata"}),
    ("expense", "receipt_date_mismatch", {"dates"}),
])
def test_manipulations_are_flagged(suite, tmp_path, domain, manipulation, expected):
    b, _ = suite
    d = b.claim(manipulation) if domain == "claim" else b.expense(manipulation)
    _, _, v = run(d, tmp_path)
    assert v.level is not Level.CLEAR, manipulation
    assert expected <= checks_of(v), (manipulation, checks_of(v))
    for f in v.findings:  # every finding is explainable
        assert f.title and (f.pointer.evidence_id or f.pointer.case_field or f.pointer.text)


def test_receipt_parser_keeps_sources():
    lines = [TextLine(text=t, box=(0, i * 10, 100, i * 10 + 9)) for i, t in enumerate([
        "HILTON GARDEN INN", "LOS ANGELES, CA", "09/10/2026", "ROOM CHARGE 1 NIGHT   219.00",
        "OCCUPANCY TAX          23.40", "SUBTOTAL              242.40", "TAX                    23.03", "TOTAL                 265.43"])]
    r = parse_lines(lines)
    assert r.merchant.value == "HILTON GARDEN INN" and r.date.value == "2026-09-10"
    assert [i.amount for i in r.items] == [219.0, 23.4]  # "OCCUPANCY TAX" is an item, not the tax line
    assert (r.subtotal.value, r.tax.value, r.total.value) == (242.4, 23.03, 265.43)
    assert r.total.box == (0, 70, 100, 79)
    assert parse_date("Sep 14, 2026").isoformat() == "2026-09-14"


def test_scoring_noisy_or_and_levels():
    pol = load_policy("insurance_claims")
    f = lambda sev, c=1.0: Finding(check="x", layer="case", severity=sev, confidence=c, title="t")
    assert score([], pol) == (0.0, Level.CLEAR)
    s1, _ = score([f(Severity.HIGH)], pol)
    s2, lvl = score([f(Severity.HIGH), f(Severity.HIGH)], pol)
    assert s2 > s1 and lvl is Level.INVESTIGATE
    assert score([f(Severity.LOW)] * 2, pol)[1] is Level.CLEAR  # weak signals alone don't escalate


def test_ingest_rejects_unknown_and_oversized(tmp_path):
    bad = tmp_path / "x.jpg"
    bad.write_bytes(b"MZ\x90\x00 not an image")
    with pytest.raises(RejectedFile):
        load_evidence(bad, "C1", "receipt")  # sniffed by content, not by extension
    assert sniff(b"%PDF-1.7\n") == ("pdf", "application/pdf")
    big = tmp_path / "big.png"
    Image.new("RGB", (8000, 6000)).save(big)  # 48 MP > 40 MP limit
    with pytest.raises(RejectedFile):
        load_evidence(big, "C1", "damage_photo")


def test_path_traversal_is_rejected(tmp_path):
    d = tmp_path / "case"
    d.mkdir()
    (tmp_path / "secret.jpg").write_bytes(b"\xff\xd8\xff")
    (d / "case.json").write_text(json.dumps({"case_id": "C1", "domain": "expense_reports", "evidence": [{"file": "../secret.jpg"}]}))
    _, _, v = run_case(d, work_dir=tmp_path)
    assert any("escapes" in f.detail for f in v.findings)
