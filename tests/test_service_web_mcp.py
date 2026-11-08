"""Service, store, REST API + reviewer UI, MCP server and CLI."""

import json

import anyio
import pytest
from fastapi.testclient import TestClient

from evidence_check.bench.cases import Builder
from evidence_check.extract.oracle import OracleModels
from evidence_check.service import IntakeError, Service
from evidence_check.store import Store
from evidence_check.web.app import create_app


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    src = tmp_path_factory.mktemp("src")
    b = Builder(src, seed=3)
    clean = [b.claim(), b.claim(), b.expense()]
    bad = [b.claim("reused_photo"), b.expense("edited_total")]
    return src, clean, bad


@pytest.fixture()
def svc(tmp_path, built):
    src, clean, bad = built
    s = Service(tmp_path / "data", OracleModels(src))
    for d in clean + bad:
        s.submit_dir(d, actor="test")
    assert s.drain() == 5
    return s


# -- store ---------------------------------------------------------------------

def test_audit_chain_detects_tampering(tmp_path):
    st = Store(tmp_path / "x.db")
    for i in range(5):
        st.audit("alice", "view", f"C{i}")
    assert st.verify_audit() == (True, None)
    st.db.execute("UPDATE audit SET actor='mallory' WHERE id=3")
    assert st.verify_audit() == (False, 3)


def test_job_queue_retries_then_fails(tmp_path):
    st = Store(tmp_path / "x.db")
    st.enqueue("C1")
    for attempt in range(3):
        job = st.claim_job()
        assert job and job["case_id"] == "C1"
        st.finish_job(job["id"], "boom")
    assert st.claim_job() is None and st.queue_stats() == {"failed": 1}


# -- service ---------------------------------------------------------------------

def test_service_checks_and_ranks(svc):
    rows = svc.store.cases()
    assert rows[0]["level"] in ("investigate", "review") and rows[-1]["level"] == "clear"
    assert {r["status"] for r in rows} == {"checked"}
    assert svc.store.verify_audit()[0]


def test_decisions_feed_precision(svc):
    flagged = [r for r in svc.store.cases() if r["level"] != "clear"][0]
    svc.decide(flagged["case_id"], 0, "confirm", "", "alice", 12.0)
    check = flagged["verdict"]["findings"][0]["check"]
    assert svc.store.check_precision()[check]["precision"] == 1.0
    with pytest.raises(ValueError):
        svc.decide(flagged["case_id"], 0, "deny", "", "alice")


def test_intake_rejects_traversal_and_junk(svc):
    case = {"case_id": "../../etc", "domain": "expense_reports", "evidence": [{"file": "../../x.jpg", "role": "receipt"}]}
    cid = svc.submit(case, {"../../x.jpg": b"not an image"}, run_now=True)
    assert cid == ".._.._etc" or "/" not in cid
    stored = svc.data / "cases" / cid
    assert stored.resolve().is_relative_to((svc.data / "cases").resolve())
    v = svc.store.case(cid)["verdict"]
    assert any(f["check"] == "ingest" for f in v["findings"])  # the junk file is reported, not processed
    with pytest.raises(IntakeError):
        svc.submit({"domain": "x"}, {})


# -- web ---------------------------------------------------------------------------

def test_local_mode_ui_and_api(svc):
    c = TestClient(create_app(svc, keys={}))
    assert c.get("/healthz").json() == {"ok": True}
    assert c.get("/readyz").json()["ok"]
    q = c.get("/")
    assert q.status_code == 200 and "Review queue" in q.text
    top = c.get("/api/cases").json()[0]
    page = c.get(f"/cases/{top['case_id']}")
    assert page.status_code == 200 and "Reasons" in page.text and "Confirm" in page.text
    dash = c.get("/dashboard")
    assert dash.status_code == 200
    for html in (q.text, dash.text, page.text):  # no Python objects leaking into pages (e.g. dict.clear)
        assert "built-in method" not in html and " object at 0x" not in html
    assert "<html" in c.get(f"/cases/{top['case_id']}/report.html").text
    r = c.post(f"/cases/{top['case_id']}/findings/0", data={"decision": "dismiss", "reason": "known customer"}, follow_redirects=False)
    assert r.status_code == 303
    assert c.get(f"/api/cases/{top['case_id']}").json()["decisions"][-1]["decision"] == "dismiss"
    assert c.get("/files/" + top["case_id"] + "/case.json").status_code == 404
    assert c.get("/api/audit").json()["chain_ok"]


