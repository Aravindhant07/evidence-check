"""Image forensics: error-level analysis and copy-move detection, with visual evidence."""

from __future__ import annotations

import io
import re
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from ..models import Evidence, Finding, Pointer, Severity
from .base import Context, register

BLOCK = 16


def _load_rgb(path: str, max_side: int = 1600) -> tuple[np.ndarray, float]:
    im = Image.open(path).convert("RGB")
    scale = min(1.0, max_side / max(im.size))
    if scale < 1.0:
        im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
    return np.asarray(im), scale


def ela_map(rgb: np.ndarray, quality: int = 90) -> np.ndarray:
    """Per-pixel error level: how much each pixel changes when the image is saved again as JPEG."""
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, "JPEG", quality=quality)
    again = np.asarray(Image.open(io.BytesIO(buf.getvalue())).convert("RGB"))
    return np.abs(rgb.astype(np.int16) - again.astype(np.int16)).max(axis=2).astype(np.float32)


def suspicious_regions(rgb: np.ndarray, ela: np.ndarray, ratio: float) -> tuple[list[tuple[int, int, int, int, float]], np.ndarray]:
    """Blocks whose error level is far above that of comparable (non-flat) content elsewhere in the image."""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    h, w = gray.shape
    bh, bw = h // BLOCK, w // BLOCK
    if bh < 4 or bw < 4:
        return [], np.zeros((1, 1))
    g = gray[: bh * BLOCK, : bw * BLOCK].reshape(bh, BLOCK, bw, BLOCK)
    e = ela[: bh * BLOCK, : bw * BLOCK].reshape(bh, BLOCK, bw, BLOCK)
    texture = g.std(axis=(1, 3))
    level = e.mean(axis=(1, 3))
    content = texture > 8.0  # ignore flat paper/background blocks
    if content.sum() < 8:
        return [], level
    # Compare each block with blocks of similar texture, so dense text is not flagged just for being dense.
    rel = np.zeros_like(level)
    bins = np.quantile(texture[content], [0, 0.25, 0.5, 0.75, 1.0])
    for lo, hi in zip(bins[:-1], bins[1:]):
        sel = content & (texture >= lo) & (texture <= hi)
        if sel.sum() >= 4:
            rel[sel] = level[sel] / (np.median(level[sel]) + 0.5)
    flagged = (rel >= ratio).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(flagged, connectivity=8)
    regions = []
    total = bh * bw
    for i in range(1, n):
        x, y, ww, hh, area = stats[i]
        if area < 2 or area > 0.2 * total:  # isolated noise, or a global property of the image
            continue
        strength = float(rel[labels == i].mean())
        regions.append((x * BLOCK, y * BLOCK, (x + ww) * BLOCK, (y + hh) * BLOCK, strength))
    return regions, rel


def save_heatmap(rgb: np.ndarray, rel: np.ndarray, path: Path) -> str:
    h, w = rgb.shape[:2]
    m = np.clip(rel / max(3.0, float(rel.max())), 0, 1)
    m = cv2.resize((m * 255).astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST)
    color = cv2.applyColorMap(m, cv2.COLORMAP_JET)[:, :, ::-1]
    blend = (0.55 * rgb + 0.45 * color).astype(np.uint8)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(blend).save(path)
    return str(path)


def _box_to_original(box, scale):
    return tuple(int(round(v / scale)) for v in box)


@register
class ErrorLevelCheck:
    id = "error_level"
    layer = "item"
    roles = {"receipt", "invoice", "damage_photo", "product_photo", "document"}

    def run(self, ctx: Context) -> list[Finding]:
        out = []
        for ev in ctx.evidence:
            if ev.kind != "image" or ev.role not in self.roles:
                continue
            rgb, scale = _load_rgb(ev.file)
            ela = ela_map(rgb)
            regions, rel = suspicious_regions(rgb, ela, ctx.policy.tolerances.ela_ratio)
            # A local edit shows up as one or a few coherent regions; many scattered regions are the
            # natural variation of real-world texture and edges, not evidence of editing.
            if not regions or len(regions) > 3:
                continue
            regions.sort(key=lambda r: -r[4])
            x0, y0, x1, y1, strength = regions[0]
            heat = save_heatmap(rgb, rel, ctx.work_dir / f"ela_{ev.id}.png")
            # Supporting signal only (LOW). On the benchmark (460 cases) ELA fired on 13 images and none was a planted
            # edit: phone JPEGs vary too much. It adds weight next to other findings and gives reviewers a heatmap.
            out.append(Finding(
                check=self.id, layer="item", severity=Severity.LOW, confidence=min(0.9, 0.4 + 0.08 * strength),
                title="Part of this image may have been saved differently from the rest (supporting signal only).",
                detail=f"Error-level analysis: {len(regions)} region(s); strongest {strength:.1f}x the level of similar content.",
                pointer=Pointer(evidence_id=ev.id, box=_box_to_original((x0, y0, x1, y1), scale), heatmap=heat),
                data={"regions": [[*_box_to_original(r[:4], scale), round(r[4], 2)] for r in regions[:5]]}))
        return out


