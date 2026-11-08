"""Human-readable reports: a one-line summary, Markdown, and a standalone HTML file with the evidence highlighted."""

from __future__ import annotations

import base64
import html
import io
from pathlib import Path

from PIL import Image, ImageDraw

LEVEL_TEXT = {"clear": "No problems found", "review": "Needs a human review", "investigate": "Send to investigation"}
SEV_COLOR = {"critical": "#b42318", "high": "#c4320a", "medium": "#b54708", "low": "#667085", "info": "#667085"}


def scored(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f["severity"] != "info"]


def summary(verdict: dict) -> str:
    """One sentence a reviewer reads first."""
    fs = scored(verdict["findings"])
    n_files = len(verdict.get("evidence", []))
    if verdict["level"] == "clear":
        extra = f" {len(fs)} minor signal(s) noted." if fs else ""
        return f"{LEVEL_TEXT['clear']} in {n_files} file(s).{extra}"
    top = fs[0]["title"].rstrip(".") if fs else "several weak signals"
    more = f" (+{len(fs) - 1} more reason{'s' if len(fs) > 2 else ''})" if len(fs) > 1 else ""
    return f"{LEVEL_TEXT[verdict['level']]}: {top}{more}."


def markdown(verdict: dict) -> str:
    c = verdict.get("case", {})
    ev = {e["id"]: e for e in verdict.get("evidence", [])}
    out = [f"# Case {verdict['case_id']}: {verdict['level'].upper()} (risk {verdict['score']:.2f})", "", summary(verdict), ""]
    if c:
        amt = f"${c['claimed_amount']:,.2f}" if c.get("claimed_amount") else "n/a"
        out += [f"- Domain: {c.get('domain', '').replace('_', ' ')}; claimed {amt}; "
                f"{(c.get('loss_type') or c.get('category') or '').replace('_', ' ')}; incident {c.get('incident_date')}", ""]
    out += ["## Reasons", ""]
    for i, f in enumerate(verdict["findings"], 1):
        p = f.get("pointer") or {}
        where = ev.get(p.get("evidence_id"), {}).get("name", "")
        out.append(f"{i}. **[{f['severity']}]** {f['title']}" + (f" _(in {where})_" if where else ""))
        if f.get("detail"):
            out.append(f"   - {f['detail']}")
    if not verdict["findings"]:
        out.append("None.")
    if verdict.get("skipped"):
        out += ["", "## Not checked", ""] + [f"- {s}" for s in verdict["skipped"]]
    out += ["", f"_Policy {verdict['policy']}; models: {', '.join(f'{k}={v}' for k, v in verdict.get('models', {}).items()) or 'none'}. "
            "This is a recommendation for a human reviewer, not a decision._"]
    return "\n".join(out) + "\n"


def annotate(path: str | Path, boxes: list[tuple[tuple[int, int, int, int], str]], max_side: int = 900) -> Image.Image:
    im = Image.open(path).convert("RGB")
    draw = ImageDraw.Draw(im)
    w = max(2, round(max(im.size) / 300))
    for box, color in boxes:
        draw.rectangle(box, outline=color, width=w)
    im.thumbnail((max_side, max_side))
    return im


def data_uri(im: Image.Image) -> str:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def standalone_html(verdict: dict, case_dir: str | Path) -> str:
    """A single self-contained HTML file (images embedded) to attach to a claim file or email."""
    case_dir = Path(case_dir)
    boxes: dict[str, list] = {}
    for f in verdict["findings"]:
        p = f.get("pointer") or {}
        if p.get("evidence_id") and p.get("box"):
            boxes.setdefault(p["evidence_id"], []).append((tuple(p["box"]), SEV_COLOR[f["severity"]]))
    figs = []
    for e in verdict.get("evidence", []):
        if e["kind"] != "image":
            continue
        try:
            uri = data_uri(annotate(case_dir / e["name"], boxes.get(e["id"], [])))
        except Exception:
            continue
        figs.append(f'<figure><img src="{uri}" alt="{html.escape(e["name"])}"><figcaption>{html.escape(e["name"])} '
                    f'({html.escape(e["role"].replace("_", " "))})</figcaption></figure>')
    items = "".join(
        f'<li><span class="sev" style="background:{SEV_COLOR[f["severity"]]}">{f["severity"]}</span> {html.escape(f["title"])}'
        f'<div class="detail">{html.escape(f.get("detail") or "")}</div></li>' for f in verdict["findings"]) or "<li>None.</li>"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Case {html.escape(verdict['case_id'])}</title><style>
:root{{--bg:#fff;--fg:#101828;--muted:#475467;--line:#eaecf0}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0c111d;--fg:#f5f5f6;--muted:#94969c;--line:#1f242f}}}}
body{{font:16px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg);max-width:960px;margin:0 auto;padding:24px 16px}}
h1{{font-size:1.4rem}} .lead{{font-size:1.1rem}} li{{margin:.6rem 0}} .detail{{color:var(--muted);font-size:.9rem}}
.sev{{color:#fff;border-radius:4px;padding:1px 6px;font-size:.75rem;text-transform:uppercase;margin-right:6px}}
figure{{margin:16px 0;border:1px solid var(--line);border-radius:8px;padding:8px}} img{{max-width:100%;height:auto;display:block}}
figcaption{{color:var(--muted);font-size:.85rem;margin-top:4px}} .foot{{color:var(--muted);font-size:.85rem;margin-top:32px}}
</style></head><body>
<h1>Case {html.escape(verdict['case_id'])}: {verdict['level'].upper()} (risk {verdict['score']:.2f})</h1>
<p class="lead">{html.escape(summary(verdict))}</p><h2>Reasons</h2><ol>{items}</ol><h2>Evidence</h2>{''.join(figs)}
<p class="foot">Policy {html.escape(verdict['policy'])}. A recommendation for a human reviewer, not a decision.</p></body></html>"""
