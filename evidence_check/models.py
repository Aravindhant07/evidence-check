"""Data model: cases, evidence, extractions, findings and verdicts."""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

Box = tuple[int, int, int, int]  # x0, y0, x1, y1 in image pixels


class CardTransaction(BaseModel):
    date: date
    merchant: str
    amount: float


class Location(BaseModel):
    lat: float | None = None
    lon: float | None = None
    address: str = ""


class Policy(BaseModel):
    start: date | None = None
    end: date | None = None


class EvidenceRef(BaseModel):
    file: str
    role: Literal["receipt", "invoice", "damage_photo", "product_photo", "document", "statement", "other"] = "other"


class Case(BaseModel):
    case_id: str
    domain: str
    claimant_id: str = ""
    claimant_name: str = ""
    segment: dict[str, str] = Field(default_factory=dict)  # e.g. region, channel: used by the fairness report
    claimed_amount: float | None = None
    currency: str = "USD"
    loss_type: str | None = None  # claims: water_damage, fire, theft, vehicle_collision ...
    category: str | None = None  # expenses: meals, travel, lodging ...
    merchant: str | None = None
    description: str = ""
    incident_date: date | None = None  # claims: date of loss; expenses: expense date
    filed_date: date | None = None
    policy: Policy = Field(default_factory=Policy)
    location: Location = Field(default_factory=Location)
    jurisdiction: str | None = None  # tax table key, e.g. US-NY
    card_transactions: list[CardTransaction] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)


class TextLine(BaseModel):
    text: str
    box: Box | None = None


class Field_(BaseModel):
    """An extracted value with the place it came from (verified against the OCR text)."""

    value: Any
    text: str = ""
    box: Box | None = None


class LineItem(BaseModel):
    description: str
    amount: float
    box: Box | None = None


class ReceiptFields(BaseModel):
    merchant: Field_ | None = None
    date: Field_ | None = None
    items: list[LineItem] = Field(default_factory=list)
    subtotal: Field_ | None = None
    tax: Field_ | None = None
    total: Field_ | None = None


class Extraction(BaseModel):
    lines: list[TextLine] = Field(default_factory=list)
    receipt: ReceiptFields | None = None
    caption: str | None = None
    labels: dict[str, float] = Field(default_factory=dict)  # zero-shot label -> probability
    transcript: str | None = None
    ai_generated_score: float | None = None
    embedding: list[float] | None = Field(default=None, exclude=True)  # image embedding for duplicate search (not serialised)
    models: dict[str, str] = Field(default_factory=dict)  # job -> model id@revision


class Evidence(BaseModel):
    id: str  # sha256 prefix
    case_id: str
    file: str  # path on disk
    name: str
    role: str
    kind: Literal["image", "pdf", "audio"]
    mime: str
    sha256: str
    size: int
    width: int | None = None
    height: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    extraction: Extraction = Field(default_factory=Extraction)


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Pointer(BaseModel):
    evidence_id: str | None = None
    box: Box | None = None
    heatmap: str | None = None  # path to a PNG overlay
    text: str | None = None  # quoted span
    case_field: str | None = None
    other_case: str | None = None  # for history findings
    other_evidence: str | None = None


class Finding(BaseModel):
    check: str
    layer: Literal["item", "case", "history"]
    severity: Severity
    confidence: float = 1.0
    title: str  # one plain-language sentence for reviewers
    detail: str = ""  # technical detail, shown on demand
    pointer: Pointer = Field(default_factory=Pointer)
    data: dict[str, Any] = Field(default_factory=dict)


class Level(str, Enum):
    CLEAR = "clear"
    REVIEW = "review"
    INVESTIGATE = "investigate"


class Verdict(BaseModel):
    case_id: str
    score: float
    level: Level
    findings: list[Finding]
    policy: str
    models: dict[str, str] = Field(default_factory=dict)
    skipped: list[str] = Field(default_factory=list)  # checks that could not run, and why
