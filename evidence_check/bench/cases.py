"""Build benchmark cases (claims and expenses) with planted manipulations and ground truth.

Each case folder holds case.json, the evidence files, and truth.json:
  {"fraud": bool, "manipulations": [...], "ocr": {file: [[text, [x0,y0,x1,y1]], ...]}, "transcripts": {file: text}}
The OCR ground truth lets checks be evaluated separately from the vision model ("oracle perception").
"""

from __future__ import annotations

import json
import random
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

from PIL import Image

from .synth import (CITIES, ITEMS, MERCHANTS, PHONES, ReceiptSpec, edit_region, exif_bytes, photograph, render_receipt,
                    save_jpeg, scene_photo)
from .voice import Speaker, statement_text

CLAIM_MANIPULATIONS = ["edited_total", "generator_receipt", "flat_receipt_no_card", "wrong_tax", "no_card_txn",
                       "photo_before_policy", "photo_before_incident", "gps_mismatch", "amount_inflated", "copy_move_photo",
                       "edited_photo_software", "reused_photo", "wrong_loss_type", "statement_contradiction"]
EXPENSE_MANIPULATIONS = ["edited_total", "generator_receipt", "flat_receipt_no_card", "wrong_tax", "no_card_txn",
                         "receipt_date_mismatch", "amount_inflated", "reused_receipt"]


def _ocr_lines(flat_boxes_lines, to_photo):
    return [[t, list(to_photo(b))] for t, b in flat_boxes_lines]


def _receipt_lines(spec: ReceiptSpec, img_boxes: dict, flat: Image.Image) -> list[tuple[str, tuple]]:
    """Ground-truth OCR: every printed line with its box in the flat image (re-rendered to get all boxes)."""
    from PIL import ImageDraw

    from .synth import font

    d = ImageDraw.Draw(flat.copy())
    f, fb = font(22), font(26, mono=False)
    tax = spec.printed_tax if spec.printed_tax is not None else spec.tax
    total = spec.printed_total if spec.printed_total is not None else spec.total
    texts = [(spec.merchant, fb), (spec.city, f), ("TEL (555) 010-2299", f), ("", f), (img_boxes.get("_date_text", ""), f), ("", f)]
    texts += [(f"{n[:22]:<22}{p:>9.2f}", f) for n, p in spec.items]
    texts += [("-" * 31, f), (f"{'SUBTOTAL':<22}{spec.subtotal:>9.2f}", f), (f"{'TAX':<22}{tax:>9.2f}", f),
              (f"{'TOTAL':<22}{total:>9.2f}", f)]
    out, y = [], 30
    for t, fnt in texts:
        if t and not t.startswith("-"):
            x = (flat.width - d.textlength(t, font=fnt)) // 2 if fnt is fb or t.startswith(("TEL",)) or t == spec.city else 30
            out.append((t, d.textbbox((x, y), t, font=fnt)))
        y += 34
    return out


