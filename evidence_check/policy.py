"""Domain policies (YAML) and scoring."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .models import Finding, Level, Severity

ROOT = Path(__file__).resolve().parent  # domains/ ships inside the package


class Thresholds(BaseModel):
    review: float = 0.35
    investigate: float = 0.75


class Tolerances(BaseModel):
    amount_pct: float = 0.02  # evidence total vs claimed amount
    amount_abs: float = 1.0
    card_days: int = 3  # receipt date vs card transaction date
    card_amount_abs: float = 0.5
    max_photo_age_days: int = 365  # photo taken long before the incident
    max_filing_delay_days: int = 60
    gps_km: float = 25.0
    duplicate_phash: int = 8  # Hamming distance on a 64-bit perceptual hash
    duplicate_clip: float = 0.95  # cosine similarity
    ela_ratio: float = 3.0  # a region's error level vs the image median
    ai_score: float = 0.8


class DomainPolicy(BaseModel):
    name: str
    version: str = "1"
    description: str = ""
    checks: dict[str, bool] = Field(default_factory=dict)  # check id -> enabled (default enabled)
    weights: dict[str, float] = Field(default_factory=lambda: {"info": 0.0, "low": 0.1, "medium": 0.3, "high": 0.6, "critical": 0.9})
    check_weights: dict[str, float] = Field(default_factory=dict)  # per-check multiplier (calibration / improvement agent)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    tolerances: Tolerances = Field(default_factory=Tolerances)
    tax_rates: dict[str, list[float]] = Field(default_factory=dict)  # jurisdiction -> allowed rates (0.08875 ...)
    loss_labels: dict[str, list[str]] = Field(default_factory=dict)  # loss type / category -> CLIP prompts
    mask_fields: list[str] = Field(default_factory=lambda: ["claimant_name"])

    def enabled(self, check_id: str) -> bool:
        return self.checks.get(check_id, True)

    @property
    def tag(self) -> str:
        return f"{self.name}@{self.version}"


def load_policy(name_or_path: str | Path) -> DomainPolicy:
    p = Path(name_or_path)
    if not p.exists():
        p = ROOT / "domains" / f"{name_or_path}.yaml"
    return DomainPolicy.model_validate(yaml.safe_load(p.read_text()))


def score(findings: list[Finding], policy: DomainPolicy) -> tuple[float, Level]:
    """Noisy-OR over findings: independent pieces of evidence add up, no single weak signal dominates."""
    keep = 1.0
    for f in findings:
        w = policy.weights.get(f.severity.value, 0.0) * policy.check_weights.get(f.check, 1.0)
        keep *= 1.0 - min(0.99, max(0.0, w * f.confidence))
    s = round(1.0 - keep, 4)
    if s >= policy.thresholds.investigate:
        return s, Level.INVESTIGATE
    if s >= policy.thresholds.review:
        return s, Level.REVIEW
    return s, Level.CLEAR


SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3, Severity.INFO: 4}


def rank(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], -f.confidence, f.check))


def policy_dict(p: DomainPolicy) -> dict[str, Any]:
    return p.model_dump(mode="json")
