"""Case-level checks: does the evidence agree with itself and with the case?"""

from __future__ import annotations

import math
import re
from datetime import date, datetime

from ..extract.receipt import iso_to_date
from ..models import Evidence, Finding, Pointer, Severity
from .base import Context, register

RECEIPT_ROLES = ("receipt", "invoice")
PHOTO_ROLES = ("damage_photo", "product_photo")


def _receipts(ctx: Context) -> list[Evidence]:
    return [e for e in ctx.of_role(*RECEIPT_ROLES) if e.extraction.receipt and e.extraction.receipt.total]


def _usd(x: float) -> str:
    return f"${x:,.2f}"


@register
class ReceiptMathCheck:
    id = "receipt_math"
    layer = "case"

    def run(self, ctx: Context) -> list[Finding]:
        out = []
        for ev in _receipts(ctx):
            r = ev.extraction.receipt
            total = r.total.value
            items = round(sum(i.amount for i in r.items), 2) if r.items else None
            sub = r.subtotal.value if r.subtotal else items
            tax = r.tax.value if r.tax else 0.0
            if r.subtotal and items is not None and abs(items - r.subtotal.value) > 0.02:
                # medium: a missed or misread item line produces the same symptom
                out.append(Finding(check=self.id, layer="case", severity=Severity.MEDIUM, confidence=0.6,
                                   title=f"The line items add up to {_usd(items)}, but the receipt's subtotal says {_usd(r.subtotal.value)}.",
                                   detail=f"{len(r.items)} items parsed.", pointer=Pointer(evidence_id=ev.id, box=r.subtotal.box, text=r.subtotal.text)))
            if sub is not None and abs(sub + tax - total) > 0.02:
                out.append(Finding(check=self.id, layer="case", severity=Severity.HIGH, confidence=0.85,
                                   title=f"The receipt doesn't add up: {_usd(sub)} + {_usd(tax)} tax = {_usd(sub + tax)}, but the total says {_usd(total)}.",
                                   detail="Totals edited after printing, or generated receipts, often fail this arithmetic.",
                                   pointer=Pointer(evidence_id=ev.id, box=r.total.box, text=r.total.text)))
            rates = ctx.policy.tax_rates.get(ctx.case.jurisdiction or "", [])
            if rates and r.tax and sub and sub > 0:
                rate = tax / sub
                if not any(abs(rate - a) <= 0.0015 + 0.005 / sub for a in rates):
                    out.append(Finding(check="tax_rate", layer="case", severity=Severity.HIGH, confidence=0.75,
                                       title=f"The tax rate on this receipt ({rate:.2%}) is not a valid rate in {ctx.case.jurisdiction}.",
                                       detail=f"Valid rates: {', '.join(f'{x:.3%}' for x in rates)}",
                                       pointer=Pointer(evidence_id=ev.id, box=r.tax.box, text=r.tax.text)))
        return out


@register
class AmountCheck:
    id = "amount_match"
    layer = "case"

    def run(self, ctx: Context) -> list[Finding]:
        recs = _receipts(ctx)
        if ctx.case.claimed_amount is None or not recs:
            return []
        evidence_total = round(sum(e.extraction.receipt.total.value for e in recs), 2)
        claimed = ctx.case.claimed_amount
        tol = max(ctx.policy.tolerances.amount_abs, ctx.policy.tolerances.amount_pct * evidence_total)
        if claimed - evidence_total > tol:
            return [Finding(check=self.id, layer="case", severity=Severity.HIGH, confidence=0.9,
                            title=f"The claim is for {_usd(claimed)}, but the receipts only add up to {_usd(evidence_total)}.",
                            detail=f"Difference {_usd(claimed - evidence_total)} across {len(recs)} receipt(s).",
                            pointer=Pointer(case_field="claimed_amount"))]
        return []


def _photo_time(ev: Evidence) -> datetime | None:
    t = ev.metadata.get("capture_time")
    return datetime.fromisoformat(t) if t else None


