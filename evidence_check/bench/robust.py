"""Adversarial robustness: what still gets caught when a fraudster launders the files first?

Every image in the suite (clean and manipulated, so the history pools are laundered too) goes through one transform:
recompression, resizing, cropping, stripping metadata, a screenshot, or a simulated re-photograph of the screen.
Then the whole suite runs again. Text-based evidence (receipts read by OCR) survives most of these; pixel and
metadata signals mostly do not, which is the point of layering.

    python -m evidence_check.bench.robust --out robust-out
"""

from __future__ import annotations

import argparse
import io
import json
import random
import shutil
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .cases import build_suite
from .run import evaluate, make_models, run_suite


def _save_like(im: Image.Image, path: Path, keep_exif: bytes | None = None, quality: int = 88) -> None:
    if path.suffix.lower() == ".png":
        im.save(path, "PNG")
    else:
        im.save(path, "JPEG", quality=quality, **({"exif": keep_exif} if keep_exif else {}))


def recompress(im, exif, r):
    return im, exif, 55


def resize(im, exif, r):
    return im.resize((im.width // 2, im.height // 2), Image.LANCZOS), exif, 88


def crop(im, exif, r):
    w, h = im.size
    return im.crop((int(w * .06), int(h * .05), int(w * .95), int(h * .94))), exif, 88


def strip_metadata(im, exif, r):
    return im, None, 90


def screenshot(im, exif, r):
    """Pasted into a phone-screen frame with status and navigation bars, saved without metadata."""
    w, h = im.size
    canvas = Image.new("RGB", (w + 40, h + 220), (24, 24, 27))
    canvas.paste(im, (20, 120))
    d = ImageDraw.Draw(canvas)
    d.rectangle((0, 0, w + 40, 60), fill=(10, 10, 12))
    d.text((24, 22), "9:41", fill=(240, 240, 240))
    d.rectangle(((w + 40) // 2 - 60, h + 175, (w + 40) // 2 + 60, h + 182), fill=(200, 200, 200))
    return canvas, None, 92


def rephoto(im, exif, r):
    """A photo of the image on a screen: slight perspective, blur, moire-like noise, colour cast, no original metadata."""
    w, h = im.size
    dx, dy = int(w * .04), int(h * .04)
    coeffs = _perspective([(0, 0), (w, 0), (w, h), (0, h)],
                          [(r.randint(0, dx), r.randint(0, dy)), (w - r.randint(0, dx), r.randint(0, dy)),
                           (w - r.randint(0, dx), h - r.randint(0, dy)), (r.randint(0, dx), h - r.randint(0, dy))])
    out = im.transform((w, h), Image.PERSPECTIVE, coeffs, Image.BICUBIC).filter(ImageFilter.GaussianBlur(1.1))
    a = np.asarray(out).astype(np.float32)
    a += np.random.default_rng(r.randint(0, 10**6)).normal(0, 6, a.shape)
    a *= np.array([1.0, 0.97, 1.04])
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)), None, 85


def _perspective(src, dst):
    m = []
    for (x, y), (u, v) in zip(dst, src):
        m.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        m.append([0, 0, 0, x, y, 1, -v * x, -v * y])
    b = np.array(src).reshape(8)
    return np.linalg.solve(np.array(m, dtype=float), b).tolist()


TRANSFORMS = {"none": None, "recompress": recompress, "resize": resize, "crop": crop, "strip_metadata": strip_metadata,
              "screenshot": screenshot, "rephoto": rephoto}


def launder(case_dirs: list[Path], name: str, seed: int = 0) -> None:
    fn = TRANSFORMS[name]
    if fn is None:
        return
    r = random.Random(seed)
    for d in case_dirs:
        for p in d.iterdir():
            if p.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                continue
            with Image.open(p) as src:
                exif = src.info.get("exif")
                im = src.convert("RGB")
            im, exif, q = fn(im, exif, r)
            if name in ("screenshot", "rephoto") and p.suffix.lower() != ".png":
                buf = io.BytesIO()
                im.save(buf, "PNG")
                im = Image.open(buf).convert("RGB")
            _save_like(im, p, exif, q)


def run(out: Path, seeds: list[int], clean: int, models_kind: str = "oracle") -> dict:
    results, per = {}, defaultdict(dict)
    for name in TRANSFORMS:
        rows = []
        for seed in seeds:
            base = out / "base" / f"seed-{seed}"
            if not base.exists():
                build_suite(base, clean, seed)
            dst = out / name / f"seed-{seed}"
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(base, dst)
            dirs = sorted((p for p in dst.iterdir() if (p / "case.json").exists()),
                          key=lambda p: (json.loads((p / "truth.json").read_text())["fraud"], p.name))
            launder(dirs, name, seed)
            rows += run_suite(dirs, make_models(models_kind), out / "work")
        res = evaluate(rows)
        full = res["layers"][-1]
        results[name] = {"caught": full["flagged"], "n_fraud": full["n_fraud"], "false_alarms": full["false_alarms"],
                         "n_clean": full["n_clean"], "checks": res["checks"]}
        for p in full["cases"]:
            key = f"{p['domain'].split('_')[0]}: {p['manipulation']}"
            hit, n = per[key].get(name, (0, 0))
            per[key][name] = (hit + p["flagged"], n + 1)
    return {"transforms": results, "matrix": per}


def report(res: dict) -> str:
    names = list(TRANSFORMS)
    out = ["# Robustness to laundering", "",
           "Every image in the suite was transformed before checking (clean cases too, so earlier photos in the history "
           "were laundered as well). Perception is oracle (ground-truth text), so this isolates what the checks themselves survive.", "",
           "| Transform | Manipulations caught | False alarms on clean cases |", "|---|---|---|"]
    for n in names:
        t = res["transforms"][n]
        out.append(f"| {n.replace('_', ' ')} | {t['caught']}/{t['n_fraud']} ({t['caught'] / max(1, t['n_fraud']):.0%}) | "
                   f"{t['false_alarms']}/{t['n_clean']} |")
    out += ["", "## Caught, per manipulation and transform", "", "| Manipulation | " + " | ".join(n.replace("_", " ") for n in names) + " |",
            "|---" * (len(names) + 1) + "|"]
    for key, row in sorted(res["matrix"].items()):
        cells = []
        for n in names:
            hit, tot = row.get(n, (0, 0))
            cells.append("✓" if hit == tot else ("✗" if hit == 0 else f"{hit}/{tot}"))
        out.append(f"| {key.replace('_', ' ')} | " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="robust-out")
    ap.add_argument("--seeds", default="11,12,13")
    ap.add_argument("--clean", type=int, default=8)
    a = ap.parse_args(argv)
    out = Path(a.out)
    res = run(out, [int(s) for s in a.seeds.split(",")], a.clean)
    md = report(res)
    (out / "report.md").write_text(md)
    (out / "results.json").write_text(json.dumps(res, indent=1, default=str))
    print(md)
    return res


if __name__ == "__main__":
    main()
