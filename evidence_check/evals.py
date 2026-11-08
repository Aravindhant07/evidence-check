"""Per-model golden sets and the model swap gate.

Each perception model is measured on a fixed, versioned set (generator seeds and a pinned dataset revision), and
the numbers are compared with evidence_check/evals_baseline.json. Changing a model pin in models.yaml must not make any metric
worse than its tolerance; CI or a person runs `evcheck eval models` before the change ships.

    ocr       field accuracy (total, subtotal, tax, date, merchant) on photographed receipts with known text
    labels    top loss-type label on real car-damage photos (all are vehicle collisions)
    speech    word error rate and fact extraction (amount, date, loss type) on spoken statements
    ai_image  share of genuine photos the AI-image detector scores >= 0.8 (lower is better)
"""

from __future__ import annotations

import json
import re
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASELINE = ROOT / "evals_baseline.json"
SETS = {"ocr": {"seed": 101, "n": 16}, "labels": {"photos": 30}, "speech": {"seed": 7, "n": 8}, "ai_image": {"photos": 60}}
# metric -> (direction, tolerance): "up" means higher is better
TOLERANCE = {"ocr.field_accuracy": ("up", 0.03), "ocr.total_accuracy": ("up", 0.05), "labels.accuracy": ("up", 0.05),
             "speech.wer": ("down", 0.03), "speech.fact_accuracy": ("up", 0.05), "ai_image.false_positive_rate": ("down", 0.05)}


def wer(ref: str, hyp: str) -> float:
    r, h = re.findall(r"[a-z0-9']+", ref.lower()), re.findall(r"[a-z0-9']+", hyp.lower())
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return d[len(h)] / max(1, len(r))


def eval_ocr(reg, seed: int, n: int) -> dict:
    import random

    from PIL import Image

    from .bench.synth import CITIES, ITEMS, MERCHANTS, ReceiptSpec, photograph, render_receipt
    from .extract.receipt import parse_lines

    r = random.Random(seed)
    fields = ok = totals = 0
    for _ in range(n):
        jur = r.choice(list(CITIES))
        merchant, kind = r.choice(MERCHANTS[r.choice(["insurance_claims", "expense_reports"])])
        from datetime import date

        spec = ReceiptSpec(merchant, CITIES[jur][0], date(2026, r.randint(1, 12), r.randint(1, 28)),
                           r.sample(ITEMS[kind], k=min(len(ITEMS[kind]), r.randint(1, 4))), CITIES[jur][3])
        flat, _ = render_receipt(spec)
        photo, _ = photograph(flat, r)
        lines, _ = reg.read_receipt(photo.convert("RGB"))
        got = parse_lines(lines)
        truth = {"total": round(spec.total, 2), "subtotal": round(spec.subtotal, 2), "tax": round(spec.tax, 2),
                 "date": spec.day.isoformat(), "merchant": merchant.upper()}
        vals = {"total": got.total and round(got.total.value, 2), "subtotal": got.subtotal and round(got.subtotal.value, 2),
                "tax": got.tax and round(got.tax.value, 2), "date": got.date and got.date.value,
                "merchant": got.merchant and re.sub(r"\s+", " ", str(got.merchant.value).upper())}
        for k, v in truth.items():
            fields += 1
            ok += vals[k] == v
        totals += vals["total"] == truth["total"]
    return {"field_accuracy": round(ok / fields, 3), "total_accuracy": round(totals / n, 3), "n": n}


def eval_labels(reg, photos: int) -> dict:
    from PIL import Image

    from .bench.photos import fetch_car_photos
    from .policy import load_policy

    pol = load_policy("insurance_claims")
    prompts, owner = [], {}
    for loss, ps in pol.loss_labels.items():
        for p in ps:
            prompts.append(p)
            owner[p] = loss
    hit = 0
    paths = fetch_car_photos()[:photos]
    for p in paths:
        probs, _ = reg.clip(Image.open(p).convert("RGB"), prompts)
        hit += owner[max(probs, key=probs.get)] == "vehicle_collision"
    return {"accuracy": round(hit / len(paths), 3), "n": len(paths)}


def eval_speech(reg, seed: int, n: int) -> dict:
    import random
    from datetime import date

    from .bench.voice import Speaker, statement_text
    from .checks.statement import amounts, dates, loss_types

    r = random.Random(seed)
    sp = Speaker("tts")
    errs, facts, total = [], 0, 0
    with tempfile.TemporaryDirectory() as t:
        for i in range(n):
            loss = r.choice(["vehicle_collision", "water_damage", "fire", "theft"])
            day = date(2026, r.randint(1, 12), r.randint(1, 28))
            amount = r.choice([80, 240, 650, 1200, 1850, 3400])
            text = statement_text(loss, day, amount)
            wav = Path(t) / f"s{i}.wav"
            sp.write(text, wav, seed=i)
            hyp = reg.transcribe(str(wav))
            errs.append(wer(text, hyp))
            said = [a for a in amounts(hyp) if a >= 20]
            facts += any(abs(a - amount) <= max(25, 0.1 * amount) for a in said)
            facts += day in dates(hyp, day.year)
            facts += loss in loss_types(hyp)
            total += 3
    return {"wer": round(sum(errs) / len(errs), 3), "fact_accuracy": round(facts / total, 3), "n": n}


def eval_ai_image(reg, photos: int) -> dict:
    from PIL import Image

    from .bench.photos import fetch_car_photos

    paths = fetch_car_photos()[:photos]
    scores = [reg.ai_score(Image.open(p).convert("RGB")) for p in paths]
    return {"false_positive_rate": round(sum(s >= 0.8 for s in scores) / len(scores), 3), "n": len(scores)}


def run_all(reg, only: set[str] | None = None) -> dict:
    out = {}
    jobs = {"ocr": lambda: eval_ocr(reg, **SETS["ocr"]), "labels": lambda: eval_labels(reg, **SETS["labels"]),
            "speech": lambda: eval_speech(reg, **SETS["speech"]), "ai_image": lambda: eval_ai_image(reg, **SETS["ai_image"])}
    for name, fn in jobs.items():
        if only and name not in only:
            continue
        t0 = time.perf_counter()
        out[name] = {**fn(), "seconds": round(time.perf_counter() - t0, 1)}
    out["_models"] = {job: reg.tag(job) for job in reg.cfg}
    out["_sets"] = SETS
    return out


def compare(current: dict, baseline: dict) -> tuple[bool, list[str]]:
    problems = []
    for key, (direction, tol) in TOLERANCE.items():
        group, metric = key.split(".")
        if group not in current or group not in baseline:
            continue
        now, then = current[group][metric], baseline[group][metric]
        worse = (then - now) if direction == "up" else (now - then)
        if worse > tol:
            problems.append(f"{key}: {then} -> {now} (worse by {worse:.3f}, tolerance {tol})")
    return not problems, problems


def load_baseline() -> dict | None:
    return json.loads(BASELINE.read_text()) if BASELINE.exists() else None
