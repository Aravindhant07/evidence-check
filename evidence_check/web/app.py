"""REST API and reviewer UI (FastAPI + Jinja).

Auth: API keys with roles, from EVCHECK_KEYS="key:role,key:role" or <data>/keys.json ({"key": "role"}).
Roles: intake (submit only) < reviewer (read, decide) < investigator (+ close cases) < admin (+ audit log).
With no keys configured the server runs in local mode (everyone is admin) and refuses to listen beyond localhost.
There is no endpoint that denies a claim: verdicts are recommendations and people close cases.
"""

from __future__ import annotations

import hmac
import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .. import report
from ..privacy import mask_verdict
from ..service import IntakeError, Service, safe_name

ROLES = {"intake": 0, "reviewer": 1, "investigator": 2, "admin": 3}
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
MAX_UPLOAD = 25 * 1024 * 1024


def load_keys(data_dir: Path) -> dict[str, str]:
    keys: dict[str, str] = {}
    f = data_dir / "keys.json"
    if f.exists():
        keys.update(json.loads(f.read_text()))
    for part in filter(None, os.environ.get("EVCHECK_KEYS", "").split(",")):
        k, _, role = part.partition(":")
        keys[k.strip()] = role.strip() or "reviewer"
    bad = {r for r in keys.values() if r not in ROLES}
    if bad:
        raise ValueError(f"unknown roles in keys: {bad}")
    return keys


class Decision(BaseModel):
    finding: int
    decision: str
    reason: str = ""
    seconds: float | None = None


class Closing(BaseModel):
    outcome: str
    note: str = ""


