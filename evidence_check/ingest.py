"""Load case bundles safely: type by content (not extension), size and pixel limits, originals untouched."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

from .models import Case, Evidence

MAX_BYTES = 25 * 1024 * 1024
MAX_PIXELS = 40_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS  # Pillow raises DecompressionBombError above twice this; we check exactly below

SIGNATURES = [  # (offset, bytes, kind, mime)
    (0, b"\xff\xd8\xff", "image", "image/jpeg"),
    (0, b"\x89PNG\r\n\x1a\n", "image", "image/png"),
    (0, b"RIFF", "image", "image/webp"),  # confirmed below (WEBP vs WAVE)
    (0, b"%PDF-", "pdf", "application/pdf"),
    (0, b"fLaC", "audio", "audio/flac"),
    (0, b"OggS", "audio", "audio/ogg"),
    (0, b"ID3", "audio", "audio/mpeg"),
    (4, b"ftypM4A", "audio", "audio/mp4"),
]


class RejectedFile(ValueError):
    pass


def sniff(head: bytes) -> tuple[str, str]:
    if head[:4] == b"RIFF":
        if head[8:12] == b"WEBP":
            return "image", "image/webp"
        if head[8:12] == b"WAVE":
            return "audio", "audio/wav"
    if head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio", "audio/mpeg"
    for off, sig, kind, mime in SIGNATURES:
        if sig != b"RIFF" and head[off:off + len(sig)] == sig:
            return kind, mime
    raise RejectedFile("unsupported or unrecognised file type")


def load_evidence(path: Path, case_id: str, role: str) -> Evidence:
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise RejectedFile(f"{path.name}: {size} bytes exceeds the {MAX_BYTES} byte limit")
    data = path.read_bytes()
    kind, mime = sniff(data[:16])
    sha = hashlib.sha256(data).hexdigest()
    ev = Evidence(id=sha[:16], case_id=case_id, file=str(path), name=path.name, role=role, kind=kind, mime=mime,
                  sha256=sha, size=size)
    if kind == "image":
        with Image.open(path) as im:
            w, h = im.size
            if w * h > MAX_PIXELS:
                raise RejectedFile(f"{path.name}: {w}x{h} exceeds the {MAX_PIXELS} pixel limit")
            ev.width, ev.height = w, h
    return ev


def load_case(case_dir: str | Path) -> tuple[Case, list[Evidence], list[str]]:
    """Return the case, its evidence, and messages for any files that were rejected."""
    d = Path(case_dir)
    case = Case.model_validate(json.loads((d / "case.json").read_text()))
    evidence, rejected = [], []
    for ref in case.evidence:
        p = (d / ref.file).resolve()
        if d.resolve() not in p.parents:
            rejected.append(f"{ref.file}: path escapes the case folder")
            continue
        if not p.exists():
            rejected.append(f"{ref.file}: missing")
            continue
        try:
            evidence.append(load_evidence(p, case.case_id, ref.role))
        except RejectedFile as e:
            rejected.append(str(e))
        except Exception as e:  # corrupt or hostile file: reject, never crash the pipeline
            rejected.append(f"{ref.file}: could not be read ({type(e).__name__})")
    return case, evidence, rejected
