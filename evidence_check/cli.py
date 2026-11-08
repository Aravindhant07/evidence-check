"""evcheck: command line for checking cases, running the service, the MCP server and the benchmark."""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Check the evidence behind claims and expense reports.")
keys_app = typer.Typer(no_args_is_help=True, help="Manage API keys.")
app.add_typer(keys_app, name="keys")
improve_app = typer.Typer(no_args_is_help=True, help="Policy improvement: proposals, the gate, approval, shadow, rollback.")
app.add_typer(improve_app, name="improve")

MODELS_HELP = "Perception: none (metadata and PDF text only), real (local Hugging Face models), oracle (benchmark ground truth)"


def make_models(kind: str, truth_root: Path | None = None):
    if kind == "none":
        return None
    if kind == "real":
        from .extract.models import ModelRegistry

        return ModelRegistry()
    if kind == "oracle":
        from .extract.oracle import OracleModels

        return OracleModels(truth_root)
    raise typer.BadParameter(f"unknown models {kind!r}")


def service(data: Path, models: str, truth_root: Path | None = None, webhook_url: str | None = None):
    import os

    from .service import Service

    hook = None
    if webhook_url:
        from .integrations import make_webhook

        secret = os.environ.get("EVCHECK_WEBHOOK_SECRET")
        if not secret:
            raise typer.BadParameter("set EVCHECK_WEBHOOK_SECRET to sign webhook deliveries")
        hook = make_webhook(webhook_url, secret)
    return Service(data, make_models(models, truth_root), webhook=hook)


@app.command()
def check(case_dir: Path = typer.Argument(..., exists=True, file_okay=False),
          models: str = typer.Option("none", help=MODELS_HELP),
          policy: Optional[str] = typer.Option(None, help="Domain policy name or YAML path (default: the case's domain)"),
          history: Optional[Path] = typer.Option(None, help="History index file to compare against (and add to)"),
          as_json: bool = typer.Option(False, "--json", help="Print the verdict as JSON"),
          html: Optional[Path] = typer.Option(None, help="Also write a standalone HTML report here"),
          fail_on: Optional[str] = typer.Option(None, help="Exit with code 1 at this level or above: review or investigate")):
    """Check one case folder (case.json + evidence files) and print the verdict."""
    from . import report
    from .engine import run_case
    from .history import HistoryIndex
    from .service import evidence_view

    case, evidence, v = run_case(case_dir, policy, make_models(models), HistoryIndex(history) if history else None)
    verdict = {**v.model_dump(mode="json"), "evidence": [evidence_view(e) for e in evidence], "case": case.model_dump(mode="json")}
    if as_json:
        typer.echo(json.dumps({**verdict, "summary": report.summary(verdict)}, indent=1, default=str))
    else:
        typer.echo(report.markdown(verdict))
    if html:
        html.write_text(report.standalone_html(verdict, case_dir))
        typer.echo(f"Report written to {html}", err=True)
    order = {"clear": 0, "review": 1, "investigate": 2}
    if fail_on and order[v.level.value] >= order[fail_on]:
        raise typer.Exit(1)


@app.command()
def submit(case_dir: Path = typer.Argument(..., exists=True, file_okay=False),
           data: Path = typer.Option(Path("data"), help="Data directory"),
           now: bool = typer.Option(False, help="Check immediately instead of queueing for a worker"),
           models: str = typer.Option("none", help=MODELS_HELP)):
    """Add a case to the service's queue (or check it now with --now)."""
    svc = service(data, models)
    cid = svc.submit_dir(case_dir, actor="cli", run_now=now)
    row = svc.store.case(cid)
    typer.echo(f"{cid}: {row['status']}" + (f" ({row['level']}, risk {row['score']:.2f})" if row["level"] else ""))


@app.command()
def worker(data: Path = typer.Option(Path("data")), models: str = typer.Option("real", help=MODELS_HELP),
           webhook_url: Optional[str] = typer.Option(None, help="POST signed verdicts here (secret: EVCHECK_WEBHOOK_SECRET)")):
    """Process queued cases until stopped (run several for more throughput; they share the queue safely)."""
    svc = service(data, models, webhook_url=webhook_url)
    typer.echo(f"worker started on {data} (models: {models}); Ctrl+C to stop", err=True)
    try:
        svc.run_worker()
    except KeyboardInterrupt:
        svc.stop()


@app.command()
def serve(data: Path = typer.Option(Path("data")), host: str = "127.0.0.1", port: int = 8000,
          models: str = typer.Option("none", help=MODELS_HELP), workers: int = typer.Option(1, help="In-process worker threads (0: none)"),
          truth_root: Optional[Path] = typer.Option(None, hidden=True),
          webhook_url: Optional[str] = typer.Option(None, help="POST signed verdicts here (secret: EVCHECK_WEBHOOK_SECRET)")):
    """Run the reviewer UI and REST API (with in-process workers)."""
    import uvicorn

    from .web.app import create_app, load_keys

    if not load_keys(data) and host not in ("127.0.0.1", "localhost", "::1"):
        typer.echo("Refusing to listen on a network address without API keys. Add one with `evcheck keys add`.", err=True)
        raise typer.Exit(2)
    svc = service(data, models, truth_root, webhook_url)
    for _ in range(workers):
        svc.start_worker_thread()
    typer.echo(f"evidence-check on http://{host}:{port}  (API docs: /api/docs)", err=True)
    uvicorn.run(create_app(svc), host=host, port=port, log_level="warning")


