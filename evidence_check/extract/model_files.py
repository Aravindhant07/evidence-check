"""Download pinned models for offline use and verify their files against models.lock.json (SHA-256)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

ALLOW = ["*.json", "*.safetensors", "*.txt", "*.model", "tokenizer*", "vocab*", "merges*", "*.tiktoken"]
DENY = ["*.bin", "*.pt", "*.pth", "*.ckpt", "*.h5", "*.msgpack", "*.onnx", "*.py", "*.pkl"]  # pickles and code never load


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p.resolve(), "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(model_id: str, revision: str, offline: bool) -> Path:
    from huggingface_hub import snapshot_download

    return Path(snapshot_download(model_id, revision=revision, allow_patterns=ALLOW, ignore_patterns=DENY,
                                  local_files_only=offline))


def download(config: Path, include_optional: bool = False) -> dict[str, Path]:
    cfg = yaml.safe_load(config.read_text())
    return {job: snapshot(c["id"], c["revision"], offline=False) for job, c in cfg.items()
            if include_optional or not c.get("optional")}


def lock(config: Path, lock_path: Path, include_optional: bool = True) -> dict:
    cfg = yaml.safe_load(config.read_text())
    out = {}
    for job, c in cfg.items():
        if c.get("optional") and not include_optional:
            continue
        try:
            d = snapshot(c["id"], c["revision"], offline=True)
        except Exception:
            continue  # not downloaded here; lock what is present
        out[job] = {"id": c["id"], "revision": c["revision"],
                    "files": {str(p.relative_to(d)): _sha(p) for p in sorted(d.rglob("*")) if p.is_file()}}
    lock_path.write_text(json.dumps(out, indent=1) + "\n")
    return out


def verify(config: Path, lock_path: Path) -> list[str]:
    """Problems found (empty list = every downloaded model matches its pinned revision and recorded hashes)."""
    cfg = yaml.safe_load(config.read_text())
    locked = json.loads(lock_path.read_text())
    problems = []
    for job, c in cfg.items():
        entry = locked.get(job)
        if not entry:
            if not c.get("optional"):
                problems.append(f"{job}: not in the lock file")
            continue
        if entry["revision"] != c["revision"] or entry["id"] != c["id"]:
            problems.append(f"{job}: models.yaml pins {c['id']}@{c['revision'][:8]} but the lock has {entry['id']}@{entry['revision'][:8]}")
            continue
        try:
            d = snapshot(c["id"], c["revision"], offline=True)
        except Exception:
            if not c.get("optional"):
                problems.append(f"{job}: not downloaded (run `evcheck models download`)")
            continue
        for name, sha in entry["files"].items():
            p = d / name
            if not p.exists():
                problems.append(f"{job}: missing {name}")
            elif _sha(p) != sha:
                problems.append(f"{job}: {name} does not match its recorded SHA-256")
        extra = {str(p.relative_to(d)) for p in d.rglob("*") if p.is_file()} - set(entry["files"])
        problems += [f"{job}: unexpected file {x}" for x in sorted(extra) if any(x.endswith(s[1:]) for s in DENY)]
    return problems
