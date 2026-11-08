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


def service(data: Path, models: str, truth_root: Path | None = None):
    from .service import Service

    return Service(data, make_models(models, truth_root))


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
def worker(data: Path = typer.Option(Path("data")), models: str = typer.Option("real", help=MODELS_HELP)):
    """Process queued cases until stopped (run several for more throughput; they share the queue safely)."""
    svc = service(data, models)
    typer.echo(f"worker started on {data} (models: {models}); Ctrl+C to stop", err=True)
    try:
        svc.run_worker()
    except KeyboardInterrupt:
        svc.stop()


@app.command()
def serve(data: Path = typer.Option(Path("data")), host: str = "127.0.0.1", port: int = 8000,
          models: str = typer.Option("none", help=MODELS_HELP), workers: int = typer.Option(1, help="In-process worker threads (0: none)"),
          truth_root: Optional[Path] = typer.Option(None, hidden=True)):
    """Run the reviewer UI and REST API (with in-process workers)."""
    import uvicorn

    from .web.app import create_app, load_keys

    if not load_keys(data) and host not in ("127.0.0.1", "localhost", "::1"):
        typer.echo("Refusing to listen on a network address without API keys. Add one with `evcheck keys add`.", err=True)
        raise typer.Exit(2)
    svc = service(data, models, truth_root)
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
        serve(data=data, host="127.0.0.1", port=port, models=models, workers=1, truth_root=src)


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