def create_app(service: Service, keys: dict[str, str] | None = None) -> FastAPI:
    keys = load_keys(service.data) if keys is None else keys
    local_mode = not keys
    app = FastAPI(title="evidence-check", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.service, app.state.local_mode = service, local_mode

    def who(request: Request) -> dict[str, str]:
        if local_mode:
            return {"name": "local", "role": "admin"}
        supplied = request.headers.get("x-api-key") or request.cookies.get("evcheck_key") or ""
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:]
        for k, role in keys.items():
            if supplied and hmac.compare_digest(k, supplied):
                return {"name": f"{role}:{k[:4]}…", "role": role}
        raise HTTPException(401, "missing or invalid API key")

    def need(role: str):
        def dep(request: Request) -> dict[str, str]:
            u = who(request)
            if ROLES[u["role"]] < ROLES[role]:
                raise HTTPException(403, f"needs role {role}")
            return u
        return dep

    def case_or_404(case_id: str) -> dict:
        row = service.store.case(case_id)
        if not row:
            raise HTTPException(404, "no such case")
        return row

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if exc.status_code == 401 and not request.url.path.startswith("/api") and request.method == "GET":
            return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    # -- health ----------------------------------------------------------------
    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/readyz")
    def readyz():
        service.store.db.execute("SELECT 1")
        return {"ok": True, "queue": service.store.queue_stats(), "history": service.history.stats(),
                "models": getattr(service.models, "name", "none")}

    # -- API -------------------------------------------------------------------
    @app.post("/api/cases", status_code=201)
    async def submit(case: str = Form(..., description="case.json content"), files: list[UploadFile] = File(default=[]),
                     wait: bool = False, user=Depends(need("intake"))):
        try:
            spec = json.loads(case)
        except json.JSONDecodeError as e:
            raise HTTPException(422, f"case is not valid JSON: {e}")
        blobs = {}
        for f in files:
            data = await f.read(MAX_UPLOAD + 1)
            if len(data) > MAX_UPLOAD:
                raise HTTPException(413, f"{f.filename} is larger than 25 MB")
            blobs[safe_name(f.filename or "file")] = data
        try:
            cid = service.submit(spec, blobs, actor=user["name"], run_now=wait)
        except IntakeError as e:
            raise HTTPException(422, str(e))
        row = service.store.case(cid)
        return {"case_id": cid, "status": row["status"], "verdict": public_verdict(mask_verdict(row["verdict"], user["role"])) if wait else None}

    @app.get("/api/cases")
    def list_cases(level: str | None = None, status: str | None = None, user=Depends(need("reviewer"))):
        return [queue_row(r) for r in service.store.cases(level, status)]

    @app.get("/api/cases/{case_id}")
    def get_case(case_id: str, user=Depends(need("reviewer"))):
        row = case_or_404(case_id)
        service.store.audit(user["name"], "view", case_id, {"via": "api"})
        return {**queue_row(row), "verdict": public_verdict(mask_verdict(row["verdict"], user["role"])),
                "decisions": service.store.decisions(case_id)}

    @app.post("/api/cases/{case_id}/decisions")
    def decide(case_id: str, d: Decision, user=Depends(need("reviewer"))):
        try:
            service.decide(case_id, d.finding, d.decision, d.reason, user["name"], d.seconds)
        except (KeyError, ValueError) as e:
            raise HTTPException(422, str(e))
        return {"ok": True}

    @app.post("/api/cases/{case_id}/close")
    def close(case_id: str, c: Closing, user=Depends(need("investigator"))):
        case_or_404(case_id)
        try:
            service.close_case(case_id, c.outcome, c.note, user["name"])
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"ok": True}

    @app.get("/api/metrics")
    def metrics(user=Depends(need("reviewer"))):
        return dashboard_data(service)

    @app.get("/api/audit")
    def audit(target: str | None = None, user=Depends(need("admin"))):
        ok, bad = service.store.verify_audit()
        return {"chain_ok": ok, "first_bad_row": bad, "entries": service.store.audit_log(target=target)}

    @app.get("/cases/{case_id}/report.md", response_class=PlainTextResponse)
    def report_md(case_id: str, user=Depends(need("reviewer"))):
        row = case_or_404(case_id)
        return report.markdown(row["verdict"]) if row["verdict"] else "Not checked yet."

    @app.get("/cases/{case_id}/report.html", response_class=HTMLResponse)
    def report_html(case_id: str, user=Depends(need("reviewer"))):
        row = case_or_404(case_id)
        if not row["verdict"]:
            raise HTTPException(409, "not checked yet")
        service.store.audit(user["name"], "export", case_id, {"format": "html"})
        return report.standalone_html(row["verdict"], row["case_dir"])

    # -- files (evidence and heatmaps), path-safe -----------------------------
    @app.get("/files/{case_id}/{name}")
    def evidence_file(case_id: str, name: str, user=Depends(need("reviewer"))):
        row = case_or_404(case_id)
        p = Path(row["case_dir"]) / safe_name(name)
        if not p.is_file() or p.name == "case.json":
            raise HTTPException(404, "no such file")
        return FileResponse(p, headers={"Content-Security-Policy": "default-src 'none'", "X-Content-Type-Options": "nosniff"})

    @app.get("/artefacts/{case_id}/{name}")
    def artefact(case_id: str, name: str, user=Depends(need("reviewer"))):
        case_or_404(case_id)
        p = service.work_dir / safe_name(case_id) / safe_name(name)
        if not p.is_file():
            raise HTTPException(404, "no such file")
        return FileResponse(p, headers={"X-Content-Type-Options": "nosniff"})

    # -- UI ----------------------------------------------------------------------
    def page(request: Request, name: str, user: dict, **ctx: Any):
        return TEMPLATES.TemplateResponse(request, name, {"user": user, "local_mode": local_mode, **ctx})

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/"):
        return page(request, "login.html", {"name": "", "role": ""}, next=next if next.startswith("/") else "/", error=None)

    @app.post("/login")
    def login(request: Request, key: str = Form(...), next: str = Form("/")):
        if not any(hmac.compare_digest(k, key) for k in keys):
            return page(request, "login.html", {"name": "", "role": ""}, next=next, error="That key is not valid.")
        r = RedirectResponse(next if next.startswith("/") and not next.startswith("//") else "/", status_code=303)
        r.set_cookie("evcheck_key", key, httponly=True, samesite="strict", max_age=12 * 3600)
        return r

    @app.get("/logout")
    def logout():
        r = RedirectResponse("/login", status_code=303)
        r.delete_cookie("evcheck_key")
        return r

    @app.get("/", response_class=HTMLResponse)
    def queue_page(request: Request, level: str | None = None, user=Depends(need("reviewer"))):
        rows = [queue_row(r) for r in service.store.cases(level)]
        counts = {lv: sum(1 for r in service.store.cases() if r["level"] == lv) for lv in ("investigate", "review", "clear")}
        return page(request, "queue.html", user, rows=rows, level=level, counts=counts)

    @app.get("/cases/{case_id}", response_class=HTMLResponse)
    def case_page(request: Request, case_id: str, user=Depends(need("reviewer"))):
        row = case_or_404(case_id)
        service.store.audit(user["name"], "view", case_id, {"via": "ui"})
        order = [r["case_id"] for r in service.store.cases() if r["level"] in ("investigate", "review")]
        i = order.index(case_id) if case_id in order else -1
        nxt = order[i + 1] if 0 <= i < len(order) - 1 else None
        v = mask_verdict(row["verdict"], user["role"])
        others = {}  # earlier evidence for duplicate findings, shown side by side
        for i, f in enumerate((v or {}).get("findings", [])):
            p = f.get("pointer") or {}
            other = service.store.case(p["other_case"]) if p.get("other_case") else None
            if other and other["verdict"]:
                e = next((e for e in other["verdict"]["evidence"] if e["id"] == p.get("other_evidence")), None)
                if e and e["kind"] == "image":
                    others[i] = {"case_id": other["case_id"], "name": e["name"], "claimant_id": other["claimant_id"]}
        return page(request, "case.html", user, row=row, v=v, summary=report.summary(v) if v else "", others=others,
                    decisions=service.store.latest_decisions(case_id), next_case=nxt, opened=time.time(),
                    boxes=boxes_by_evidence(v) if v else {}, can_close=ROLES[user["role"]] >= ROLES["investigator"])

    @app.post("/cases/{case_id}/findings/{finding}")
    def decide_form(case_id: str, finding: int, decision: str = Form(...), reason: str = Form(""),
                    opened: float = Form(0), user=Depends(need("reviewer"))):
        seconds = round(time.time() - opened, 1) if opened else None
        try:
            service.decide(case_id, finding, decision, reason, user["name"], seconds)
        except (KeyError, ValueError) as e:
            raise HTTPException(422, str(e))
        return RedirectResponse(f"/cases/{case_id}#f{finding}", status_code=303)

    @app.post("/cases/{case_id}/close")
    def close_form(case_id: str, outcome: str = Form(...), note: str = Form(""), user=Depends(need("investigator"))):
        try:
            service.close_case(case_id, outcome, note, user["name"])
        except ValueError as e:
            raise HTTPException(422, str(e))
        return RedirectResponse(f"/cases/{case_id}", status_code=303)

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request, user=Depends(need("reviewer"))):
        return page(request, "dashboard.html", user, d=dashboard_data(service))

    return app