class Builder:
    def __init__(self, out: Path, seed: int = 11, real_photos: list[Path] | None = None, voice: str = "silent"):
        """real_photos: real damage photos (e.g. bench.photos.fetch_car_photos()). When given, every claim is a
        vehicle claim illustrated with real photos, which is what content checks (CLIP labels) need.
        voice: "silent" (transcript in truth.json only) or "tts" (spoken statements, for the speech model)."""
        self.speaker = Speaker(voice)
        self.real_photos = list(real_photos or [])
        self._deck: list[Path] = []  # real photos are dealt without replacement: a repeat would be a real duplicate
        self.out = Path(out)
        self.rng = random.Random(seed)
        random.seed(seed)
        self.n = 0
        self.photo_pool: list[tuple[str, str]] = []  # (path, claimant) of earlier genuine damage photos
        self.receipt_pool: list[tuple[str, str, dict]] = []

    # -- pieces -------------------------------------------------------------
    def _spec(self, domain: str, day: date, jur: str, category: str | None = None) -> ReceiptSpec:
        choices = MERCHANTS[domain]
        if category:
            choices = [m for m in choices if m[1] == category] or choices
        merchant, kind = self.rng.choice(choices)
        items = self.rng.sample(ITEMS[kind], k=min(len(ITEMS[kind]), self.rng.randint(1, 4)))
        return ReceiptSpec(merchant, CITIES[jur][0], day, items, CITIES[jur][3])

    def _write_receipt(self, d: Path, name: str, spec: ReceiptSpec, when: datetime, phone, gps, flat_png: bool = False,
                       generator: bool = False) -> tuple[str, list]:
        flat, boxes = render_receipt(spec)
        date_line = next((t for t in [f"{spec.day:%m/%d/%Y}"]), "")
        boxes["_date_text"] = date_line
        lines = _receipt_lines(spec, boxes, flat)
        if flat_png:
            path = d / f"{name}.png"
            pnginfo = None
            if generator:
                from PIL.PngImagePlugin import PngInfo

                pnginfo = PngInfo()
                pnginfo.add_text("parameters", "photo of a store receipt, thermal paper, realistic, total 284.50\nSteps: 30, Sampler: DPM++ 2M, Model: sdxl_base")
            flat.save(path, "PNG", pnginfo=pnginfo)
            return path.name, [[t, list(b)] for t, b in lines]
        photo, to_photo = photograph(flat, self.rng)
        path = d / f"{name}.jpg"
        save_jpeg(photo, path, quality=self.rng.choice([85, 88, 90]), exif=exif_bytes(when, phone, gps))
        self._last_total_box = to_photo(boxes["total_value"])
        return path.name, _ocr_lines(lines, to_photo)

    # -- cases ----------------------------------------------------------------
    def claim(self, manipulation: str | None = None) -> Path:
        r = self.rng
        self.n += 1
        cid = f"CLM-{2000 + self.n}"
        d = self.out / cid
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        jur = r.choice(list(CITIES))
        city, lat, lon, _ = CITIES[jur]
        loss = "vehicle_collision" if self.real_photos else r.choice(["water_damage", "fire", "theft", "vehicle_collision"])
        claimed_loss = r.choice([x for x in ("water_damage", "fire") if x != loss]) if manipulation == "wrong_loss_type" else loss
        pstart = date(2025, r.randint(1, 12), r.randint(1, 28))
        incident = pstart + timedelta(days=r.randint(60, 500))
        filed = incident + timedelta(days=r.randint(1, 14))
        claimant = f"P-{r.randint(1000, 9999)}"
        phone = (*r.choice(PHONES), f"SN{r.randint(10**7, 10**8 - 1)}")  # each person has their own phone
        evidence, ocr, txns, totals = [], {}, [], []
        # damage photos
        n_photos = r.randint(1, 2)
        for i in range(n_photos):
            when = datetime.combine(incident, datetime.min.time()) + timedelta(hours=r.randint(8, 30))
            gps = (lat + r.uniform(-0.02, 0.02), lon + r.uniform(-0.02, 0.02))
            software = None
            if manipulation == "photo_before_policy" and i == 0:
                when = datetime.combine(pstart - timedelta(days=r.randint(20, 200)), datetime.min.time()) + timedelta(hours=12)
            if manipulation == "photo_before_incident" and i == 0:
                when = datetime.combine(incident - timedelta(days=r.randint(5, 40)), datetime.min.time()) + timedelta(hours=12)
            if manipulation == "gps_mismatch" and i == 0:
                other = r.choice([c for k, c in CITIES.items() if k != jur])
                gps = (other[1], other[2])
            if manipulation == "edited_photo_software" and i == 0:
                software = "Adobe Photoshop 25.3 (Macintosh)"
            name = f"damage_{i + 1}.jpg"
            if manipulation == "reused_photo" and i == 0 and self.photo_pool:
                src, _ = r.choice(self.photo_pool)
                im = Image.open(src).convert("RGB")
                w, h = im.size
                im = im.crop((int(w * 0.04), int(h * 0.05), int(w * 0.97), int(h * 0.96))).resize((int(w * 0.85), int(h * 0.85)))
                save_jpeg(im, d / name, quality=82, exif=exif_bytes(when, phone, gps))
            else:
                if self.real_photos:
                    if not self._deck:
                        self._deck = self.real_photos[:]
                        r.shuffle(self._deck)
                    img = Image.open(self._deck.pop()).convert("RGB")
                    if img.width < 1000:
                        img = img.resize((1024, round(1024 * img.height / img.width)), Image.LANCZOS)
                else:
                    img = scene_photo(r, loss)
                if manipulation == "copy_move_photo" and i == 0:
                    w0, h0 = img.size
                    x0, y0 = r.randint(40, w0 // 3), r.randint(40, h0 // 3)
                    patch = img.crop((x0, y0, x0 + 200, y0 + 180))
                    img.paste(patch, (min(w0 - 210, x0 + r.randint(300, 500)), min(h0 - 190, y0 + r.randint(80, 300))))
                save_jpeg(img, d / name, quality=90, exif=exif_bytes(when, phone, gps, software))
                if not manipulation:
                    self.photo_pool.append((str(d / name), claimant))
            evidence.append({"file": name, "role": "damage_photo"})
        # repair receipts
        for i in range(r.randint(1, 2)):
            day = incident + timedelta(days=r.randint(0, 3))
            spec = self._spec("insurance_claims", day, jur)
            if manipulation == "wrong_tax" and i == 0:
                bad = round(spec.subtotal * r.choice([0.031, 0.052, 0.137]), 2)
                spec.printed_tax, spec.printed_total = bad, round(spec.subtotal + bad, 2)
            when = datetime.combine(day, datetime.min.time()) + timedelta(hours=r.randint(9, 19))
            flat = manipulation in ("generator_receipt", "flat_receipt_no_card") and i == 0
            fname, lines = self._write_receipt(d, f"receipt_{i + 1}", spec, when, phone, (lat, lon), flat_png=flat,
                                               generator=manipulation == "generator_receipt")
            printed = spec.printed_total if spec.printed_total is not None else spec.total
            if manipulation == "edited_total" and i == 0:
                inflated = round(spec.total * r.choice([3.0, 4.5, 10.0]), 2)
                edit_region(d / fname, self._last_total_box, f"{inflated:>9.2f}".strip(), d / fname)
                lines = [[f"{'TOTAL':<22}{inflated:>9.2f}", b] if t.startswith("TOTAL") else [t, b] for t, b in lines]
                printed = inflated
            ocr[fname] = lines
            evidence.append({"file": fname, "role": "receipt"})
            totals.append(printed)
            pays = not (flat or (manipulation == "no_card_txn" and i == 0))
            if pays:
                txns.append({"date": str(day), "merchant": spec.merchant, "amount": spec.total if manipulation != "wrong_tax" or i else printed})
        txns.append({"date": str(incident - timedelta(days=3)), "merchant": "SHELL OIL 5571", "amount": round(r.uniform(20, 80), 2)})
        claimed = round(sum(totals), 2)
        if manipulation == "amount_inflated":
            claimed = round(claimed * r.choice([1.5, 2.0, 3.0]), 2)
        # voice statement: what the claimant says happened (the true loss, date and cost)
        said_day, said_amount = incident, sum(totals)
        if manipulation == "statement_contradiction":
            said_day = incident - timedelta(days=r.randint(9, 25))
            said_amount = max(claimed * 2.5, claimed + 120) if claimed < 150 else claimed * 0.3
        text = statement_text(loss, said_day, said_amount)
        self.speaker.write(text, d / "statement.wav", seed=self.n)
        evidence.append({"file": "statement.wav", "role": "statement"})
        case = {"case_id": cid, "domain": "insurance_claims", "claimant_id": claimant, "claimant_name": f"Customer {claimant}",
                "segment": {"region": jur, "channel": r.choice(["app", "agent", "web"])},
                "claimed_amount": claimed, "loss_type": claimed_loss, "description": f"{claimed_loss.replace('_', ' ')} at insured property",
                "incident_date": str(incident), "filed_date": str(filed), "policy": {"start": str(pstart), "end": str(pstart + timedelta(days=730))},
                "location": {"lat": lat, "lon": lon, "address": city}, "jurisdiction": jur, "card_transactions": txns, "evidence": evidence}
        (d / "case.json").write_text(json.dumps(case, indent=1))
        (d / "truth.json").write_text(json.dumps({"fraud": manipulation is not None, "manipulations": [manipulation] if manipulation else [], "ocr": ocr,
                                              "transcripts": {"statement.wav": text}}, indent=1))
        return d

    def expense(self, manipulation: str | None = None) -> Path:
        r = self.rng
        self.n += 1
        cid = f"EXP-{5000 + self.n}"
        d = self.out / cid
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        jur = r.choice(list(CITIES))
        city, lat, lon, _ = CITIES[jur]
        category = r.choice(["meals", "lodging", "travel"])
        day = date(2026, r.randint(1, 9), r.randint(1, 28))
        filed = day + timedelta(days=r.randint(1, 20))
        employee = f"E-{r.randint(100, 999)}"
        phone = (*r.choice(PHONES), f"SN{r.randint(10**7, 10**8 - 1)}")  # each person has their own phone
        spec = self._spec("expense_reports", day, jur, category)
        if manipulation == "wrong_tax":
            bad = round(spec.subtotal * r.choice([0.031, 0.052, 0.137]), 2)
            spec.printed_tax, spec.printed_total = bad, round(spec.subtotal + bad, 2)
        when = datetime.combine(day, datetime.min.time()) + timedelta(hours=r.randint(7, 22))
        flat = manipulation in ("generator_receipt", "flat_receipt_no_card")
        if manipulation == "reused_receipt" and self.receipt_pool:
            src, _, src_case = r.choice(self.receipt_pool)
            im = Image.open(src).convert("RGB")
            w, h = im.size
            x0, y0 = int(w * 0.03), int(h * 0.02)
            im = im.crop((x0, y0, int(w * 0.98), int(h * 0.97)))
            fname = "receipt_1.jpg"
            save_jpeg(im, d / fname, quality=84, exif=exif_bytes(when, phone, None))
            lines = [[t, [b[0] - x0, b[1] - y0, b[2] - x0, b[3] - y0] if b else b]  # boxes follow the crop
                     for t, b in json.loads((Path(src).parent / "truth.json").read_text())["ocr"][Path(src).name]]
            total = src_case["claimed_amount"]
            day = date.fromisoformat(src_case["incident_date"])
            spec_merchant = src_case["merchant"]
            txns = [{"date": str(day), "merchant": spec_merchant, "amount": total}]
            printed = total
        else:
            fname, lines = self._write_receipt(d, "receipt_1", spec, when, phone, None, flat_png=flat,
                                               generator=manipulation == "generator_receipt")
            printed = spec.printed_total if spec.printed_total is not None else spec.total
            if manipulation == "edited_total":
                inflated = round(spec.total * r.choice([1.8, 2.5, 4.0]), 2)
                edit_region(d / fname, self._last_total_box, f"{inflated:.2f}", d / fname)
                lines = [[f"{'TOTAL':<22}{inflated:>9.2f}", b] if t.startswith("TOTAL") else [t, b] for t, b in lines]
                printed = inflated
            pays = not (flat or manipulation == "no_card_txn")
            txns = [{"date": str(day), "merchant": spec.merchant, "amount": spec.total if manipulation != "wrong_tax" else printed}] if pays else []
            spec_merchant = spec.merchant
        txns.append({"date": str(day - timedelta(days=2)), "merchant": "UBER TRIP", "amount": round(r.uniform(9, 40), 2)})
        claimed = printed if manipulation != "amount_inflated" else round(printed * r.choice([1.5, 2.0]), 2)
        claim_day = day if manipulation != "receipt_date_mismatch" else day + timedelta(days=r.randint(5, 30))
        case = {"case_id": cid, "domain": "expense_reports", "claimant_id": employee, "claimant_name": f"Employee {employee}",
                "segment": {"region": jur, "channel": r.choice(["mobile", "web"])},
                "claimed_amount": claimed, "category": category, "merchant": spec_merchant, "description": f"{category} expense",
                "incident_date": str(claim_day), "filed_date": str(max(filed, claim_day)), "jurisdiction": jur,
                "location": {"lat": lat, "lon": lon, "address": city}, "card_transactions": txns,
                "evidence": [{"file": fname, "role": "receipt"}]}
        (d / "case.json").write_text(json.dumps(case, indent=1))
        (d / "truth.json").write_text(json.dumps({"fraud": manipulation is not None, "manipulations": [manipulation] if manipulation else [],
                                                  "ocr": {fname: lines}}, indent=1))
        if not manipulation and not flat:
            self.receipt_pool.append((str(d / fname), employee, case))
        return d


def build_suite(out: str | Path, clean_per_domain: int = 12, seed: int = 11, real_photos: list[Path] | None = None,
                voice: str = "silent") -> list[Path]:
    """Clean cases first (they seed the history pools), then one case per manipulation, per domain."""
    b = Builder(Path(out), seed, real_photos, voice)
    dirs = [b.claim() for _ in range(clean_per_domain)] + [b.expense() for _ in range(clean_per_domain)]
    dirs += [b.claim(m) for m in CLAIM_MANIPULATIONS] + [b.expense(m) for m in EXPENSE_MANIPULATIONS]
    return dirs