@app.command()
def mcp(data: Path = typer.Option(Path("data")), models: str = typer.Option("none", help=MODELS_HELP)):
    """Run the MCP server (stdio) so AI agents can check cases and look up evidence."""
    from .mcp_server import build_server

    build_server(service(data, models)).run()


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def bench(ctx: typer.Context):
    """Run the benchmark (options as in `python -m evidence_check.bench.run --help`)."""
    from .bench.run import main

    main(ctx.args)


@app.command()
def demo(data: Path = typer.Option(Path("demo-data")), port: int = 8000,
         models: str = typer.Option("oracle", help=MODELS_HELP + ". The demo defaults to oracle: instant and offline."),
         serve_ui: bool = typer.Option(True, "--serve/--no-serve")):
    """Create sample claims and expense reports, check them, and open the reviewer UI on the riskiest case."""
    from .bench.cases import Builder

    src = data / "samples"
    b = Builder(src, seed=7)
    dirs = [b.claim() for _ in range(5)] + [b.expense() for _ in range(5)]
    dirs += [b.claim(m) for m in ("edited_total", "reused_photo", "gps_mismatch", "statement_contradiction", "generator_receipt")]
    dirs += [b.expense(m) for m in ("reused_receipt", "wrong_tax", "no_card_txn")]
    svc = service(data, models, truth_root=src)
    for d in dirs:
        svc.submit_dir(d, actor="demo")
    n = svc.drain()
    top = svc.store.cases()[0]
    typer.echo(f"Checked {n} sample cases. Riskiest: {top['case_id']} ({top['level']}, risk {top['score']:.2f})")
    if serve_ui:
        url = f"http://127.0.0.1:{port}/cases/{top['case_id']}"
        typer.echo(f"Open {url}")
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass
        serve(data=data, host="127.0.0.1", port=port, models=models, workers=1, truth_root=src, webhook_url=None)


@app.command()
def calibrate(results: Optional[Path] = typer.Option(None, help="Benchmark results.json (default: closed cases in --data)"),
              data: Path = typer.Option(Path("data")),
              cases_per_day: float = 1000, review_capacity: float = 60, fraud_rate: float = 0.03,
              avg_fraud_loss: float = 2500, review_cost: float = 25,
              out: Optional[Path] = typer.Option(None, help="Write the Markdown report here")):
    """Threshold sweep, the threshold for your review capacity, ROI and a fairness check, from labelled cases."""
    from . import calibrate as cal
    from .store import Store

    data_ = cal.from_benchmark(results) if results else cal.from_store(Store(data / "evcheck.db"))
    if not data_ or len({x.fraud for x in data_}) < 2:
        typer.echo("Need labelled cases of both kinds: close cases as fraud/legitimate in the UI, or pass --results.", err=True)
        raise typer.Exit(2)
    md, _ = cal.report(data_, cal.Economics(cases_per_day, review_capacity, fraud_rate, avg_fraud_loss, review_cost))
    if out:
        out.write_text(md)
    typer.echo(md)


@app.command()
def watch(inbox: Path = typer.Argument(..., file_okay=False), data: Path = typer.Option(Path("data")),
          once: bool = typer.Option(False, help="Process what is there now and exit")):
    """Submit case folders dropped into an inbox folder (for systems that can only export files)."""
    from .integrations import watch as run_watch

    inbox.mkdir(parents=True, exist_ok=True)
    ids = run_watch(inbox, service(data, "none"), once=once)
    if once:
        typer.echo(f"submitted {len(ids)} case(s)")


@app.command()
def retention(days: int = typer.Option(..., help="Delete evidence files of cases closed more than this many days ago"),
              data: Path = typer.Option(Path("data"))):
    """Apply the retention policy (verdicts, decisions and the audit log are kept)."""
    removed = service(data, "none").retention(days)
    typer.echo(f"purged evidence files of {len(removed)} case(s)")


@app.command()
def investigate(case_id: str, data: Path = typer.Option(Path("data")),
                model: str = typer.Option("local", help="local (Qwen2.5-3B on CPU, pinned) or scripted:<file.json> for replays")):
    """Ask the investigator agent for a cited brief on a case (read-only; every sentence is citation-checked)."""
    import anyio

    from .agents.investigator import investigate as run
    from .agents.runtime import LocalChatModel
    from .mcp_server import build_server

    svc = service(data, "none")
    if model.startswith("scripted:"):
        replies = json.loads(Path(model.split(":", 1)[1]).read_text())

        class Replay:
            name = "replay"

            def chat(self, messages):
                return replies.pop(0)

        llm = Replay()
    else:
        llm = LocalChatModel()
    brief = anyio.run(run, build_server(svc), llm, case_id)
    svc.store.audit("investigator", "brief", case_id, {"kept": len(brief.kept), "dropped": len(brief.dropped), "model": brief.model})
    typer.echo(json.dumps(brief.to_dict(), indent=1))


