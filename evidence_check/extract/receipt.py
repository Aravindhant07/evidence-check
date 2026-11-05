"""Deterministic receipt/invoice parser over text lines (from OCR or a PDF text layer).

Every field keeps the line (and box, when OCR provides one) it came from, so findings can
highlight it and so no value exists that is not actually printed on the document.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from ..models import Field_, LineItem, ReceiptFields, TextLine

MONEY = re.compile(r"(?<![\d.])-?\$?\s?(\d{1,3}(?:,\d{3})*|\d+)\.(\d{2})(?!\d)")
TOTAL = re.compile(r"^\s*(GRAND\s+TOTAL|TOTAL\s+DUE|AMOUNT\s+DUE|BALANCE\s+DUE|TOTAL)\b", re.I)
SUBTOTAL = re.compile(r"^\s*(SUB\s*-?\s*TOTAL|NET\s+AMOUNT)\b", re.I)
TAX = re.compile(r"^\s*(SALES\s+TAX|TAX|VAT|GST|HST)\b", re.I)
NOT_ITEM = re.compile(r"^\s*(TOTAL|SUBTOTAL|SUB TOTAL|TAX|VAT|GST|HST|CHANGE|CASH|TENDER(ED)?|VISA|MASTERCARD|AMEX|CARD|"
                      r"PAYMENT|BALANCE|DUE|TIP|GRATUITY|DISCOUNT|SAVINGS|AUTH|APPROVED)\b", re.I)
MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
DATE_PATTERNS = [
    (re.compile(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b"), "ymd"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2}|\d{2})\b"), "mdy"),
    (re.compile(r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?\s+(\d{1,2}),?\s+(20\d{2})\b", re.I), "Mdy"),
    (re.compile(r"\b(\d{1,2})\s+(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?\s+(20\d{2})\b", re.I), "dMy"),
]


def money_values(text: str) -> list[float]:
    out = []
    for m in MONEY.finditer(text):
        v = float(m.group(1).replace(",", "") + "." + m.group(2))
        out.append(-v if m.group(0).lstrip().startswith("-") else v)
    return out


def parse_date(text: str) -> date | None:
    for pat, kind in DATE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        try:
            if kind == "ymd":
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if kind == "mdy":
                y = int(m.group(3))
                return date(y + 2000 if y < 100 else y, int(m.group(1)), int(m.group(2)))
            if kind == "Mdy":
                return date(int(m.group(3)), MONTHS[m.group(1)[:3].upper()], int(m.group(2)))
            if kind == "dMy":
                return date(int(m.group(3)), MONTHS[m.group(2)[:3].upper()], int(m.group(1)))
        except ValueError:
            continue
    return None


def _field(value, line: TextLine) -> Field_:
    return Field_(value=value, text=line.text, box=line.box)


def parse_lines(lines: list[TextLine]) -> ReceiptFields:
    r = ReceiptFields()
    texts = [l.text.strip() for l in lines]
    # merchant: first line with letters that is not a date, address number line or money line
    for l in lines[:6]:
        t = l.text.strip()
        if re.search(r"[A-Za-z]{3,}", t) and not money_values(t) and not parse_date(t) and not re.match(r"^\d+\s", t):
            r.merchant = _field(t, l)
            break
    for l in lines:
        d = parse_date(l.text)
        if d:
            r.date = _field(d.isoformat(), l)
            break
    subtotal_idx = None
    for i, l in enumerate(lines):
        t, vals = l.text, money_values(l.text)
        if not vals:
            continue
        if SUBTOTAL.search(t):
            r.subtotal, subtotal_idx = _field(vals[-1], l), i
        elif TAX.search(t) and not TOTAL.search(t):
            r.tax = _field(vals[-1], l)
        elif TOTAL.search(t):
            r.total = _field(vals[-1], l)  # last TOTAL line wins (grand total after subtotals)
    end = subtotal_idx if subtotal_idx is not None else next((i for i, t in enumerate(texts) if TOTAL.search(t) or TAX.search(t)), len(lines))
    for l in lines[:end]:
        vals = money_values(l.text)
        if vals and not NOT_ITEM.search(l.text):
            desc = MONEY.sub("", l.text).strip(" .:$-x@")
            if re.search(r"[A-Za-z]", desc):
                r.items.append(LineItem(description=desc[:60], amount=vals[-1], box=l.box))
    return r


def iso_to_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None