def test_roles_are_enforced(svc):
    keys = {"k-intake": "intake", "k-rev": "reviewer", "k-inv": "investigator", "k-admin": "admin"}
    c = TestClient(create_app(svc, keys=keys))
    assert c.get("/api/cases").status_code == 401
    assert c.get("/", follow_redirects=False).headers["location"].startswith("/login")
    assert c.get("/api/cases", headers={"x-api-key": "k-intake"}).status_code == 403
    rows = c.get("/api/cases", headers={"x-api-key": "k-rev"}).json()
    cid = rows[0]["case_id"]
    assert c.post(f"/api/cases/{cid}/close", json={"outcome": "fraud"}, headers={"x-api-key": "k-rev"}).status_code == 403
    assert c.post(f"/api/cases/{cid}/close", json={"outcome": "fraud"}, headers={"authorization": "Bearer k-inv"}).status_code == 200
    assert c.get("/api/audit", headers={"x-api-key": "k-inv"}).status_code == 403
    assert c.get("/api/audit", headers={"x-api-key": "k-admin"}).status_code == 200
    login = c.post("/login", data={"key": "k-rev", "next": "/"}, follow_redirects=False)
    assert login.status_code == 303 and "evcheck_key" in login.headers["set-cookie"]


def test_api_submit_with_files(svc, built):
    _, clean, _ = built
    c = TestClient(create_app(svc, keys={"k": "intake"}))
    spec = json.loads((clean[2] / "case.json").read_text())
    spec["case_id"] = "API-1"
    files = [("files", (ref["file"], (clean[2] / ref["file"]).read_bytes())) for ref in spec["evidence"]]
    r = c.post("/api/cases?wait=true", data={"case": json.dumps(spec)}, files=files, headers={"x-api-key": "k"})
    assert r.status_code == 201, r.text
    assert r.json()["verdict"]["summary"]
    assert c.post("/api/cases", data={"case": "{bad"}, headers={"x-api-key": "k"}).status_code == 422


# -- MCP -------------------------------------------------------------------------------

def test_mcp_tools(svc, built):
    from mcp import Client

    from evidence_check.mcp_server import build_server

    _, clean, _ = built
    inbox = svc.data / "inbox"
    import shutil

    shutil.copytree(clean[0], inbox / "new-case")

    async def go():
        async with Client(build_server(svc)) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert {"get_case", "find_similar", "check_case"} <= names
            assert not names & {"decide", "close_case", "deny"}  # agents cannot decide
            q = (await c.call_tool("list_queue", {"level": "investigate"})).structured_content
            dup = next(r for r in svc.store.cases() if any(f["check"] == "duplicate_evidence" for f in r["verdict"]["findings"]))
            case = (await c.call_tool("get_case", {"case_id": dup["case_id"]})).structured_content
            f = next(f for f in case["findings"] if f["check"] == "duplicate_evidence")
            sim = (await c.call_tool("find_similar", {"case_id": dup["case_id"], "evidence_id": f["evidence_id"]})).structured_content
            assert sim["result"][0]["other_case"]
            bad = await c.call_tool("check_case", {"case_folder": "../../"})
            assert bad.is_error
            ok = (await c.call_tool("check_case", {"case_folder": "new-case"})).structured_content
            assert ok["level"] == "clear"
            return q

    anyio.run(go)


# -- CLI -----------------------------------------------------------------------------------

def test_cli_check(built, tmp_path):
    from typer.testing import CliRunner

    from evidence_check.cli import app

    _, _, bad = built
    r = CliRunner().invoke(app, ["check", str(bad[1]), "--models", "oracle", "--fail-on", "review", "--html", str(tmp_path / "r.html")])
    assert r.exit_code == 1, r.output
    assert "Reasons" in r.output and (tmp_path / "r.html").exists()
    r = CliRunner().invoke(app, ["check", str(bad[1]), "--models", "oracle", "--json"])
    assert json.loads(r.stdout)["level"] != "clear"