@register
class DateCheck:
    id = "dates"
    layer = "case"

    def run(self, ctx: Context) -> list[Finding]:
        c, tol, out = ctx.case, ctx.policy.tolerances, []
        claims = ctx.policy.name.startswith("insurance")
        if claims and c.incident_date and c.policy.start and c.incident_date < c.policy.start:
            out.append(Finding(check=self.id, layer="case", severity=Severity.HIGH, confidence=0.95,
                               title="The incident date is before the policy started.",
                               detail=f"incident {c.incident_date}, policy start {c.policy.start}", pointer=Pointer(case_field="incident_date")))
        if claims and c.incident_date and c.policy.end and c.incident_date > c.policy.end:
            out.append(Finding(check=self.id, layer="case", severity=Severity.HIGH, confidence=0.95,
                               title="The incident date is after the policy ended.",
                               detail=f"incident {c.incident_date}, policy end {c.policy.end}", pointer=Pointer(case_field="incident_date")))
        if c.incident_date and c.filed_date and (c.filed_date - c.incident_date).days > tol.max_filing_delay_days:
            out.append(Finding(check=self.id, layer="case", severity=Severity.LOW, confidence=0.7,
                               title=f"The claim was filed {(c.filed_date - c.incident_date).days} days after the incident.",
                               pointer=Pointer(case_field="filed_date")))
        for ev in ctx.of_role(*PHOTO_ROLES):
            t = _photo_time(ev)
            if not t or not c.incident_date:
                continue
            d = t.date()
            if claims and c.policy.start and d < c.policy.start:
                out.append(Finding(check=self.id, layer="case", severity=Severity.CRITICAL, confidence=0.9,
                                   title=f"This photo was taken on {d}, before the policy started ({c.policy.start}).",
                                   detail="Pre-existing damage is a classic claims fraud pattern.", pointer=Pointer(evidence_id=ev.id)))
            elif (c.incident_date - d).days > 1:
                out.append(Finding(check=self.id, layer="case", severity=Severity.HIGH, confidence=0.85,
                                   title=f"This photo was taken on {d}, before the incident date ({c.incident_date}).",
                                   pointer=Pointer(evidence_id=ev.id)))
            if c.filed_date and (d - c.filed_date).days > 1:
                out.append(Finding(check=self.id, layer="case", severity=Severity.LOW, confidence=0.6,
                                   title=f"This photo was taken on {d}, after the claim was filed.", pointer=Pointer(evidence_id=ev.id)))
        for ev in _receipts(ctx):
            r = ev.extraction.receipt
            d = iso_to_date(r.date.value) if r.date else None
            if not d:
                continue
            if not claims and c.filed_date and d > c.filed_date:  # repair invoices legitimately follow a claim
                out.append(Finding(check=self.id, layer="case", severity=Severity.HIGH, confidence=0.9,
                                   title=f"The receipt is dated {d}, after the claim was filed ({c.filed_date}).",
                                   pointer=Pointer(evidence_id=ev.id, box=r.date.box, text=r.date.text)))
            if not claims and c.incident_date and abs((d - c.incident_date).days) > 1:
                out.append(Finding(check=self.id, layer="case", severity=Severity.HIGH, confidence=0.8,
                                   title=f"The receipt is dated {d}, but the expense was claimed for {c.incident_date}.",
                                   pointer=Pointer(evidence_id=ev.id, box=r.date.box, text=r.date.text)))
            if claims and c.incident_date and d < c.incident_date:
                out.append(Finding(check=self.id, layer="case", severity=Severity.MEDIUM, confidence=0.75,
                                   title=f"The receipt is dated {d}, before the incident ({c.incident_date}).",
                                   pointer=Pointer(evidence_id=ev.id, box=r.date.box, text=r.date.text)))
        return out


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) > 2 and t not in {"the", "inc", "llc", "store", "co"}}