# -- view helpers ------------------------------------------------------------------

def public_verdict(v: dict | None) -> dict | None:
    if not v:
        return None
    return {**v, "summary": report.summary(v)}


def queue_row(r: dict) -> dict:
    v = r.get("verdict") or {}
    fs = report.scored(v.get("findings", []))
    case = v.get("case", {})
    return {"case_id": r["case_id"], "domain": r["domain"], "status": r["status"], "level": r["level"], "score": r["score"],
            "claimant_id": r["claimant_id"], "amount": case.get("claimed_amount"),
            "kind": (case.get("loss_type") or case.get("category") or "").replace("_", " "),
            "top_reason": fs[0]["title"] if fs else "", "reasons": len(fs), "checked": r["checked"]}


def boxes_by_evidence(v: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for i, f in enumerate(v["findings"]):
        p = f.get("pointer") or {}
        if p.get("evidence_id") and (p.get("box") or p.get("heatmap")):
            out.setdefault(p["evidence_id"], []).append({"i": i, "box": p.get("box"), "sev": f["severity"],
                                                         "heatmap": Path(p["heatmap"]).name if p.get("heatmap") else None})
    return out


def dashboard_data(service: Service) -> dict:
    rows = service.store.cases()
    checked = [r for r in rows if r["verdict"]]
    by_level = {lv: sum(1 for r in checked if r["level"] == lv) for lv in ("clear", "review", "investigate")}
    precision = service.store.check_precision()
    fired: dict[str, int] = {}
    for r in checked:
        for c in {f["check"] for f in report.scored(r["verdict"]["findings"])}:
            fired[c] = fired.get(c, 0) + 1
    times = [d["seconds"] for d in service.store.decisions() if d["seconds"]]
    return {"cases": len(rows), "checked": len(checked), "by_level": by_level, "queue": service.store.queue_stats(),
            "fired": dict(sorted(fired.items(), key=lambda kv: -kv[1])), "precision": precision,
            "median_decision_s": sorted(times)[len(times) // 2] if times else None,
            "audit_ok": service.store.verify_audit()[0], "history": service.history.stats(), "drift": service.drift()}
