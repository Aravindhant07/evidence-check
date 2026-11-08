"""One service behind every interface (CLI, REST API, reviewer UI, MCP, workers): intake, checking, decisions.

Layout of a data directory:
    data/evcheck.db       cases, verdicts, decisions, jobs, audit log
    data/history.jsonl    history index (every piece of evidence ever checked)
    data/cases/<id>/      stored case bundles (case.json + evidence files)
    data/work/<id>/       artefacts (heatmaps)
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

from .engine import run_case
from .history import HistoryIndex
from .ingest import load_case
from .models import Evidence
from .store import Store

SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")
MAX_FILES = 40


class IntakeError(ValueError):
    pass


def safe_name(name: str) -> str:
    base = Path(str(name).replace("\\", "/")).name
    base = SAFE_ID.sub("_", base).lstrip(".")
    if not base:
        raise IntakeError(f"invalid file name {name!r}")
    return base[:120]


class Service:
    def __init__(self, data_dir: str | Path, models: Any = None, webhook: Any = None):
        self.data = Path(data_dir)
        self.data.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.data / "evcheck.db")
        self.history = HistoryIndex(self.data / "history.jsonl")
        self.models = models
        self.webhook = webhook  # callable(case_id, verdict_dict) for integrations
        self.cases_dir = self.data / "cases"
        self.work_dir = self.data / "work"
        self._stop = threading.Event()

    # -- intake ----------------------------------------------------------------
    def submit_dir(self, src: str | Path, actor: str = "cli", run_now: bool = False) -> str:
        """Copy a case bundle into the data directory (only case.json and the files it lists) and queue it."""
        src = Path(src)
        try:
            spec = json.loads((src / "case.json").read_text())
        except Exception as e:
            raise IntakeError(f"case.json missing or not valid JSON ({e})") from e
        files = {}
        for ref in spec.get("evidence", [])[:MAX_FILES]:
            name = safe_name(ref.get("file", ""))
            p = src / ref.get("file", "")
            if p.is_file():
                files[name] = p.read_bytes()
        return self.submit(spec, files, actor, run_now)

    def submit(self, spec: dict, files: dict[str, bytes], actor: str = "api", run_now: bool = False) -> str:
        if not isinstance(spec, dict) or not spec.get("case_id"):
            raise IntakeError("case.json needs a case_id")
        case_id = SAFE_ID.sub("_", str(spec["case_id"]))[:64]
        if len(files) > MAX_FILES:
            raise IntakeError(f"too many files (max {MAX_FILES})")
        spec = {**spec, "case_id": case_id}
        spec["evidence"] = [{**ref, "file": safe_name(ref.get("file", ""))} for ref in spec.get("evidence", [])[:MAX_FILES]]
        staging = Path(tempfile.mkdtemp(prefix="intake-", dir=self.data))
        try:
            for name, data in files.items():
                (staging / safe_name(name)).write_bytes(data)
            (staging / "case.json").write_text(json.dumps(spec))
            case, _, rejected = load_case(staging)  # validates case.json and sniffs every file before we accept it
            dest = self.cases_dir / case_id
            if dest.exists():
                shutil.rmtree(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staging), dest)
        except IntakeError:
            raise
        except Exception as e:
            raise IntakeError(f"case rejected: {type(e).__name__}: {e}") from e
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        self.store.upsert_case(case_id, case.domain, case.claimant_id, str(dest), case.segment)
        self.store.audit(actor, "submit", case_id, {"files": sorted(files), "rejected": rejected})
        if run_now:
            self.process(case_id)
        else:
            self.store.enqueue(case_id)
        return case_id

    # -- checking ----------------------------------------------------------------
    def process(self, case_id: str, actor: str = "worker") -> dict:
        row = self.store.case(case_id)
        if not row:
            raise KeyError(case_id)
        self.store.set_status(case_id, "running")
        t0 = time.perf_counter()
        case, evidence, v = run_case(row["case_dir"], models=self.models, history=self.history, work_dir=self.work_dir)
        verdict = v.model_dump(mode="json")
        verdict["evidence"] = [evidence_view(e) for e in evidence]
        verdict["seconds"] = round(time.perf_counter() - t0, 2)
        verdict["case"] = case.model_dump(mode="json", exclude={"card_transactions"})
        self.store.save_verdict(case_id, verdict)
        fired = {f["check"] for f in verdict["findings"] if f["severity"] != "info"}
        self.store.record_flags(date.today().isoformat(), fired)
        self.store.audit(actor, "check", case_id, {"level": verdict["level"], "score": verdict["score"], "policy": verdict["policy"]})
        if self.webhook:
            try:
                self.webhook(case_id, verdict)
            except Exception as e:  # an unreachable integration never loses the verdict
                self.store.audit("system", "webhook_failed", case_id, {"error": str(e)[:200]})
        return verdict

    def work_once(self) -> bool:
        job = self.store.claim_job()
        if not job:
            return False
        try:
            self.process(job["case_id"])
            self.store.finish_job(job["id"])
        except Exception as e:
            self.store.finish_job(job["id"], f"{type(e).__name__}: {e}")
            self.store.set_status(job["case_id"], "error")
        return True

    def run_worker(self, poll_s: float = 1.0) -> None:
        self.store.requeue_stale()
        while not self._stop.is_set():
            if not self.work_once():
                self._stop.wait(poll_s)

    def start_worker_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.run_worker, daemon=True, name="evcheck-worker")
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()

    def drain(self) -> int:
        n = 0
        while self.work_once():
            n += 1
        return n

    # -- review --------------------------------------------------------------------
    def decide(self, case_id: str, finding: int, decision: str, reason: str, actor: str, seconds: float | None = None) -> None:
        if decision not in ("confirm", "dismiss"):
            raise ValueError("decision must be confirm or dismiss")
        row = self.store.case(case_id)
        if not row or not row["verdict"] or not 0 <= finding < len(row["verdict"]["findings"]):
            raise KeyError(f"{case_id}#{finding}")
        check = row["verdict"]["findings"][finding]["check"]
        self.store.decide(case_id, finding, check, decision, reason[:500], actor, seconds)
        self.store.audit(actor, decision, case_id, {"finding": finding, "check": check, "reason": reason[:200]})

    def close_case(self, case_id: str, outcome: str, note: str, actor: str) -> None:
        """Record the human outcome. There is deliberately no automatic denial: people decide."""
        if outcome not in ("legitimate", "fraud", "needs_info"):
            raise ValueError("outcome must be legitimate, fraud or needs_info")
        self.store.set_status(case_id, f"closed:{outcome}")
        self.store.audit(actor, "close", case_id, {"outcome": outcome, "note": note[:500]})

    # -- operations ----------------------------------------------------------------
    def drift(self, baseline_days: int = 30, min_days: int = 7, sigmas: float = 3.0) -> list[dict]:
        """Checks whose flag rate today is outside the control band of the previous days (mean ± 3σ).
        A sudden change usually means the input changed (a new phone model, a scanner, a partner's export)
        or a fraud pattern appeared; either way someone should look."""
        import statistics

        today = date.today().isoformat()
        totals = {r["day"]: r["cases"] for r in self.store.flag_series("__cases__")}
        days = sorted(d for d in totals if d < today)[-baseline_days:]
        if len(days) < min_days or today not in totals:
            return []
        alerts = []
        for chk, series in self.store.daily_rates().items():
            base = [series.get(d, 0.0) for d in days]
            mu, sd = statistics.fmean(base), statistics.pstdev(base)
            now = series.get(today, 0.0)
            band = sigmas * max(sd, 0.01)
            if abs(now - mu) > band:
                alerts.append({"check": chk, "today": round(now, 3), "baseline": round(mu, 3), "band": round(band, 3),
                               "direction": "up" if now > mu else "down"})
        return alerts

    def retention(self, days: int, actor: str = "retention") -> list[str]:
        """Delete the evidence files of cases closed more than `days` ago. The verdict, decisions, audit log and
        history fingerprints (hashes, not images) are kept, so reuse can still be detected later."""
        cutoff = time.time() - days * 86400
        removed = []
        for r in self.store.cases():
            if r["status"].startswith("closed:") and (r["checked"] or 0) < cutoff and Path(r["case_dir"]).exists():
                for p in Path(r["case_dir"]).iterdir():
                    if p.name != "case.json":
                        p.unlink()
                shutil.rmtree(self.work_dir / r["case_id"], ignore_errors=True)
                self.store.set_status(r["case_id"], r["status"] + ":purged")
                self.store.audit(actor, "retention_delete", r["case_id"], {"older_than_days": days})
                removed.append(r["case_id"])
        return removed

    def find_similar(self, case_id: str, evidence_id: str) -> list[dict]:
        row = self.store.case(case_id)
        if not row or not row["verdict"]:
            return []
        return [f for f in row["verdict"]["findings"] if f["check"] == "duplicate_evidence"
                and (f.get("pointer") or {}).get("evidence_id") == evidence_id]


def evidence_view(e: Evidence) -> dict:
    """What the reviewer UI and reports show about one file (no embeddings, no raw metadata dump)."""
    x = e.extraction
    md = e.metadata
    return {
        "id": e.id, "name": e.name, "role": e.role, "kind": e.kind, "mime": e.mime, "size": e.size,
        "width": e.width, "height": e.height, "sha256": e.sha256,
        "capture_time": md.get("capture_time"), "gps": md.get("gps"),
        "camera": " ".join(v for v in (md.get("exif", {}).get("Make"), md.get("exif", {}).get("Model")) if v) or None,
        "software": md.get("exif", {}).get("Software"),
        "lines": [{"text": l.text, "box": l.box} for l in x.lines][:200],
        "receipt": x.receipt.model_dump(mode="json") if x.receipt else None,
        "caption": x.caption, "labels": dict(sorted((x.labels or {}).items(), key=lambda kv: -kv[1])[:5]),
        "transcript": x.transcript, "ai_generated_score": x.ai_generated_score, "models": x.models,
    }
