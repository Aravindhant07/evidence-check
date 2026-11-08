"""PII masking by role, and redaction of card numbers anywhere text is shown.

Reviewers (and agents) see what they need to judge evidence, not who the person is: names become initials,
addresses are hidden, GPS is rounded to about 1 km. Investigators and admins see everything. Card numbers
in OCR text are always cut to the last four digits.
"""

from __future__ import annotations

import copy
import re

FULL_ACCESS = {"investigator", "admin"}
CARD = re.compile(r"\b(?:\d[ -]?){12,15}(\d{4})\b")


def redact_cards(text: str | None) -> str | None:
    return CARD.sub(lambda m: "•••• " + m.group(1), text) if text else text


def initials(name: str | None) -> str:
    return " ".join(p[0] + "." for p in (name or "").split() if p) or ""


def mask_verdict(v: dict | None, role: str) -> dict | None:
    if not v:
        return v
    v = copy.deepcopy(v)
    for e in v.get("evidence", []):
        for line in e.get("lines", []):
            line["text"] = redact_cards(line["text"])
        if e.get("transcript"):
            e["transcript"] = redact_cards(e["transcript"])
    if role in FULL_ACCESS:
        return v
    case = v.get("case") or {}
    if case.get("claimant_name"):
        case["claimant_name"] = initials(case["claimant_name"])
    if isinstance(case.get("location"), dict):
        case["location"] = {**case["location"], "address": None}
    for e in v.get("evidence", []):
        if isinstance(e.get("gps"), dict):
            e["gps"] = {k: round(x, 2) for k, x in e["gps"].items()}
    return v