def copy_move(rgb: np.ndarray, min_matches: int = 12, min_shift: int = 40) -> tuple[tuple, tuple, int] | None:
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    orb = cv2.ORB_create(nfeatures=3000)
    kps, des = orb.detectAndCompute(gray, None)
    if des is None or len(kps) < 20:
        return None
    matches = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(des, des, k=3)
    shifts: dict[tuple[int, int], list[tuple]] = {}
    for ms in matches:
        for m in ms[1:]:  # ms[0] is the keypoint matched with itself
            if m.distance > 32:
                continue
            p, q = np.array(kps[m.queryIdx].pt), np.array(kps[m.trainIdx].pt)
            d = q - p
            if np.hypot(*d) < min_shift:
                continue
            if d[0] < 0 or (d[0] == 0 and d[1] < 0):  # count each pair once, in a canonical direction
                p, q, d = q, p, -d
            key = (int(round(d[0] / 8)), int(round(d[1] / 8)))
            shifts.setdefault(key, []).append((p, q))
            break
    if not shifts:
        return None
    key, pairs = max(shifts.items(), key=lambda kv: len(kv[1]))
    if len(pairs) < min_matches:
        return None
    src = np.array([p for p, _ in pairs])
    dst = np.array([q for _, q in pairs])
    def bbox(a):
        return (int(a[:, 0].min()), int(a[:, 1].min()), int(a[:, 0].max()), int(a[:, 1].max()))
    return bbox(src), bbox(dst), len(pairs)


def copy_move_blocks(rgb: np.ndarray, side: int = 256, block: int = 8, min_blocks: int = 25, min_shift: int = 32):
    """Block matching at every pixel position (on a downscaled copy): many textured blocks repeated at one
    displacement means a cloned region. Complements keypoints, which are sparse in smooth areas."""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    scale = min(1.0, side / max(gray.shape))
    g = cv2.resize(gray, (round(gray.shape[1] * scale), round(gray.shape[0] * scale)), interpolation=cv2.INTER_AREA).astype(np.float32)
    h, w = g.shape
    # all blocks at stride 1, as rows of a matrix
    win = np.lib.stride_tricks.sliding_window_view(g, (block, block))[:, :, :, :]
    ys, xs = np.mgrid[0:win.shape[0], 0:win.shape[1]]
    blocks = win.reshape(-1, block * block)
    std = blocks.std(axis=1)
    keep = std > 4
    blocks, ys, xs = blocks[keep], ys.reshape(-1)[keep], xs.reshape(-1)[keep]
    if len(blocks) < 50:
        return None
    feats = np.round((blocks - blocks.mean(axis=1, keepdims=True)) / 6).astype(np.int16)  # coarse, brightness-invariant
    order = np.lexsort(feats.T[::-1])
    f_sorted = feats[order]
    same = np.all(f_sorted[1:] == f_sorted[:-1], axis=1)
    idx = np.nonzero(same)[0]
    if len(idx) == 0:
        return None
    a, b = order[idx], order[idx + 1]
    dx, dy = xs[b] - xs[a], ys[b] - ys[a]
    flip = (dx < 0) | ((dx == 0) & (dy < 0))
    dx, dy = np.where(flip, -dx, dx), np.where(flip, -dy, dy)
    far = np.hypot(dx, dy) / scale >= min_shift
    if not far.any():
        return None
    dx, dy, a, b = dx[far], dy[far], a[far], b[far]
    keys, counts = np.unique(np.stack([dx, dy], 1), axis=0, return_counts=True)
    k = int(np.argmax(counts))
    if counts[k] < min_blocks:
        return None
    sel = (dx == keys[k][0]) & (dy == keys[k][1])
    src = np.stack([xs[a[sel]], ys[a[sel]]], 1) / scale
    dst = np.stack([xs[b[sel]], ys[b[sel]]], 1) / scale
    flipped = flip[far][sel]
    src, dst = np.where(flipped[:, None], dst, src), np.where(flipped[:, None], src, dst)
    bs = block / scale
    def bbox(p):
        return (int(p[:, 0].min()), int(p[:, 1].min()), int(p[:, 0].max() + bs), int(p[:, 1].max() + bs))
    return bbox(src), bbox(dst), int(counts[k])


