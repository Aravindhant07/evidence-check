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


def merge_rows(regions: list[tuple[str, tuple[int, int, int, int]]]) -> list[TextLine]:
    """Join OCR regions that sit on the same row (e.g. 'SUBTOTAL' and '21.45') into one line, left to right."""
    rows: list[list[tuple[str, tuple]]] = []
    for text, box in sorted(regions, key=lambda r: (r[1][1] + r[1][3]) / 2):
        cy, h = (box[1] + box[3]) / 2, max(1, box[3] - box[1])
        for row in rows:
            rb = row[0][1]
            rcy, rh = (rb[1] + rb[3]) / 2, max(1, rb[3] - rb[1])
            if abs(cy - rcy) <= 0.5 * min(h, rh):
                row.append((text, box))
                break
        else:
            rows.append([(text, box)])
    out = []
    for row in rows:
        row.sort(key=lambda r: r[1][0])
        xs0, ys0, xs1, ys1 = zip(*[b for _, b in row])
        out.append(TextLine(text="  ".join(t.strip() for t, _ in row if t.strip()), box=(min(xs0), min(ys0), max(xs1), max(ys1))))
    return out


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
    def _florence(self, img: Image.Image, task: str, max_new_tokens: int = 1024) -> Any:
        import torch

        model, proc = self._load("ocr_caption")
        inputs = proc(text=task, images=img, return_tensors="pt")
        with torch.inference_mode():
            ids = model.generate(input_ids=inputs["input_ids"], pixel_values=inputs["pixel_values"],
                                 max_new_tokens=max_new_tokens, num_beams=1, do_sample=False)
        text = proc.batch_decode(ids, skip_special_tokens=False)[0]
        out = proc.post_process_generation(text, task=task, image_size=img.size)
        return out.get(task, out)

    def ocr(self, img: Image.Image) -> list[TextLine]:
        res = self._florence(img, "<OCR_WITH_REGION>")
        regions = []
        for quad, label in zip(res.get("quad_boxes", []), res.get("labels", [])):
            xs, ys = quad[0::2], quad[1::2]
            text = label.replace("</s>", "").replace("<s>", "").strip()
            if text:
                regions.append((text, (int(min(xs)), int(min(ys)), int(max(xs)) + 1, int(max(ys)) + 1)))
        return merge_rows(regions)

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
        labels = {i: str(l).lower() for i, l in model.config.id2label.items()}
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
                        ev.extraction.lines = self.ocr(img)
                        ev.extraction.models["ocr"] = self.tag("ocr_caption")
                        if ev.role in ("receipt", "invoice"):
                            ev.extraction.receipt = parse_lines(ev.extraction.lines)
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
