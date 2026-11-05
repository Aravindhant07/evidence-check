"""Item-level checks: is each file what it claims to be?"""

from __future__ import annotations

import re
from datetime import datetime

from ..models import Finding, Pointer, Severity
from .base import Context, register

EDITORS = re.compile(r"photoshop|gimp|snapseed|lightroom|pixelmator|canva|picsart|affinity|paint\.net|fotor|facetune|"
                     r"photopea|ilovepdf|smallpdf|sejda|pdfescape|pdf-xchange editor|foxit phantom|acrobat pro", re.I)
GENERATORS = re.compile(r"stable[\s-]?diffusion|comfyui|automatic1111|midjourney|dall[\s·-]?e|firefly|ideogram|"
                        r"leonardo|flux|sdxl|novelai|invokeai|chatgpt|gpt-image|imagen|gemini|grok|recraft|playground", re.I)
AI_SOURCES = {"trainedAlgorithmicMedia", "compositeWithTrainedAlgorithmicMedia", "algorithmicMedia"}
PHOTO_ROLES = {"receipt", "invoice", "damage_photo", "product_photo", "document"}


@register
class MetadataCheck:
    id = "metadata"
    layer = "item"

    def run(self, ctx: Context) -> list[Finding]:
        out = []
        for ev in ctx.evidence:
            if ev.kind != "image":
                continue
            md = ev.metadata
            exif = md.get("exif", {})
            tools = " ".join([exif.get("Software", ""), *md.get("xmp_tools", [])])
            m = EDITORS.search(tools)
            if m:
                sev = Severity.HIGH if ev.role in PHOTO_ROLES else Severity.MEDIUM
                out.append(Finding(check=self.id, layer="item", severity=sev, confidence=0.8,
                                   title=f"This image was saved by editing software ({m.group(0)}).",
                                   detail=f"Software/XMP tools: {tools.strip()[:200]}",
                                   pointer=Pointer(evidence_id=ev.id, text=tools.strip()[:120])))
            if ev.role in PHOTO_ROLES and not exif.get("Make") and not exif.get("Model") and not md.get("capture_time"):
                out.append(Finding(check=self.id, layer="item", severity=Severity.LOW, confidence=0.6,
                                   title="No camera information: this may not be an original phone photo.",
                                   detail="Screenshots, downloaded images, edited exports and generated images usually lack camera EXIF.",
                                   pointer=Pointer(evidence_id=ev.id)))
            ct, mt = md.get("capture_time"), md.get("modify_time")
            if ct and mt:
                gap = (datetime.fromisoformat(mt) - datetime.fromisoformat(ct)).days
                if gap > 1:
                    out.append(Finding(check=self.id, layer="item", severity=Severity.LOW, confidence=0.6,
                                       title=f"The image was modified {gap} days after it was taken.",
                                       detail=f"DateTimeOriginal {ct}, DateTime {mt}", pointer=Pointer(evidence_id=ev.id)))
        return out


@register
class ContentCredentialsCheck:
    id = "content_credentials"
    layer = "item"

    def run(self, ctx: Context) -> list[Finding]:
        out = []
        for ev in ctx.evidence:
            c = ev.metadata.get("c2pa")
            if not c:
                continue
            ai = AI_SOURCES & set(c.get("digital_source_types", []))
            if ai:
                out.append(Finding(check=self.id, layer="item", severity=Severity.CRITICAL, confidence=0.95,
                                   title=f"Content credentials say this image was made with AI ({c.get('generator') or 'unknown tool'}).",
                                   detail=f"C2PA digitalSourceType: {', '.join(sorted(ai))}; actions: {c.get('actions')}",
                                   pointer=Pointer(evidence_id=ev.id)))
            # Integrity failures (hash mismatches) mean the pixels changed after signing. Trust failures
            # (unknown or self-signed certificate) are common and say nothing about tampering.
            errs = [e for e in c.get("validation_errors", []) if e and re.search(r"mismatch|hash|tamper|invalid", e, re.I)
                    and not re.search(r"untrusted|trust|certificate|credential", e, re.I)]
            if errs:
                out.append(Finding(check=self.id, layer="item", severity=Severity.HIGH, confidence=0.85,
                                   title="The image's content credentials no longer match it: it was changed after signing.",
                                   detail=f"C2PA validation: {', '.join(errs)[:200]}", pointer=Pointer(evidence_id=ev.id)))
            elif "c2pa.edited" in (c.get("actions") or []) and not ai:
                out.append(Finding(check=self.id, layer="item", severity=Severity.LOW, confidence=0.7,
                                   title="Content credentials record that this image was edited.",
                                   detail=f"C2PA actions: {c.get('actions')}", pointer=Pointer(evidence_id=ev.id)))
        return out


@register
class GeneratorTraceCheck:
    id = "generator_traces"
    layer = "item"

    def run(self, ctx: Context) -> list[Finding]:
        out = []
        for ev in ctx.evidence:
            if ev.kind != "image":
                continue
            md = ev.metadata
            hay = " ".join([*(f"{k}={v}" for k, v in md.get("info", {}).items()), *md.get("exif", {}).values(), *md.get("xmp_tools", [])])
            keys = {k.lower() for k in md.get("info", {})}
            m = GENERATORS.search(hay)
            if m or keys & {"parameters", "prompt", "workflow", "negative_prompt"}:
                what = m.group(0) if m else "generation parameters"
                out.append(Finding(check=self.id, layer="item", severity=Severity.CRITICAL, confidence=0.9,
                                   title=f"The file carries traces of an AI image generator ({what}).",
                                   detail=hay[:300], pointer=Pointer(evidence_id=ev.id, text=what)))
            score = ev.extraction.ai_generated_score
            if score is not None and score >= ctx.policy.tolerances.ai_score:
                out.append(Finding(check="ai_image_model", layer="item", severity=Severity.MEDIUM, confidence=round(score, 2),
                                   title="An AI-image detector rates this image as likely generated.",
                                   detail=f"Detector score {score:.2f} (one weak signal; detectors are easy to evade)",
                                   pointer=Pointer(evidence_id=ev.id)))
        return out


@register
class PdfStructureCheck:
    id = "pdf_structure"
    layer = "item"

    def run(self, ctx: Context) -> list[Finding]:
        out = []
        for ev in ctx.evidence:
            if ev.kind != "pdf":
                continue
            md = ev.metadata
            if md.get("has_javascript"):
                out.append(Finding(check=self.id, layer="item", severity=Severity.HIGH, confidence=0.9,
                                   title="This PDF contains scripts, which genuine receipts and invoices do not need.",
                                   detail="/JS or /JavaScript present (never executed by evidence-check)", pointer=Pointer(evidence_id=ev.id)))
            if md.get("eof_markers", 1) > 1:
                out.append(Finding(check=self.id, layer="item", severity=Severity.MEDIUM, confidence=0.7,
                                   title=f"This PDF was changed after it was first created ({md['eof_markers'] - 1} later save(s)).",
                                   detail="Multiple %%EOF markers indicate incremental updates.", pointer=Pointer(evidence_id=ev.id)))
            tool = f"{md.get('producer', '')} {md.get('creator', '')}"
            m = EDITORS.search(tool)
            if m:
                out.append(Finding(check=self.id, layer="item", severity=Severity.MEDIUM, confidence=0.7,
                                   title=f"This PDF was produced by an editing tool ({m.group(0)}), not a billing system.",
                                   detail=f"Producer/Creator: {tool.strip()[:200]}", pointer=Pointer(evidence_id=ev.id, text=tool.strip()[:120])))
        return out