@register
class CopyMoveCheck:
    id = "copy_move"
    layer = "item"
    roles = {"damage_photo", "product_photo"}  # not receipts: repeated glyphs in text rows look like copy-move

    def run(self, ctx: Context) -> list[Finding]:
        out = []
        for ev in ctx.evidence:
            if ev.kind != "image" or ev.role not in self.roles:
                continue
            rgb, scale = _load_rgb(ev.file, max_side=1200)
            res = copy_move(rgb) or copy_move_blocks(rgb)
            if not res:
                continue
            src, dst, n = res
            out.append(Finding(
                check=self.id, layer="item", severity=Severity.HIGH, confidence=min(0.9, 0.5 + n / 100),
                title="Part of this photo appears to be copied and pasted elsewhere in the same photo.",
                detail=f"{n} matching keypoint pairs share the same displacement.",
                pointer=Pointer(evidence_id=ev.id, box=_box_to_original(dst, scale)),
                data={"source_box": list(_box_to_original(src, scale)), "matches": n}))
        return out


def line_sharpness(gray: np.ndarray, box) -> float | None:
    """Edge sharpness of a printed line, normalised by its contrast."""
    x0, y0, x1, y1 = (int(v) for v in box)
    h, w = gray.shape
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    if x1 - x0 < 8 or y1 - y0 < 6:
        return None
    patch = gray[y0:y1, x0:x1]
    contrast = float(np.percentile(patch, 95) - np.percentile(patch, 5))
    if contrast < 40:
        return None
    lap = np.abs(cv2.Laplacian(patch, cv2.CV_32F))
    return float(np.percentile(lap, 99.5)) / contrast


@register
class TextConsistencyCheck:
    """A value typed in digitally after the photo was taken is sharper (and cleaner) than the printed text around it."""

    id = "text_consistency"
    layer = "item"
    # Experimental: on the synthetic benchmark the sharpness signal was not separable from normal line-to-line
    # variation (short values score high; re-saving smooths typed-in text). Off unless a policy enables it.
    default_enabled = False

    def run(self, ctx: Context) -> list[Finding]:
        out = []
        for ev in ctx.evidence:
            lines = [l for l in ev.extraction.lines if l.box]
            if ev.kind != "image" or ev.role not in ("receipt", "invoice", "document") or len(lines) < 4:
                continue
            gray = np.asarray(Image.open(ev.file).convert("L")).astype(np.float32)
            scored = []
            for l in lines:
                # Compare like with like: only printed amounts (where edits happen), value half of the line.
                if not re.search(r"\d[\d,]*\.\d{2}\s*$", l.text):
                    continue
                b = l.box
                b = (b[0] + (b[2] - b[0]) // 2, b[1], b[2], b[3])
                v = line_sharpness(gray, b)
                if v is not None:
                    scored.append((v, l, b))
            if len(scored) < 4:
                continue
            vals = np.array([v for v, _, _ in scored])
            med = float(np.median(vals))
            mad = float(np.median(np.abs(vals - med))) + 1e-6
            for v, l, b in scored:
                if v / med >= 1.5 and (v - med) / mad >= 4:
                    money = bool(re.search(r"\d\.\d{2}", l.text))
                    out.append(Finding(
                        check=self.id, layer="item", severity=Severity.HIGH if money else Severity.MEDIUM,
                        confidence=min(0.9, 0.5 + 0.1 * (v / med)),
                        title=f"\"{l.text.strip()[:40]}\" is printed noticeably sharper than the rest of the document; it may have been typed in digitally.",
                        detail=f"Edge sharpness {v / med:.1f}x the document median ({(v - med) / mad:.0f} MADs).",
                        pointer=Pointer(evidence_id=ev.id, box=tuple(int(x) for x in b), text=l.text.strip())))
        return out
