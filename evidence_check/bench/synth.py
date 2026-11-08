"""Realistic synthetic evidence: receipts rendered as phone photos with camera metadata,
textured scene photos, and the manipulations fraudsters actually use."""

from __future__ import annotations

import io
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import piexif
from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
MERCHANTS = {
    "insurance_claims": [("HOME DEPOT #1123", "REPAIR SUPPLIES"), ("ACE HARDWARE", "REPAIR SUPPLIES"),
                         ("ROTO-ROOTER PLUMBING", "SERVICE"), ("SAFELITE AUTOGLASS", "SERVICE"), ("LOWE'S #2290", "REPAIR SUPPLIES")],
    "expense_reports": [("BLUE BOTTLE COFFEE", "meals"), ("HILTON GARDEN INN", "lodging"), ("YELLOW CAB CO", "travel"),
                        ("SHAKE SHACK", "meals"), ("MARRIOTT DOWNTOWN", "lodging"), ("DELTA AIR LINES", "travel")],
}
ITEMS = {
    "REPAIR SUPPLIES": [("DRYWALL SHEET 4X8", 14.98), ("PVC PIPE 1/2IN", 6.47), ("WATER SHUTOFF VALVE", 18.97), ("CAULK TUBE", 5.98),
                        ("SUBFLOOR PANEL", 32.50), ("DEHUMIDIFIER RENTAL", 59.00), ("PAINT 1 GAL", 38.98), ("PLUMBER TAPE", 2.49)],
    "SERVICE": [("SERVICE CALL", 89.00), ("LABOR 2 HRS", 240.00), ("PARTS", 64.35), ("WINDSHIELD GLASS", 289.00), ("MOLDING KIT", 22.50)],
    "meals": [("LATTE", 5.75), ("BREAKFAST SANDWICH", 8.95), ("BURGER", 9.49), ("FRIES", 4.29), ("SALAD", 11.50), ("ICED TEA", 3.25)],
    "lodging": [("ROOM CHARGE 1 NIGHT", 189.00), ("ROOM CHARGE 1 NIGHT", 219.00), ("OCCUPANCY TAX", 23.40), ("PARKING", 35.00)],
    "travel": [("FARE", 38.40), ("AIRPORT FEE", 5.00), ("BAGGAGE FEE", 35.00), ("SEAT UPGRADE", 49.00)],
}
CITIES = {"US-NY": ("NEW YORK, NY", 40.7128, -74.0060, 0.08875), "US-CA": ("LOS ANGELES, CA", 34.0522, -118.2437, 0.095),
          "US-TX": ("AUSTIN, TX", 30.2672, -97.7431, 0.0825), "US-IL": ("CHICAGO, IL", 41.8781, -87.6298, 0.1025),
          "US-WA": ("SEATTLE, WA", 47.6062, -122.3321, 0.1035)}
PHONES = [("Apple", "iPhone 15", "17.5.1"), ("samsung", "SM-S921U", "S921USQU2AXE1"), ("Google", "Pixel 8", "AP2A.240805.005")]


def font(size: int, mono: bool = True) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / ("DejaVuSansMono.ttf" if mono else "DejaVuSans-Bold.ttf")), size)


@dataclass
class ReceiptSpec:
    merchant: str
    city: str
    day: date
    items: list[tuple[str, float]]
    tax_rate: float
    card_last4: str = "4417"
    subtotal: float = 0.0
    tax: float = 0.0
    total: float = 0.0
    printed_total: float | None = None  # what is printed, if it differs from the real total
    printed_tax: float | None = None

    def __post_init__(self):
        self.subtotal = round(sum(p for _, p in self.items), 2)
        self.tax = round(self.subtotal * self.tax_rate, 2)
        self.total = round(self.subtotal + self.tax, 2)


