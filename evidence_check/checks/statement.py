"""Statement check: does what the claimant said (voice statement, transcribed) match the written claim?

Facts are extracted from the transcript by code (amounts, dates, loss words), not judged by a model.
"""

from __future__ import annotations

import re
from datetime import date

from ..models import Finding, Pointer, Severity
from .base import Context, register

UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                                    "fifteen sixteen seventeen eighteen nineteen".split())}
TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
SCALES = {"hundred": 100, "thousand": 1000}
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9,
            "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15, "sixteenth": 16,
            "seventeenth": 17, "eighteenth": 18, "nineteenth": 19, "twentieth": 20, "thirtieth": 30}
LOSS_WORDS = {
    "water_damage": ["water", "leak", "leaking", "flood", "flooded", "pipe", "burst", "soaked", "wet"],
    "fire": ["fire", "smoke", "burned", "burnt", "flames", "soot"],
    "theft": ["stolen", "stole", "theft", "burglary", "break-in", "broke in", "robbed", "thief"],
    "vehicle_collision": ["crash", "crashed", "collision", "accident", "hit", "rear-ended", "bumper", "car", "dent"],
    "windshield": ["windshield", "windscreen", "crack"],
}


def words_to_number(tokens: list[str]) -> float | None:
    total, current, seen = 0, 0, False
    for t in tokens:
        if t in UNITS:
            current += UNITS[t]
        elif t in TENS:
            current += TENS[t]
        elif t == "hundred":
            current = max(1, current) * 100
        elif t == "thousand":
            total += max(1, current) * 1000
            current = 0
        elif t == "and":
            continue
        else:
            break
        seen = True
    return float(total + current) if seen else None


def amounts(text: str) -> list[float]:
    t = text.lower().replace(",", "")
    out = [float(m) for m in re.findall(r"\$\s?(\d+(?:\.\d{1,2})?)", t)]
    out += [float(m) for m in re.findall(r"\b(\d+(?:\.\d{1,2})?)\s*(?:dollars|bucks|usd)\b", t)]
    toks = re.findall(r"[a-z]+|\d+", t)
    for i, tok in enumerate(toks):
        if tok in ("dollars", "bucks"):
            j = i
            while j > 0 and (toks[j - 1] in UNITS or toks[j - 1] in TENS or toks[j - 1] in SCALES or toks[j - 1] == "and"):
                j -= 1
            v = words_to_number(toks[j:i])
            if v:
                out.append(v)
    return out


ORD_WORD = r"(?:(?:twenty|thirty)[\s-]+)?(?:" + "|".join(ORDINALS) + r")"
DAY = r"(\d{1,2}(?:st|nd|rd|th)?|" + ORD_WORD + r")"
MONTH = r"(" + "|".join(MONTHS) + r")"


def day_value(s: str) -> int:
    """'14', '14th', 'fourteenth', 'twenty first', 'thirty-first' -> day of month."""
    m = re.match(r"\d+", s)
    if m:
        return int(m.group(0))
    parts = re.split(r"[\s-]+", s)
    return {"twenty": 20, "thirty": 30}.get(parts[0], 0) + ORDINALS[parts[-1]] if len(parts) > 1 else ORDINALS[s]


def dates(text: str, year_hint: int) -> list[date]:
    """Dates as people say them: 'March 3rd', 'march the third', 'january twenty ninth', 'the 3rd of March'."""
    t = text.lower()
    found = [(m.group(1), m.group(2)) for m in re.finditer(r"\b" + MONTH + r"\s+(?:the\s+)?" + DAY + r"\b", t)]
    found += [(m.group(2), m.group(1)) for m in re.finditer(r"\b" + DAY + r"\s+of\s+" + MONTH + r"\b", t)]
    out = []
    for month, day in found:
        try:
            out.append(date(year_hint, MONTHS[month], day_value(day)))
        except (ValueError, KeyError):
            pass
    return out


def loss_types(text: str) -> set[str]:
    t = " " + text.lower() + " "
    return {k for k, ws in LOSS_WORDS.items() if any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in ws)}


@register
class StatementCheck:
    id = "statement"
    layer = "case"

    def run(self, ctx: Context) -> list[Finding]:
        c, out = ctx.case, []
        for ev in ctx.evidence:
            text = ev.extraction.transcript
            if not text:
                continue
            said_amounts = [a for a in amounts(text) if a >= 20]
            if c.claimed_amount and said_amounts:
                closest = min(said_amounts, key=lambda a: abs(a - c.claimed_amount))
                if abs(closest - c.claimed_amount) > max(25.0, 0.1 * c.claimed_amount):
                    out.append(Finding(check=self.id, layer="case", severity=Severity.HIGH, confidence=0.75,
                                       title=f"In the voice statement the amount is about ${closest:,.0f}, but the claim is for ${c.claimed_amount:,.2f}.",
                                       detail=f"Amounts heard: {said_amounts}", pointer=Pointer(evidence_id=ev.id, text=text[:200])))
            if c.loss_type and c.loss_type in LOSS_WORDS:
                heard = loss_types(text)
                if heard and c.loss_type not in heard and not (c.loss_type == "windshield" and "vehicle_collision" in heard):
                    out.append(Finding(check=self.id, layer="case", severity=Severity.HIGH, confidence=0.7,
                                       title=f"The voice statement describes {', '.join(sorted(h.replace('_', ' ') for h in heard))}, but the claim is for {c.loss_type.replace('_', ' ')}.",
                                       detail=text[:300], pointer=Pointer(evidence_id=ev.id, text=text[:200])))
            if c.incident_date:
                ds = dates(text, c.incident_date.year)
                if ds and all(abs((d - c.incident_date).days) > 2 for d in ds):
                    out.append(Finding(check=self.id, layer="case", severity=Severity.MEDIUM, confidence=0.7,
                                       title=f"The voice statement gives the date as {ds[0]:%B %d}, but the claim says {c.incident_date:%B %d}.",
                                       detail=text[:300], pointer=Pointer(evidence_id=ev.id, text=text[:200])))
        return out
