"""History index: every piece of evidence ever checked, so reuse across cases and customers is visible.

Images are indexed by perceptual hashes (survive resizing and recompression) and, when the image model
is loaded, by embeddings (survive crops and re-photographing). Receipts are fingerprinted by merchant,
total and date; photos by camera serial. Stored as JSON lines; swap in a vector database for large volumes.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import imagehash
import numpy as np
from PIL import Image, ImageOps

from .models import Case, Evidence


def image_hashes(path: str) -> dict[str, str]:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        w, h = im.size
        centre = im.crop((int(w * 0.1), int(h * 0.1), int(w * 0.9), int(h * 0.9)))  # robust to modest crops
        return {"phash": str(imagehash.phash(im)), "phash_centre": str(imagehash.phash(centre)),
                "dhash": str(imagehash.dhash(im))}


def same_scene(path_a: str, path_b: str, min_inliers: int = 25) -> int:
    """Geometric verification: number of keypoint matches consistent with one transform (crop/scale/shift).

    A cropped, resized or recompressed copy keeps many consistent matches; two different photos do not.
    """
    import cv2

    def load(p):
        im = Image.open(p).convert("L")
        im.thumbnail((900, 900))
        return np.asarray(im)

    a, b = load(path_a), load(path_b)
    orb = cv2.ORB_create(nfeatures=2500)
    ka, da = orb.detectAndCompute(a, None)
    kb, db = orb.detectAndCompute(b, None)
    if da is None or db is None or len(ka) < 10 or len(kb) < 10:
        return 0
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da, db, k=2)
    good = [m for m, *rest in (p for p in pairs if len(p) == 2) if rest and m.distance < 0.75 * rest[0].distance]
    if len(good) < 10:
        return 0
    src = np.float32([ka[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([kb[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    _, mask = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=6.0)
    return int(mask.sum()) if mask is not None else 0


def hamming(a: str, b: str) -> int:
    return imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b)


def receipt_fingerprint(ev: Evidence) -> str | None:
    r = ev.extraction.receipt
    if not r or not r.total:
        return None
    merchant = re.sub(r"[^A-Z0-9]", "", str(r.merchant.value).upper())[:12] if r.merchant else ""
    day = r.date.value if r.date else ""
    return f"{merchant}|{r.total.value:.2f}|{day}"


def device_fingerprint(ev: Evidence) -> str | None:
    exif = ev.metadata.get("exif", {})
    serial = exif.get("BodySerialNumber")
    if serial:
        return f"{exif.get('Make', '')}|{exif.get('Model', '')}|{serial}"
    return None


DOCUMENT_ROLES = {"receipt", "invoice", "document"}


def normalised_text(ev: Evidence) -> str:
    return " ".join(" ".join(re.sub(r"[^A-Z0-9.]+", " ", l.text.upper()).split()) for l in ev.extraction.lines)[:3000]


def text_similarity(a: str, b: str) -> float:
    ta, tb = set(a.split()), set(b.split())
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def same_document(a: str, b: str) -> bool:
    """Two receipts from the same merchant share most words (name, address, template); a reused receipt also
    repeats the same numbers (amounts, date, time, card digits). Require both."""
    if text_similarity(a, b) < 0.85:
        return False
    na = " ".join(t for t in a.split() if any(c.isdigit() for c in t))
    nb = " ".join(t for t in b.split() if any(c.isdigit() for c in t))
    return text_similarity(na, nb) >= 0.8


@dataclass
class Entry:
    evidence_id: str
    case_id: str
    claimant_id: str
    name: str
    role: str
    hashes: dict[str, str] = field(default_factory=dict)
    receipt: str | None = None
    device: str | None = None
    text: str = ""  # normalised printed text (documents)
    path: str = ""  # stored evidence file, for geometric verification of candidates
    embedding: list[float] | None = None


@dataclass
class Match:
    entry: Entry
    kind: str  # image | receipt | device
    score: float  # similarity (1.0 = identical) or count
    detail: str
    variant: bool = False  # same document but a different printed date: a re-dated copy, or a coincidence


def _date_of(fingerprint: str | None) -> str:
    return fingerprint.rsplit("|", 1)[-1] if fingerprint else ""


class HistoryIndex:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.entries: list[Entry] = []
        self._emb: np.ndarray | None = None
        self._lock = threading.Lock()
        if self.path and self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    self.entries.append(Entry(**json.loads(line)))

    def _embeddings(self) -> np.ndarray | None:
        if self._emb is None:
            vecs = [e.embedding for e in self.entries if e.embedding]
            self._emb = np.array(vecs, dtype=np.float32) if vecs else None
            self._emb_ids = [i for i, e in enumerate(self.entries) if e.embedding]
        return self._emb

    def entry_for(self, case: Case, ev: Evidence) -> Entry:
        e = Entry(ev.id, case.case_id, case.claimant_id, ev.name, ev.role,
                  receipt=receipt_fingerprint(ev), device=device_fingerprint(ev), embedding=ev.extraction.embedding,
                  text=normalised_text(ev), path=ev.file)
        if ev.kind == "image":
            try:
                e.hashes = image_hashes(ev.file)
            except Exception:
                pass
        return e

    def add_case(self, case: Case, evidence: list[Evidence]) -> None:
        with self._lock:
            known = {(e.case_id, e.evidence_id) for e in self.entries}
            new = [self.entry_for(case, ev) for ev in evidence if (case.case_id, ev.id) not in known]
            self.entries += new
            self._emb = None
            if self.path and new:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a") as fh:
                    for e in new:
                        fh.write(json.dumps(asdict(e)) + "\n")

    def query(self, case: Case, ev: Evidence, phash_max: int = 8, clip_min: float = 0.95) -> list[Match]:
        me = self.entry_for(case, ev)
        out: list[Match] = []
        others = [(i, e) for i, e in enumerate(self.entries) if e.case_id != case.case_id]
        doc = ev.role in DOCUMENT_ROLES
        if me.hashes:
            for _, e in others:
                if not e.hashes:
                    continue
                # Documents all look alike to an image hash (white paper, similar layout): for them an image
                # match only counts when the printed text, numbers included, matches too.
                if doc and not same_document(me.text, e.text):
                    continue
                d = min(hamming(me.hashes["phash"], e.hashes["phash"]),
                        hamming(me.hashes["phash_centre"], e.hashes.get("phash_centre", e.hashes["phash"])),
                        hamming(me.hashes["phash"], e.hashes.get("phash_centre", e.hashes["phash"])),
                        hamming(me.hashes["phash_centre"], e.hashes["phash"]))
                if d <= phash_max:
                    out.append(Match(e, "image", 1 - d / 64, f"perceptual hash distance {d}/64"))
                elif d <= 2.5 * phash_max and not doc and e.path and Path(e.path).exists():
                    # loose candidate (cropped/resized copy?): confirm geometrically before reporting
                    inliers = same_scene(ev.file, e.path)
                    if inliers >= 25:
                        out.append(Match(e, "image", min(0.97, 0.75 + inliers / 400),
                                         f"perceptual hash distance {d}/64, confirmed by {inliers} geometrically consistent keypoints"))
        emb = self._embeddings()
        if me.embedding and emb is not None:
            sims = emb @ np.array(me.embedding, dtype=np.float32)
            for j, s in enumerate(sims):
                e = self.entries[self._emb_ids[j]]
                if doc and not same_document(me.text, e.text):
                    continue
                if e.case_id != case.case_id and s >= clip_min and not any(m.entry is e for m in out):
                    out.append(Match(e, "image", float(s), f"embedding similarity {s:.3f}"))
        if doc:  # same printed text but a different date: report as a variant, not as the same receipt
            for m in out:
                a, b = _date_of(me.receipt), _date_of(m.entry.receipt)
                if a and b and a != b:
                    m.variant = True
                    m.detail += f"; but the printed date differs ({a} here, {b} there)"
        if me.receipt:
            for _, e in others:
                if e.receipt == me.receipt:
                    out.append(Match(e, "receipt", 1.0, f"same merchant, total and date ({me.receipt})"))
        return out

    def snapshot(self) -> "HistoryIndex":
        """An in-memory copy that can be written to without touching the real index (red team, what-if runs)."""
        h = HistoryIndex()
        h.entries = list(self.entries)
        return h

    def device_claimants(self, device: str, exclude_case: str) -> set[str]:
        return {e.claimant_id for e in self.entries if e.device == device and e.case_id != exclude_case}

    def stats(self) -> dict[str, Any]:
        return {"evidence": len(self.entries), "cases": len({e.case_id for e in self.entries}),
                "with_embeddings": sum(1 for e in self.entries if e.embedding)}
