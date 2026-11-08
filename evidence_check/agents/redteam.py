"""Red team: try to launder a flagged case until it passes, to find blind spots before fraudsters do.

The search is guided by which checks fired (attack the evidence that gave the case away) and composes up to two
laundering transforms per file. Variants that come out CLEAR are evasions: they are saved and proposed as new
evaluation cases, so every future policy change is tested against them.
"""

from __future__ import annotations

import itertools
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..bench.robust import TRANSFORMS, launder
from ..engine import run_case
from ..models import Level

# Which transforms plausibly defeat which check (the red team tries these first).
TARGETS = {
    "metadata": ["strip_metadata", "screenshot"], "dates": ["strip_metadata", "screenshot"],
    "gps_location": ["strip_metadata", "screenshot"], "content_credentials": ["strip_metadata", "screenshot"],
    "generator_traces": ["strip_metadata", "rephoto"], "copy_move": ["resize", "rephoto", "recompress"],
    "duplicate_evidence": ["crop", "rephoto", "screenshot"], "error_level": ["recompress", "resize"],
}


@dataclass
class Attempt:
    transforms: tuple[str, ...]
    level: str
    score: float
    checks: list[str]


@dataclass
class RedTeamResult:
    case_id: str
    original_level: str
    original_checks: list[str]
    attempts: list[Attempt] = field(default_factory=list)
    evasions: list[dict] = field(default_factory=list)


def plan(fired: list[str], max_depth: int = 2) -> list[tuple[str, ...]]:
    first = []
    for c in fired:
        first += [t for t in TARGETS.get(c, []) if t not in first]
    rest = [t for t in TRANSFORMS if t != "none" and t not in first]
    singles = [(t,) for t in first + rest]
    pairs = [p for p in itertools.permutations(first + rest, 2)] if max_depth >= 2 else []
    return singles + pairs


def red_team(case_dir: str | Path, models=None, history=None, out_dir: str | Path | None = None, budget: int = 20) -> RedTeamResult:
    case_dir = Path(case_dir)
    out = Path(out_dir or case_dir.parent / f"{case_dir.name}-redteam")
    snap = (lambda: history.snapshot()) if history is not None else (lambda: None)  # never write into the real history
    case, _, v = run_case(case_dir, models=models, history=snap())
    fired = sorted({f.check for f in v.findings if f.severity.value != "info"})
    res = RedTeamResult(case.case_id, v.level.value, fired)
    if v.level is Level.CLEAR:
        return res
    for i, combo in enumerate(plan(fired)[:budget]):
        dst = out / f"v{i:02d}-{'+'.join(combo)}"
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(case_dir, dst)
        for t in combo:
            launder([dst], t, seed=i)
        _, _, vv = run_case(dst, models=models, history=snap())
        checks = sorted({f.check for f in vv.findings if f.severity.value != "info"})
        res.attempts.append(Attempt(combo, vv.level.value, vv.score, checks))
        if vv.level is Level.CLEAR:
            res.evasions.append({"transforms": list(combo), "path": str(dst), "defeated": sorted(set(fired) - set(checks))})
        else:
            shutil.rmtree(dst, ignore_errors=True)
    return res
