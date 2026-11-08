"""Multimodal model extractors (Hugging Face, run locally). Models only perceive; code decides.

Loaded lazily, pinned to exact revisions from models.yaml, safetensors only, no remote code.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from PIL import Image

from ..models import Case, Evidence, TextLine
from .receipt import parse_lines

ROOT = Path(__file__).resolve().parents[2]
SCREEN_PROMPT = "a photo of a computer or phone screen"
GENERIC_PROMPTS = [SCREEN_PROMPT, "a photo of a store receipt", "a photo of a document", "a photo of a person", "a photo of an undamaged room"]


def merge_rows(regions: list[tuple[str, tuple[int, int, int, int]]], angle: float = 0.0) -> list[TextLine]:
    """Join OCR regions on the same printed row (e.g. 'SUBTOTAL' and '21.45') into one line, left to right.

    `angle` is the text tilt in radians: photos of receipts are rarely straight, and across the width of a
    receipt even 3 degrees moves the amount on the right well above or below its label.
    """
    import math

    ca, sa = math.cos(angle), math.sin(angle)

    def upright(box):  # rotate the box centre into text-aligned coordinates
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        return cx * ca + cy * sa, -cx * sa + cy * ca

    items = [(t, b, *upright(b)) for t, b in regions]
    rows: list[list] = []
    for t, b, ux, uy in sorted(items, key=lambda r: r[3]):
        h = max(1, (b[3] - b[1]) * ca)
        for row in rows:
            if abs(uy - row[0][3]) <= 0.55 * min(h, row[0][4]):
                row.append((t, b, ux, uy, h))
                break
        else:
            rows.append([(t, b, ux, uy, h)])
    out = []
    for row in rows:
        row.sort(key=lambda r: r[2])
        xs0, ys0, xs1, ys1 = zip(*[r[1] for r in row])
        out.append(TextLine(text="  ".join(r[0].strip() for r in row if r[0].strip()), box=(min(xs0), min(ys0), max(xs1), max(ys1))))
    return out


def text_angle(quads: list[list[float]]) -> float:
    """Median baseline angle of OCR quads (x1,y1,x2,y2,x3,y3,x4,y4), weighted toward wide regions."""
    import math

    angles = []
    for q in quads:
        dx, dy = q[2] - q[0], q[3] - q[1]
        if dx > 40:  # short regions give unreliable angles
            angles.append(math.atan2(dy, dx))
    if not angles:
        return 0.0
    angles.sort()
    return angles[len(angles) // 2]


class ModelRegistry:
    name = "hf-local"

    def __init__(self, config: str | Path = ROOT / "models.yaml", device: str = "cpu", jobs: set[str] | None = None):
        self.cfg = yaml.safe_load(Path(config).read_text())
        self.device = device
        self.jobs = jobs  # restrict which models run (e.g. {"ocr_caption", "image_text"})
        self._m: dict[str, Any] = {}
        self._lock = threading.Lock()

    def tag(self, job: str) -> str:
        c = self.cfg[job]
        return f"{c['id']}@{c['revision']}"

    def _load(self, job: str):
        with self._lock:
            if job in self._m:
                return self._m[job]
            import transformers as T
            from huggingface_hub import HfApi

            c = self.cfg[job]
            rev = c["revision"]
            if len(rev) < 40:  # resolve the short pin to the full commit, refusing anything that does not match
                full = HfApi().model_info(c["id"]).sha
                if not full.startswith(rev):
                    raise RuntimeError(f"{c['id']}: pinned revision {rev} is not the current commit {full}; re-pin explicitly")
                rev = full
            kw = {"revision": rev, "use_safetensors": True, "trust_remote_code": False}
            if job == "ocr_caption":
                model = T.Florence2ForConditionalGeneration.from_pretrained(c["id"], **kw).eval()
                proc = T.AutoProcessor.from_pretrained(c["id"], revision=rev, trust_remote_code=False)
                self._m[job] = (model, proc)
            elif job == "image_text":
                model = T.CLIPModel.from_pretrained(c["id"], **kw).eval()
                proc = T.CLIPProcessor.from_pretrained(c["id"], revision=rev)
                self._m[job] = (model, proc)
            elif job == "ai_image":
                model = T.AutoModelForImageClassification.from_pretrained(c["id"], **kw).eval()
                try:
                    proc = T.AutoImageProcessor.from_pretrained(c["id"], revision=rev)
                except ValueError:  # some repos omit image_processor_type; fall back to the architecture's processor
                    proc = T.ViTImageProcessor.from_pretrained(c["id"], revision=rev)
                self._m[job] = (model, proc)
            elif job == "speech":
                self._m[job] = T.pipeline("automatic-speech-recognition", model=c["id"], revision=rev, device=self.device,
                                          model_kwargs={"use_safetensors": True})
            return self._m[job]

    # -- perception primitives ------------------------------------------
    def _florence(self, img: Image.Image, task: str, max_new_tokens: int = 1024, **gen: Any) -> Any:
        import torch

        model, proc = self._load("ocr_caption")
        inputs = proc(text=task, images=img, return_tensors="pt")
        with torch.inference_mode():
            ids = model.generate(input_ids=inputs["input_ids"], pixel_values=inputs["pixel_values"],
                                 max_new_tokens=max_new_tokens, num_beams=1, do_sample=False, **gen)
        text = proc.batch_decode(ids, skip_special_tokens=False)[0]
        out = proc.post_process_generation(text, task=task, image_size=img.size)
        return out.get(task, out)

    def ocr(self, img: Image.Image, offset: tuple[int, int] = (0, 0), **gen: Any) -> list[TextLine]:
        res = self._florence(img, "<OCR_WITH_REGION>", **gen)
        regions, quads = [], []
        for quad, label in zip(res.get("quad_boxes", []), res.get("labels", [])):
            xs, ys = quad[0::2], quad[1::2]
            text = label.replace("</s>", "").replace("<s>", "").strip()
            if text:
                ox, oy = offset
                regions.append((text, (int(min(xs)) + ox, int(min(ys)) + oy, int(max(xs)) + 1 + ox, int(max(ys)) + 1 + oy)))
                quads.append(quad)
        return merge_rows(regions, text_angle(quads))

    def read_receipt(self, img: Image.Image) -> tuple[list[TextLine], str]:
        """OCR with a second look. Florence-2 can fall into a repetition loop on dashed separator lines and run out
        of tokens before the totals; when no total was read, retry with a repetition penalty, then on the bottom half."""
        lines = self.ocr(img)
        if parse_lines(lines).total:
            return lines, "single pass"
        retry = self.ocr(img, repetition_penalty=1.2)
        if parse_lines(retry).total:
            return retry, "retried with repetition penalty"
        w, h = img.size
        bottom = self.ocr(img.crop((0, h // 2, w, h)), offset=(0, h // 2))
        merged = [l for l in retry if l.box and l.box[3] <= h // 2] + bottom
        if parse_lines(merged).total:
            return merged, "retried on the bottom half"
        return lines, "total not found after retries"

    def caption(self, img: Image.Image) -> str:
        res = self._florence(img, "<MORE_DETAILED_CAPTION>", max_new_tokens=120)
        return res if isinstance(res, str) else str(res)

    def clip(self, img: Image.Image, prompts: list[str]) -> tuple[dict[str, float], list[float]]:
        import torch

        model, proc = self._load("image_text")
        inputs = proc(text=prompts, images=img, return_tensors="pt", padding=True)
        with torch.inference_mode():
            out = model(**inputs)
        probs = out.logits_per_image.softmax(dim=1)[0].tolist()
        emb = out.image_embeds[0]
        emb = (emb / emb.norm()).tolist()
        return dict(zip(prompts, probs)), emb

    def embed(self, img: Image.Image) -> list[float]:
        import torch

        model, proc = self._load("image_text")
        with torch.inference_mode():
            feats = model.get_image_features(**proc(images=img, return_tensors="pt"))
        e = feats[0] if not hasattr(feats, "pooler_output") else feats.pooler_output[0]
        return (e / e.norm()).tolist()

    def ai_score(self, img: Image.Image) -> float:
        import torch

        model, proc = self._load("ai_image")
        with torch.inference_mode():
            logits = model(**proc(images=img.convert("RGB"), return_tensors="pt")).logits[0]
        probs = logits.softmax(-1).tolist()
        labels = {int(i): str(l).lower() for i, l in model.config.id2label.items()}
        real = [i for i, l in labels.items() if l in ("real", "human", "authentic", "natural")]
        if real:  # e.g. NYUAD: {dalle, real, sd} -> P(generated) = 1 - P(real)
            return float(1.0 - sum(probs[i] for i in real))
        ai_idx = [i for i, l in labels.items() if any(k in l for k in ("ai", "fake", "generated", "synthetic", "artificial"))]
        return float(sum(probs[i] for i in ai_idx)) if ai_idx else float("nan")

    def transcribe(self, path: str) -> str:
        asr = self._load("speech")
        return str(asr(path, generate_kwargs={"language": "en", "task": "transcribe"}).get("text", "")).strip()

    # -- per-case extraction ------------------------------------------------
    def _want(self, job: str) -> bool:
        return self.jobs is None or job in self.jobs

    def extract_all(self, evidence: list[Evidence], policy=None, case: Case | None = None) -> list[str]:
        notes: list[str] = []
        prompts = list(GENERIC_PROMPTS)
        if policy:
            for ps in policy.loss_labels.values():
                prompts += [p for p in ps if p not in prompts]
        for ev in evidence:
            try:
                if ev.kind == "image":
                    img = Image.open(ev.file).convert("RGB")
                    if ev.role in ("receipt", "invoice", "document") and self._want("ocr_caption"):
                        if ev.role in ("receipt", "invoice"):
                            ev.extraction.lines, how = self.read_receipt(img)
                            ev.extraction.receipt = parse_lines(ev.extraction.lines)
                            if how != "single pass":
                                notes.append(f"{ev.name}: OCR {how}")
                        else:
                            ev.extraction.lines = self.ocr(img)
                        ev.extraction.models["ocr"] = self.tag("ocr_caption")
                    if ev.role in ("damage_photo", "product_photo") and self._want("ocr_caption"):
                        ev.extraction.caption = self.caption(img)
                        ev.extraction.models["caption"] = self.tag("ocr_caption")
                    if self._want("image_text"):
                        if ev.role in ("damage_photo", "product_photo"):
                            ev.extraction.labels, ev.extraction.embedding = self.clip(img, prompts)
                        else:
                            ev.extraction.embedding = self.embed(img)
                        ev.extraction.models["image_text"] = self.tag("image_text")
                    if self._want("ai_image"):
                        s = self.ai_score(img)
                        ev.extraction.ai_generated_score = None if s != s else round(s, 4)
                        ev.extraction.models["ai_image"] = self.tag("ai_image")
                elif ev.kind == "pdf":
                    text = ev.metadata.get("text", "")
                    if text.strip():
                        ev.extraction.lines = [TextLine(text=t) for t in text.splitlines() if t.strip()]
                        if ev.role in ("receipt", "invoice"):
                            ev.extraction.receipt = parse_lines(ev.extraction.lines)
                    else:
                        notes.append(f"{ev.name}: scanned PDF without a text layer; OCR of PDF pages is not supported yet")
                elif ev.kind == "audio" and self._want("speech"):
                    ev.extraction.transcript = self.transcribe(ev.file)
                    ev.extraction.models["speech"] = self.tag("speech")
            except Exception as e:
                notes.append(f"{ev.name}: extraction failed ({type(e).__name__}: {str(e)[:120]})")
        return notes
