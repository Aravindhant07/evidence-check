"""Production layer: masking, webhooks, inbox watcher, retention, drift alerts, calibration."""

import json
import shutil
import threading
import time
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from evidence_check import calibrate as cal
from evidence_check.bench.cases import Builder
from evidence_check.extract.oracle import OracleModels
from evidence_check.integrations import SIGNATURE_HEADER, make_webhook, sign, verify, watch
from evidence_check.privacy import mask_verdict, redact_cards
from evidence_check.service import Service


@pytest.fixture(scope="module")
def src(tmp_path_factory):
    d = tmp_path_factory.mktemp("src")
    b = Builder(d, seed=4)
    return d, [b.claim(), b.expense(), b.claim("gps_mismatch")]


def test_masking_by_role():
    v = {"case": {"claimant_name": "Maria Lopez", "location": {"lat": 1, "lon": 2, "address": "12 Main St"}},
         "evidence": [{"gps": {"lat": 40.712776, "lon": -74.005974}, "lines": [{"text": "VISA 4111 1111 1111 4417"}], "transcript": None}]}
    r = mask_verdict(v, "reviewer")
    assert r["case"]["claimant_name"] == "M. L." and r["case"]["location"]["address"] is None
    assert r["evidence"][0]["gps"] == {"lat": 40.71, "lon": -74.01}
    assert "4111" not in r["evidence"][0]["lines"][0]["text"] and "4417" in r["evidence"][0]["lines"][0]["text"]
    full = mask_verdict(v, "investigator")
    assert full["case"]["claimant_name"] == "Maria Lopez" and "4111" not in full["evidence"][0]["lines"][0]["text"]
    assert v["case"]["claimant_name"] == "Maria Lopez"  # the stored verdict is never modified
    assert redact_cards("AUTH 083972") == "AUTH 083972"


def test_webhook_is_signed_and_delivered(src, tmp_path):
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["content-length"]))
            got.append((body, self.headers["X-Evcheck-Timestamp"], self.headers[SIGNATURE_HEADER]))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    d, dirs = src
    svc = Service(tmp_path / "data", OracleModels(d), webhook=make_webhook(f"http://127.0.0.1:{srv.server_port}/hook", "s3cret"))
    svc.submit_dir(dirs[2], run_now=True)
    srv.shutdown()
    body, ts, sig = got[0]
    assert verify("s3cret", ts, body, sig) and not verify("wrong", ts, body, sig)
    assert not verify("s3cret", str(time.time() - 3600), body, sign("s3cret", str(time.time() - 3600), body))  # replay
    assert json.loads(body)["level"] in ("review", "investigate")


def test_failed_webhook_never_loses_the_verdict(src, tmp_path):
    d, dirs = src
    svc = Service(tmp_path / "data", OracleModels(d), webhook=lambda cid, v: (_ for _ in ()).throw(RuntimeError("down")))
    cid = svc.submit_dir(dirs[0], run_now=True)
    assert svc.store.case(cid)["status"] == "checked"
    assert any(e["action"] == "webhook_failed" for e in svc.store.audit_log())


def test_watcher_accepts_and_rejects(src, tmp_path):
    d, dirs = src
    inbox = tmp_path / "inbox"
    shutil.copytree(dirs[0], inbox / "good")
    (inbox / "bad").mkdir(parents=True)
    (inbox / "bad" / "case.json").write_text("{not json")
    svc = Service(tmp_path / "data")
    assert watch(inbox, svc, once=True) == [dirs[0].name]
    assert len(list((inbox / ".processed").iterdir())) == 1
    rejected = list((inbox / ".rejected").iterdir())
    assert len(rejected) == 1 and "case.json" in (rejected[0] / "reason.txt").read_text()


def test_retention_purges_files_but_keeps_records(src, tmp_path):
    d, dirs = src
    svc = Service(tmp_path / "data", OracleModels(d))
    cid = svc.submit_dir(dirs[0], run_now=True)
    svc.close_case(cid, "legitimate", "", "alice")
    assert svc.retention(days=30) == []  # too recent
    svc.store.db.execute("UPDATE cases SET checked = checked - 40 * 86400 WHERE case_id = ?", (cid,))
    assert svc.retention(days=30) == [cid]
    row = svc.store.case(cid)
    assert row["verdict"] and row["status"] == "closed:legitimate:purged"
    assert [p.name for p in (svc.data / "cases" / cid).iterdir()] == ["case.json"]
    assert svc.store.verify_audit()[0]


def test_drift_alert_on_sudden_change(tmp_path):
    svc = Service(tmp_path / "data")
    today = date.today()
    for i in range(1, 21):
        day = (today - timedelta(days=i)).isoformat()
        svc.store.db.execute("INSERT INTO daily_flags VALUES (?, '__cases__', 100, 100)", (day,))
        svc.store.db.execute("INSERT INTO daily_flags VALUES (?, 'card_match', 100, ?)", (day, 4 + i % 3))
    svc.store.db.execute("INSERT INTO daily_flags VALUES (?, '__cases__', 100, 100)", (today.isoformat(),))
    svc.store.db.execute("INSERT INTO daily_flags VALUES (?, 'card_match', 100, 5)", (today.isoformat(),))
    assert svc.drift() == []
    svc.store.db.execute("UPDATE daily_flags SET flagged = 30 WHERE day = ? AND check_id = 'card_match'", (today.isoformat(),))
    alerts = svc.drift()
    assert alerts and alerts[0]["check"] == "card_match" and alerts[0]["direction"] == "up"


def test_calibration_respects_capacity_and_prevalence():
    mk = lambda fraud, sev: cal.Labelled("expense_reports", fraud, [{"check": "card_match", "layer": "case", "severity": sev,  # noqa: E731
                                                                      "confidence": 0.9, "title": "t"}] if sev else [], {"region": "A"})
    data = [mk(True, "high")] * 18 + [mk(True, None)] * 2 + [mk(False, None)] * 95 + [mk(False, "medium")] * 5
    econ = cal.Economics(cases_per_day=1000, review_capacity_per_day=60, fraud_rate=0.02)
    rows = cal.sweep(cal.scores(data), econ)
    r = next(r for r in rows if r["threshold"] == 0.25)  # catches high (0.54) and medium (0.27)
    assert r["recall"] == 0.9 and r["false_alarm_rate"] == 0.05
    assert r["precision_at_prevalence"] == pytest.approx(0.9 * 0.02 / (0.9 * 0.02 + 0.05 * 0.98), abs=1e-3)
    best = cal.pick_threshold(rows)
    assert best["within_capacity"] and best["flagged_per_day"] <= 60
    md, out = cal.report(data, econ)
    assert "Recommended threshold" in md and out["recommended"] == best
