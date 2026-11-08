"""History checks: evidence seen before, in other cases or for other customers."""

from __future__ import annotations

from ..models import Finding, Pointer, Severity
from .base import Context, register


@register
class DuplicateEvidenceCheck:
    id = "duplicate_evidence"
    layer = "history"

    def run(self, ctx: Context) -> list[Finding]:
        if ctx.history is None:
            ctx.skipped.append("duplicate_evidence: no history index configured")
            return []
        tol, out = ctx.policy.tolerances, []
        for ev in ctx.evidence:
            matches = ctx.history.query(ctx.case, ev, tol.duplicate_phash, tol.duplicate_clip)
            seen: set[str] = set()
            for m in sorted(matches, key=lambda m: -m.score):
                if m.entry.case_id in seen:
                    continue
                seen.add(m.entry.case_id)
                other_customer = bool(m.entry.claimant_id and m.entry.claimant_id != ctx.case.claimant_id)
                what = "receipt" if m.kind == "receipt" or ev.role in ("receipt", "invoice") else "photo"
                if other_customer:
                    sev, who = Severity.CRITICAL, "by a different customer"
                else:
                    sev, who = Severity.HIGH, "by the same customer"
                out.append(Finding(
                    check=self.id, layer="history", severity=sev, confidence=round(min(0.98, 0.6 + 0.4 * m.score), 2),
                    title=f"This {what} was already submitted in case {m.entry.case_id}, {who}.",
                    detail=f"{m.detail}; earlier file {m.entry.name}",
                    pointer=Pointer(evidence_id=ev.id, other_case=m.entry.case_id, other_evidence=m.entry.evidence_id)))
        return out


@register
class DeviceRingCheck:
    """The same camera behind photos for several unrelated customers suggests an organised ring."""

    id = "device_ring"
    layer = "history"

    def run(self, ctx: Context) -> list[Finding]:
        if ctx.history is None:
            return []
        from ..history import device_fingerprint

        out, done = [], set()
        for ev in ctx.evidence:
            dev = device_fingerprint(ev)
            if not dev or dev in done:
                continue
            done.add(dev)
            others = ctx.history.device_claimants(dev, ctx.case.case_id) - {ctx.case.claimant_id}
            if len(others) >= 2:
                out.append(Finding(
                    check=self.id, layer="history", severity=Severity.HIGH if len(others) >= 3 else Severity.MEDIUM,
                    confidence=0.7, title=f"The camera that took this photo also took photos for {len(others)} other customers.",
                    detail=f"Device {dev}; other claimants: {', '.join(sorted(others)[:6])}",
                    pointer=Pointer(evidence_id=ev.id)))
        return out
