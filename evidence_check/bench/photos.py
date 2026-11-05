"""Real damage photos for the benchmark, fetched on demand (not stored in the repository).

Source: DrBimmer/comprehensive-car-damage on Hugging Face (MIT licence), pinned revision.
"""

from __future__ import annotations

from pathlib import Path

DATASET = "DrBimmer/comprehensive-car-damage"
REVISION = "355b0990ec"
CACHE = Path.home() / ".cache" / "evidence-check" / "car_damage"
FOLDERS = ["F_Breakage", "F_Crushed", "R_Breakage", "R_Crushed"]


def fetch_car_photos(n: int = 60) -> list[Path]:
    from huggingface_hub import HfApi, hf_hub_download

    CACHE.mkdir(parents=True, exist_ok=True)
    have = sorted(CACHE.glob("*.jpg"))
    if len(have) >= n:
        return have[:n]
    full = HfApi().dataset_info(DATASET, revision=None).sha
    if not full.startswith(REVISION):
        raise RuntimeError(f"{DATASET} changed ({full}); re-pin REVISION after reviewing it")
    files = [s.rfilename for s in HfApi().dataset_info(DATASET, revision=full).siblings]
    per = max(1, n // len(FOLDERS))
    picks = [f for folder in FOLDERS for f in sorted(x for x in files if x.startswith(folder + "/") and x.lower().endswith(".jpg"))[:per]]
    out = []
    for f in picks:
        p = hf_hub_download(DATASET, f, repo_type="dataset", revision=full)
        dst = CACHE / f.replace("/", "_")
        if not dst.exists():
            dst.write_bytes(Path(p).read_bytes())
        out.append(dst)
    return out
