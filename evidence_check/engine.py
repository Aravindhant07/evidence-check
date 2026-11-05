"""The pipeline: ingest → extract (metadata + models) → checks → score → verdict."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from . import checks as _checks  # noqa: F401  (registers check plugins)
from .checks.base import REGISTRY, Context
from .extract.metadata import read_metadata
from .ingest import load_case
from .models import Case, Evidence, Finding, Pointer, Severity, Verdict
from .policy import DomainPolicy, load_policy, rank, score


def extract(evidence: list[Evidence], models: Any = None, policy: DomainPolicy | None = None, case: Case | None = None) -> list[str]:
    """Fill each evidence item's metadata and (if models are available) extraction. Returns skip notes."""
    notes = []
    for ev in evidence:
        ev.metadata = read_metadata(ev)
    if models is None:
        notes.append("model extractors not loaded: OCR, photo labels, transcripts and AI-image scores were skipped")
        for ev in evidence:  # PDFs with a text layer can still be parsed without models
            if ev.kind == "pdf" and ev.metadata.get("text"):
                from .extract.receipt import parse_lines
                from .models import TextLine

                ev.extraction.lines = [TextLine(text=t) for t in ev.metadata["text"].splitlines() if t.strip()]
                if ev.role in ("receipt", "invoice"):
                    ev.extraction.receipt = parse_lines(ev.extraction.lines)
        return notes
    notes += models.extract_all(evidence, policy, case)
    return notes


def run_case(case_dir: str | Path, policy: DomainPolicy | str | None = None, models: Any = None, history: Any = None,
             work_dir: str | Path | None = None, only: set[str] | None = None) -> tuple[Case, list[Evidence], Verdict]:
    case, evidence, rejected = load_case(case_dir)
    pol = policy if isinstance(policy, DomainPolicy) else load_policy(policy or case.domain)
    wd = Path(work_dir or tempfile.mkdtemp(prefix="evcheck-")) / case.case_id
    wd.mkdir(parents=True, exist_ok=True)
    notes = extract(evidence, models, pol, case)
    ctx = Context(case, evidence, pol, wd, history=history, models=models, skipped=list(notes))
    findings: list[Finding] = [
        Finding(check="ingest", layer="item", severity=Severity.MEDIUM, title="A file in this case could not be checked.",
                detail=msg, pointer=Pointer(text=msg)) for msg in rejected
    ]
    for cid, chk in REGISTRY.items():
        if not pol.checks.get(cid, getattr(chk, "default_enabled", True)) or (only and cid not in only):
            continue
        try:
            findings += chk.run(ctx)
        except Exception as e:  # one broken check never blocks the rest
            ctx.skipped.append(f"{cid}: failed ({type(e).__name__}: {e})"[:200])
    s, level = score(findings, pol)
    models_used = {}
    for ev in evidence:
        models_used.update(ev.extraction.models)
    v = Verdict(case_id=case.case_id, score=s, level=level, findings=rank(findings), policy=pol.tag,
                models=models_used, skipped=ctx.skipped)
    if history is not None:
        history.add_case(case, evidence)
    return case, evidence, v