@register
class CardMatchCheck:
    id = "card_match"
    layer = "case"

    def run(self, ctx: Context) -> list[Finding]:
        txns = ctx.case.card_transactions
        recs = _receipts(ctx)
        if not txns or not recs:
            return []
        tol, out = ctx.policy.tolerances, []
        for ev in recs:
            r = ev.extraction.receipt
            total = r.total.value
            d = iso_to_date(r.date.value) if r.date else None
            cands = [t for t in txns if abs(t.amount - total) <= tol.card_amount_abs and (d is None or abs((t.date - d).days) <= tol.card_days)]
            if not cands:
                out.append(Finding(check=self.id, layer="case", severity=Severity.HIGH, confidence=0.85,
                                   title=f"No card transaction matches this receipt ({_usd(total)}{' on ' + str(d) if d else ''}).",
                                   detail="Generated or altered receipts have no matching payment record.",
                                   pointer=Pointer(evidence_id=ev.id, box=r.total.box, text=r.total.text)))
                continue
            merchant = r.merchant.value if r.merchant else ""
            if merchant and not any(_tokens(merchant) & _tokens(t.merchant) for t in cands):
                out.append(Finding(check=self.id, layer="case", severity=Severity.LOW, confidence=0.6,
                                   title=f"A card payment of {_usd(total)} exists, but from a different merchant ({cands[0].merchant}).",
                                   detail=f"Receipt merchant: {merchant}", pointer=Pointer(evidence_id=ev.id, box=r.merchant.box if r.merchant else None)))
        return out


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


@register
class GpsCheck:
    id = "gps_location"
    layer = "case"

    def run(self, ctx: Context) -> list[Finding]:
        loc = ctx.case.location
        if loc.lat is None or loc.lon is None:
            return []
        out = []
        for ev in ctx.of_role(*PHOTO_ROLES):
            g = ev.metadata.get("gps")
            if not g:
                continue
            km = haversine_km((loc.lat, loc.lon), (g["lat"], g["lon"]))
            if km > ctx.policy.tolerances.gps_km:
                out.append(Finding(check=self.id, layer="case", severity=Severity.HIGH, confidence=0.8,
                                   title=f"This photo was taken {km:,.0f} km from the incident location.",
                                   detail=f"Photo GPS {g['lat']:.4f},{g['lon']:.4f}; incident {loc.lat:.4f},{loc.lon:.4f}",
                                   pointer=Pointer(evidence_id=ev.id)))
        return out


@register
class PhotoVsClaimCheck:
    id = "photo_vs_claim"
    layer = "case"

    def run(self, ctx: Context) -> list[Finding]:
        claimed = ctx.case.loss_type
        labels_cfg = ctx.policy.loss_labels
        if not claimed or claimed not in labels_cfg:
            return []
        out = []
        for ev in ctx.of_role("damage_photo"):
            probs = ev.extraction.labels
            if not probs:
                continue
            by_type = {t: max((probs.get(p, 0.0) for p in prompts), default=0.0) for t, prompts in labels_cfg.items()}
            screen = probs.get("a photo of a computer or phone screen", 0.0)
            if screen >= 0.5:
                out.append(Finding(check=self.id, layer="case", severity=Severity.MEDIUM, confidence=round(screen, 2),
                                   title="This looks like a photo of a screen, not of the damage itself.",
                                   detail=f"screen label {screen:.2f}", pointer=Pointer(evidence_id=ev.id)))
            best = max(by_type, key=by_type.get)
            if best != claimed and by_type[best] >= 0.4 and by_type[best] - by_type.get(claimed, 0.0) >= 0.25:
                out.append(Finding(check=self.id, layer="case", severity=Severity.HIGH, confidence=round(by_type[best], 2),
                                   title=f"This photo looks like {best.replace('_', ' ')}, but the claim is for {claimed.replace('_', ' ')}.",
                                   detail=f"Label scores: " + ", ".join(f"{k} {v:.2f}" for k, v in sorted(by_type.items(), key=lambda kv: -kv[1])[:4]),
                                   pointer=Pointer(evidence_id=ev.id)))
        return out
