"""Deterministic metadata extraction: EXIF/GPS, PNG text chunks, XMP, C2PA content credentials,
JPEG quality, PDF producer and edit history. No model involved."""

from __future__ import annotations

import io
import json
import re
from datetime import datetime
from typing import Any

from PIL import ExifTags, Image

from ..models import Evidence

GPS_TAG = 0x8825
EXIF_IFD = 0x8769


def _rational(v: Any) -> float:
    try:
        return float(v[0]) / float(v[1]) if isinstance(v, tuple) else float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _gps(gps: dict) -> tuple[float, float] | None:
    try:
        def deg(vals, ref):
            d, m, s = (_rational(x) for x in vals)
            x = d + m / 60 + s / 3600
            return -x if ref in ("S", "W") else x
        return deg(gps[2], gps[1]), deg(gps[4], gps[3])
    except (KeyError, TypeError, ValueError):
        return None


def _parse_exif_date(s: str | None) -> str | None:
    if not s:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d"):
        try:
            return datetime.strptime(s.strip("\x00 "), fmt).isoformat()
        except ValueError:
            pass
    return None


def jpeg_quality(im: Image.Image) -> int | None:
    """Estimate JPEG quality from the luminance quantization table (standard IJG tables)."""
    q = getattr(im, "quantization", None)
    if not q or 0 not in q:
        return None
    table = list(q[0])
    avg = sum(table) / len(table)
    # IJG: scale = 5000/Q (Q<50) or 200-2Q (Q>=50); base luminance table average is ~57.6
    scale = avg / 57.625 * 100
    quality = 5000 / scale if scale > 100 else (200 - scale) / 2
    return max(1, min(100, round(quality)))


def read_c2pa(path: str) -> dict[str, Any] | None:
    """Summary of a C2PA manifest, if present: generator, AI source type, actions, signature validity."""
    try:
        import c2pa
    except ImportError:  # pragma: no cover
        return None
    try:
        with c2pa.Reader(path) as reader:
            store = json.loads(reader.json())
    except Exception:
        return None
    active = store.get("manifests", {}).get(store.get("active_manifest", ""), {})
    actions, sources = [], set()
    for a in active.get("assertions", []):
        if str(a.get("label", "")).startswith("c2pa.actions"):
            for act in a.get("data", {}).get("actions", []):
                actions.append(act.get("action"))
                if act.get("digitalSourceType"):
                    sources.add(str(act["digitalSourceType"]).rsplit("/", 1)[-1])
    gen = active.get("claim_generator") or ""
    info = active.get("claim_generator_info") or []
    if info and isinstance(info, list):
        gen = gen or info[0].get("name", "")
    return {
        "generator": gen,
        "title": active.get("title"),
        "actions": actions,
        "digital_source_types": sorted(sources),
        "validation_errors": [s.get("code") for s in store.get("validation_status", []) or []],
    }


def image_metadata(path: str) -> dict[str, Any]:
    md: dict[str, Any] = {}
    with Image.open(path) as im:
        md["format"] = im.format
        md["mode"] = im.mode
        exif = im.getexif()
        tags = {ExifTags.TAGS.get(k, str(k)): v for k, v in exif.items() if k not in (GPS_TAG, EXIF_IFD)}
        sub = exif.get_ifd(EXIF_IFD) if exif else {}
        tags.update({ExifTags.TAGS.get(k, str(k)): v for k, v in sub.items()})
        def s(v):
            return v.decode(errors="ignore") if isinstance(v, bytes) else str(v)
        md["exif"] = {k: s(v)[:200] for k, v in tags.items() if k in (
            "Make", "Model", "Software", "DateTime", "DateTimeOriginal", "DateTimeDigitized", "BodySerialNumber",
            "LensModel", "Artist", "ImageDescription", "OffsetTimeOriginal")}
        md["capture_time"] = _parse_exif_date(tags.get("DateTimeOriginal") or tags.get("DateTimeDigitized"))
        md["modify_time"] = _parse_exif_date(tags.get("DateTime"))
        gps = exif.get_ifd(GPS_TAG) if exif else {}
        if gps:
            ll = _gps(gps)
            if ll:
                md["gps"] = {"lat": round(ll[0], 6), "lon": round(ll[1], 6)}
        # PNG text chunks (image generators often write their parameters here) and other info keys
        text = {k: (v if isinstance(v, str) else repr(v))[:500] for k, v in im.info.items()
                if isinstance(k, str) and k not in ("exif", "icc_profile", "dpi", "jfif", "jfif_version", "jfif_unit",
                                                    "jfif_density", "progressive", "progression", "adobe", "adobe_transform")}
        if text:
            md["info"] = text
        if im.format == "JPEG":
            md["jpeg_quality"] = jpeg_quality(im)
    raw = open(path, "rb").read()
    m = re.search(rb"<x:xmpmeta.*?</x:xmpmeta>", raw, re.S)
    if m:
        xmp = m.group(0).decode(errors="ignore")
        md["xmp_tools"] = sorted(set(re.findall(r"(?:CreatorTool|softwareAgent)[=>\"']+([^\"'<]+)", xmp)))[:10]
    c = read_c2pa(path)
    if c:
        md["c2pa"] = c
    return md


def pdf_metadata(path: str) -> dict[str, Any]:
    from pypdf import PdfReader

    raw = open(path, "rb").read()
    md: dict[str, Any] = {"eof_markers": raw.count(b"%%EOF"),
                          "has_javascript": bool(re.search(rb"/(JS|JavaScript)\b", raw)),
                          "has_embedded_files": b"/EmbeddedFile" in raw}
    try:
        r = PdfReader(io.BytesIO(raw))
        info = r.metadata or {}
        md["producer"] = str(info.get("/Producer", "") or "")[:200]
        md["creator"] = str(info.get("/Creator", "") or "")[:200]
        md["creation_date"] = str(info.get("/CreationDate", "") or "")
        md["mod_date"] = str(info.get("/ModDate", "") or "")
        md["pages"] = len(r.pages)
        md["text"] = "\n".join((p.extract_text() or "") for p in r.pages[:10])[:20000]
    except Exception as e:  # malformed PDF: record, don't crash
        md["error"] = type(e).__name__
    return md


def read_metadata(ev: Evidence) -> dict[str, Any]:
    try:
        if ev.kind == "image":
            return image_metadata(ev.file)
        if ev.kind == "pdf":
            return pdf_metadata(ev.file)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"[:200]}
    return {}
