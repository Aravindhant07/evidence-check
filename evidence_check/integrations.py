"""Integrations: signed webhooks back to the source system, and an inbox folder watcher."""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import time
from pathlib import Path
from typing import Callable

SIGNATURE_HEADER = "X-Evcheck-Signature"


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def verify(secret: str, timestamp: str, body: bytes, signature: str, max_age_s: int = 300) -> bool:
    """For the receiving side: reject forged or replayed deliveries."""
    if abs(time.time() - float(timestamp)) > max_age_s:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), signature)


def make_webhook(url: str, secret: str, attempts: int = 3, timeout: float = 10.0) -> Callable[[str, dict], None]:
    """POST a compact verdict to `url` with an HMAC signature; retries with backoff, raises after the last try."""
    import httpx

    def send(case_id: str, verdict: dict) -> None:
        from .report import summary

        payload = {"case_id": case_id, "level": verdict["level"], "score": verdict["score"], "summary": summary(verdict),
                   "reasons": [{"check": f["check"], "severity": f["severity"], "title": f["title"]}
                               for f in verdict["findings"] if f["severity"] != "info"],
                   "policy": verdict["policy"]}
        body = json.dumps(payload, separators=(",", ":")).encode()
        last = None
        for i in range(attempts):
            ts = str(int(time.time()))
            try:
                r = httpx.post(url, content=body, timeout=timeout, headers={
                    "content-type": "application/json", "X-Evcheck-Timestamp": ts, SIGNATURE_HEADER: sign(secret, ts, body)})
                if r.status_code < 300:
                    return
                last = f"HTTP {r.status_code}"
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}: {e}"
            time.sleep(min(30, 2 ** i))
        raise RuntimeError(f"webhook to {url} failed after {attempts} attempts ({last})")

    return send


def watch(inbox: str | Path, svc, poll_s: float = 2.0, once: bool = False, actor: str = "watcher") -> list[str]:
    """Submit every case folder dropped into `inbox` once its files stop changing.

    Accepted folders move to inbox/.processed, rejected ones to inbox/.rejected with a reason.txt.
    """
    from .service import IntakeError

    inbox = Path(inbox)
    done, bad = inbox / ".processed", inbox / ".rejected"
    for d in (done, bad):
        d.mkdir(parents=True, exist_ok=True)
    seen: dict[Path, float] = {}
    submitted: list[str] = []
    while True:
        for d in sorted(p for p in inbox.iterdir() if p.is_dir() and not p.name.startswith(".")):
            if not (d / "case.json").exists():
                continue
            stamp = max(f.stat().st_mtime for f in d.iterdir())
            if not once and seen.get(d) != stamp:  # wait one poll with no changes (upload still in progress)
                seen[d] = stamp
                continue
            seen.pop(d, None)
            try:
                submitted.append(svc.submit_dir(d, actor=actor))
                shutil.move(str(d), done / f"{d.name}-{int(time.time())}")
            except IntakeError as e:
                target = bad / f"{d.name}-{int(time.time())}"
                shutil.move(str(d), target)
                (target / "reason.txt").write_text(str(e))
        if once:
            return submitted
        time.sleep(poll_s)
