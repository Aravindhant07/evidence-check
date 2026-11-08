"""Oracle perception: feed ground-truth OCR and transcripts from benchmark truth.json instead of running a vision model.

Used to evaluate the checks separately from the models ("if perception were perfect, what would we catch?").
"""

from __future__ import annotations

import json
from pathlib import Path

from ..models import Case, Evidence, TextLine
from .receipt import parse_lines


class OracleModels:
    name = "oracle"

    def __init__(self, truth_root: str | Path | None = None):
        """truth_root: where to find <case_id>/truth.json when it is not next to the evidence (stored bundles)."""
        self.truth_root = Path(truth_root) if truth_root else None

    def extract_all(self, evidence: list[Evidence], policy=None, case: Case | None = None) -> list[str]:
        notes = []
        for ev in evidence:
            truth = Path(ev.file).parent / "truth.json"
            if not truth.exists() and self.truth_root:
                truth = self.truth_root / ev.case_id / "truth.json"
            if not truth.exists():
                notes.append(f"oracle: no truth.json for {ev.name}")
                continue
            gt = json.loads(truth.read_text())
            if ev.kind == "audio" and ev.name in gt.get("transcripts", {}):
                ev.extraction.transcript = gt["transcripts"][ev.name]
                ev.extraction.models["speech"] = "oracle"
            ocr = gt.get("ocr", {}).get(ev.name)
            if ocr:
                ev.extraction.lines = [TextLine(text=t, box=tuple(b) if b else None) for t, b in ocr]
                if ev.role in ("receipt", "invoice"):
                    ev.extraction.receipt = parse_lines(ev.extraction.lines)
                ev.extraction.models["ocr"] = "oracle"
        return notes