@app.command()
def redteam(case_dir: Path = typer.Argument(..., exists=True, file_okay=False), models: str = typer.Option("none", help=MODELS_HELP),
            budget: int = 20):
    """Try to launder a flagged case until it passes; evasions are saved for the evaluation set."""
    from .agents.redteam import red_team

    res = red_team(case_dir, make_models(models, case_dir.parent), budget=budget)
    typer.echo(f"{res.case_id}: {res.original_level} ({', '.join(res.original_checks)}); {len(res.attempts)} variants tried")
    for e in res.evasions:
        typer.echo(f"  EVADED with {' + '.join(e['transforms'])}: defeated {', '.join(e['defeated'])} -> {e['path']}")
    if not res.evasions:
        typer.echo("  no evasion found within the budget")


def _rows(results: Optional[Path], svc) -> list[dict]:
    rows = svc.labelled_rows()
    if results:
        rows += json.loads(results.read_text())["rows"]
    if not rows:
        typer.echo("No labelled cases: pass --results (a benchmark results.json) or close cases in the UI.", err=True)
        raise typer.Exit(2)
    return rows


@improve_app.command("propose")
def improve_propose(domain: str, data: Path = typer.Option(Path("data")), results: Optional[Path] = None):
    """Search for gated improvements (and reviewer-agreement fixes) and submit them as proposals."""
    from .agents.improve import from_reviewer_agreement, search

    svc = service(data, "none")
    rows = _rows(results, svc)
    base = svc.policies.policy(domain)
    props = search(rows, domain, base, svc.policies.live_overrides(domain))
    props += from_reviewer_agreement(svc.store.check_precision(), domain, base)
    for p in props:
        p = svc.policies.submit(p, rows)
        typer.echo(f"{p.id}  {p.status:11s} {p.kind} {p.params}  {p.rationale}")
        flips = (p.gate or {}).get("flips", [])
        if flips:
            typer.echo(f"    {len(flips)} case(s) would change verdict; inspect them before approving: "
                       + ", ".join(f"{f['case']} ({f['before']}->{f['after']})" for f in flips[:10]))
    if not props:
        typer.echo("No change improves on the current policy without failing the gate.")


@improve_app.command("list")
def improve_list(data: Path = typer.Option(Path("data")), status: Optional[str] = None):
    for p in service(data, "none").policies.proposals(status):
        typer.echo(f"{p['id']}  {p['status']:11s} {p['domain']}: {p['kind']} {p['params']}  ({p['source']})")


@improve_app.command("approve")
def improve_approve(proposal_id: str, actor: str = typer.Option(..., help="Who approves (recorded in the audit log)"),
                    data: Path = typer.Option(Path("data"))):
    """Approve a gated proposal: it runs in shadow next to the live policy."""
    service(data, "none").policies.approve_for_shadow(proposal_id, actor)
    typer.echo("in shadow: new cases are scored by both policies; promote when the shadow results look right")


@improve_app.command("promote")
def improve_promote(proposal_id: str, actor: str = typer.Option(...), data: Path = typer.Option(Path("data"))):
    svc = service(data, "none")
    rep_ = svc.shadow_report()
    svc.policies.promote(proposal_id, actor)
    typer.echo(f"live (shadow agreed on {rep_['agree']} of {rep_['cases']} cases)")


@improve_app.command("reject")
def improve_reject(proposal_id: str, actor: str = typer.Option(...), data: Path = typer.Option(Path("data"))):
    service(data, "none").policies.reject(proposal_id, actor)


@improve_app.command("rollback")
def improve_rollback(domain: str, actor: str = typer.Option(...), data: Path = typer.Option(Path("data"))):
    """Restore the previous live policy version in one step."""
    v = service(data, "none").policies.rollback(domain, actor)
    typer.echo(f"{domain} back on version {v}")


@app.command()
def audit(data: Path = typer.Option(Path("data"))):
    """Verify the audit log's hash chain."""
    from .store import Store

    ok, bad = Store(data / "evcheck.db").verify_audit()
    typer.echo("audit log verified" if ok else f"audit log BROKEN at row {bad}")
    raise typer.Exit(0 if ok else 1)


@keys_app.command("add")
def keys_add(role: str = typer.Option("reviewer", help="intake, reviewer, investigator or admin"),
             data: Path = typer.Option(Path("data"))):
    """Create an API key (shown once) and save it to <data>/keys.json."""
    from .web.app import ROLES

    if role not in ROLES:
        raise typer.BadParameter(f"role must be one of {', '.join(ROLES)}")
    f = data / "keys.json"
    data.mkdir(parents=True, exist_ok=True)
    keys = json.loads(f.read_text()) if f.exists() else {}
    key = "evk_" + secrets.token_urlsafe(24)
    keys[key] = role
    f.write_text(json.dumps(keys, indent=1))
    f.chmod(0o600)
    typer.echo(key)


if __name__ == "__main__":
    sys.exit(app())