def render_receipt(spec: ReceiptSpec, width: int = 560) -> tuple[Image.Image, dict[str, tuple[int, int, int, int]]]:
    """Return a flat receipt image and the boxes of key printed fields."""
    f, fb = font(22), font(26, mono=False)
    lines: list[tuple[str, str, ImageFont.FreeTypeFont]] = [("merchant", spec.merchant, fb), ("", spec.city, f),
                                                            ("", "TEL (555) 010-2299", f), ("", "", f),
                                                            ("date", f"{spec.day:%m/%d/%Y}  {random.randint(8, 20):02d}:{random.randint(0, 59):02d}", f), ("", "", f)]
    for name, price in spec.items:
        lines.append(("item", f"{name[:22]:<22}{price:>9.2f}", f))
    tax = spec.printed_tax if spec.printed_tax is not None else spec.tax
    total = spec.printed_total if spec.printed_total is not None else spec.total
    lines += [("", "-" * 31, f), ("subtotal", f"{'SUBTOTAL':<22}{spec.subtotal:>9.2f}", f),
              ("tax", f"{'TAX':<22}{tax:>9.2f}", f), ("total", f"{'TOTAL':<22}{total:>9.2f}", f), ("", "", f),
              ("", f"VISA ************{spec.card_last4}", f), ("", "APPROVED  AUTH 0" + str(random.randint(10000, 99999)), f),
              ("", "", f), ("", "THANK YOU FOR SHOPPING", f)]
    lh = 34
    h = 40 + lh * len(lines) + 30
    img = Image.new("RGB", (width, h), (250, 249, 244))
    d = ImageDraw.Draw(img)
    boxes = {}
    y = 30
    for key, text, fnt in lines:
        x = (width - d.textlength(text, font=fnt)) // 2 if key in ("merchant",) or text.startswith(("TEL", "THANK")) or key == "" and text.isupper() and len(text) < 26 else 30
        d.text((x, y), text, fill=(25, 25, 30), font=fnt)
        if key and key not in boxes:
            bb = d.textbbox((x, y), text, font=fnt)
            boxes[key] = bb
        if key == "total":
            boxes["total_value"] = d.textbbox((x + d.textlength(text[:22], font=fnt), y), text[22:], font=fnt)
        y += lh
    return img, boxes


def photograph(flat: Image.Image, rng: random.Random, angle: float | None = None):
    """Make a flat document look like a phone photo: background, slight rotation, lighting, blur, noise.

    Returns the photo and a function mapping flat-image boxes to photo coordinates.
    """
    import math

    a = angle if angle is not None else rng.uniform(-3, 3)
    bg_w, bg_h = int(flat.width * 1.35), int(flat.height * 1.15)
    tone = rng.randint(90, 150)
    bg = Image.new("RGB", (bg_w, bg_h), (tone, tone - 10, tone - 25))
    paper = flat.rotate(a, expand=True, fillcolor=(tone, tone - 10, tone - 25), resample=Image.BICUBIC)
    ox, oy = (bg_w - paper.width) // 2, (bg_h - paper.height) // 2
    bg.paste(paper, (ox, oy))
    arr = np.asarray(bg).astype(np.float32)
    yy, xx = np.mgrid[0:bg_h, 0:bg_w]
    light = 1.0 - 0.18 * ((xx / bg_w - rng.uniform(0.3, 0.7)) ** 2 + (yy / bg_h - rng.uniform(0.3, 0.7)) ** 2)
    arr = arr * light[..., None] + np.random.default_rng(rng.randint(0, 10**6)).normal(0, 3.0, arr.shape)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.6))
    t = math.radians(a)
    cx, cy = flat.width / 2, flat.height / 2
    ncx, ncy = paper.width / 2, paper.height / 2

    def to_photo(box):
        pts = []
        for x, y in ((box[0], box[1]), (box[2], box[1]), (box[0], box[3]), (box[2], box[3])):
            dx, dy = x - cx, y - cy
            rx = dx * math.cos(t) + dy * math.sin(t)
            ry = -dx * math.sin(t) + dy * math.cos(t)
            pts.append((rx + ncx + ox, ry + ncy + oy))
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return (int(min(xs)), int(min(ys)), int(max(xs)) + 1, int(max(ys)) + 1)

    return img, to_photo


def scene_photo(rng: random.Random, kind: str = "water_damage", size: tuple[int, int] = (1024, 768)) -> Image.Image:
    """A textured synthetic 'scene' (stand-in when no real photo dataset is configured)."""
    w, h = size
    g = np.random.default_rng(rng.randint(0, 10**6))
    base = {"water_damage": (120, 105, 85), "fire": (60, 45, 40), "vehicle_collision": (140, 145, 150), "theft": (170, 160, 150)}.get(kind, (128, 128, 128))
    noise = g.normal(0, 1, (h // 8, w // 8))
    noise = np.array(Image.fromarray(((noise - noise.min()) / np.ptp(noise) * 255).astype(np.uint8)).resize((w, h), Image.BICUBIC)) / 255.0
    arr = np.stack([np.clip(c * (0.6 + 0.8 * noise) + g.normal(0, 6, (h, w)), 0, 255) for c in base], axis=2).astype(np.uint8)
    img = Image.fromarray(arr)
    d = ImageDraw.Draw(img)
    # Real rooms and vehicles are full of edges, corners and small objects; add comparable structure.
    for _ in range(45):
        x0, y0 = rng.randint(0, w - 60), rng.randint(0, h - 60)
        col = tuple(int(c * rng.uniform(0.3, 1.5)) % 256 for c in base)
        shape = rng.random()
        if shape < 0.4:
            d.rectangle([x0, y0, x0 + rng.randint(15, 180), y0 + rng.randint(15, 140)], outline=col, width=rng.randint(1, 5))
        elif shape < 0.7:
            r = rng.randint(6, 50)
            d.ellipse([x0, y0, x0 + r, y0 + r], outline=col, width=rng.randint(1, 4))
        else:
            d.line([x0, y0, x0 + rng.randint(-150, 150), y0 + rng.randint(-150, 150)], fill=col, width=rng.randint(1, 4))
    for _ in range(300):  # speckles: dirt, grain, small debris
        x0, y0 = rng.randint(0, w - 4), rng.randint(0, h - 4)
        v = rng.randint(0, 255)
        d.point((x0, y0), fill=(v, v, v))
    return img.filter(ImageFilter.GaussianBlur(0.7))


def exif_bytes(when: datetime, phone: tuple, gps: tuple[float, float] | None = None, software: str | None = None) -> bytes:
    def dms(x: float):
        x = abs(x)
        d, m = int(x), int((x - int(x)) * 60)
        s = round(((x - d) * 60 - m) * 60 * 100)
        return ((d, 1), (m, 1), (s, 100))
    ts = when.strftime("%Y:%m:%d %H:%M:%S").encode()
    zeroth = {piexif.ImageIFD.Make: phone[0].encode(), piexif.ImageIFD.Model: phone[1].encode(),
              piexif.ImageIFD.Software: (software or phone[2]).encode(), piexif.ImageIFD.DateTime: ts}
    exif = {piexif.ExifIFD.DateTimeOriginal: ts, piexif.ExifIFD.DateTimeDigitized: ts,
            piexif.ExifIFD.BodySerialNumber: (phone[3] if len(phone) > 3 else "SN00000000").encode()}  # unique per device
    gps_ifd = {}
    if gps:
        gps_ifd = {piexif.GPSIFD.GPSLatitudeRef: b"N" if gps[0] >= 0 else b"S", piexif.GPSIFD.GPSLatitude: dms(gps[0]),
                   piexif.GPSIFD.GPSLongitudeRef: b"E" if gps[1] >= 0 else b"W", piexif.GPSIFD.GPSLongitude: dms(gps[1])}
    return piexif.dump({"0th": zeroth, "Exif": exif, "GPS": gps_ifd, "1st": {}, "thumbnail": None})


def save_jpeg(img: Image.Image, path: Path, quality: int = 88, exif: bytes | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    kw = {"quality": quality}
    if exif:
        kw["exif"] = exif
    img.save(path, "JPEG", **kw)


def edit_region(path: Path, box: tuple[int, int, int, int], new_text: str, out: Path, quality: int = 88) -> None:
    """Simulate a fraudster editing a printed value: paint over it and type a new one, then re-save."""
    im = Image.open(path)
    exif = im.info.get("exif")
    im = im.convert("RGB")
    d = ImageDraw.Draw(im)
    x0, y0, x1, y1 = box
    patch = im.crop((max(0, x0 - 30), y0, max(1, x0 - 4), y1)).resize((x1 - x0 + 6, y1 - y0 + 6))
    im.paste(patch, (x0 - 3, y0 - 3))
    d.text((x0, y0 - 2), new_text, fill=(20, 20, 24), font=font(max(12, y1 - y0 + 2)))
    save_jpeg(im, out, quality=quality, exif=exif)
