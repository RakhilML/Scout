"""
Scout CLI — AI-powered web research on a schedule.

Commands:
  run       Run a one-shot research task
  schedule  Run a research task on a repeating schedule
  list      List all scheduled jobs
  stop      Stop (remove) a scheduled job
  pause     Pause a scheduled job
  resume    Resume a paused job
  models    List available models from LM Studio
  check     Verify LM Studio connectivity + model availability
  history   List saved reports in an output folder
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

# ─── WINDOWS ENCODING FIX ────────────────────────────────────────────────────
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich import box
from rich.markdown import Markdown
from rich.rule import Rule

# Ensure Scouts dir is on path for sibling imports
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

console = Console()
err_console = Console(stderr=True)

# ─── LOGGING SETUP ────────────────────────────────────────────────────────────

def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Silence noisy third-party loggers unless verbose
    # Must be set AFTER basicConfig so they override the root level
    if not verbose:
        for noisy in (
            "trafilatura", "trafilatura.core", "trafilatura.utils",
            "trafilatura.htmlprocessing", "trafilatura.settings",
            "apscheduler", "apscheduler.scheduler", "apscheduler.executors.default",
            "httpx", "httpcore", "urllib3",
            "scout.dspy_llm", "LiteLLM", "litellm",
        ):
            logging.getLogger(noisy).setLevel(logging.CRITICAL)


# ─── CLI GROUP ────────────────────────────────────────────────────────────────

@click.group()
@click.option("--verbose", "-v", is_flag=True, default=False, help="Enable debug logging")
@click.pass_context
def main(ctx, verbose: bool):
    """
    \b
    ┌─────────────────────────────────────┐
    │  Scout — AI web research on demand  │
    └─────────────────────────────────────┘
    Searches the web, fetches real content, and uses your local LLM
    to extract exactly what you need — no API keys, no subscriptions.
    """
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    _setup_logging(verbose)

    from config import ensure_dirs
    ensure_dirs()


# ─── RUN COMMAND ─────────────────────────────────────────────────────────────

@main.command()
@click.argument("goal")
@click.option("--output", "-o", default=None,
              help="Folder to save the report (default: from .env or ./reports)")
@click.option("--max-results", "-n", default=None, type=int,
              help="Number of search results to fetch (default: from .env)")
@click.option("--no-save", is_flag=True, default=False,
              help="Print insights only, do not save to file")
@click.pass_context
def run(ctx, goal: str, output: str | None, max_results: int | None, no_save: bool):
    """
    Run a one-shot research task and save a report.

    \b
    Examples:
      scout run "latest news on LLMs"
      scout run "lowest price RTX 4090" --output ./prices
      scout run "what's new in Python 3.13" --max-results 8
      scout run "BTC price today" --no-save
    """
    from config import load_config
    from searcher import gather, gather_stats
    from llm import extract_insights, check_lm_studio
    from reporter import save_report
    from models import ScoutReport
    from exceptions import ConfigError, SearchError, LLMError, LLMConnectionError

    verbose = ctx.obj.get("verbose", False)

    # ── Load config ──
    try:
        cfg = load_config()
    except ConfigError as e:
        err_console.print(f"[bold red]Config error:[/] {e}")
        sys.exit(1)

    if max_results:
        cfg.max_results = max_results
    out_dir = output or cfg.output_dir

    # ── Header ──
    console.print()
    console.print(Panel.fit(
        f"[bold cyan]Goal:[/] {goal}\n"
        f"[dim]Model:[/] {cfg.lm_studio_model}  "
        f"[dim]Max results:[/] {cfg.max_results}  "
        f"[dim]Output:[/] {out_dir}",
        title="[bold]Scout Run[/]",
        border_style="cyan",
    ))
    console.print()

    start_time = time.monotonic()

    # ── Step 1: Search ──
    pages = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Searching DuckDuckGo...", total=None)
        try:
            pages = gather(goal, cfg)
        except SearchError as e:
            err_console.print(f"[bold red]Search failed:[/] {e}")
            sys.exit(1)
        progress.update(task, description=f"Found {len(pages)} results")

    # ── Search stats ──
    stats = gather_stats(pages)
    _print_fetch_table(pages, console)

    if stats["successful"] == 0:
        console.print("[yellow]Warning:[/] All pages returned snippets only — "
                      "insights may be limited.")
    console.print()

    # ── Step 2: LLM ──
    insights = ""
    structured = None
    extraction_method = "raw"
    confidence = "medium"

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task(
            f"Asking [bold]{cfg.lm_studio_model}[/]...",
            total=None,
        )
        try:
            # llm.py tries DSPy -> Instructor -> raw internally
            # We also try to get the structured object directly for richer output
            try:
                from dspy_llm import extract_insights_dspy
                structured = extract_insights_dspy(goal, pages, cfg)
                insights = structured.to_markdown()
                extraction_method = "dspy"
                confidence = structured.confidence
            except Exception:
                insights = extract_insights(goal, pages, cfg)
                extraction_method = "raw"

        except LLMConnectionError as e:
            err_console.print(f"[bold red]LM Studio connection error:[/]\n{e}")
            sys.exit(1)
        except LLMError as e:
            err_console.print(f"[bold red]LLM error:[/] {e}")
            sys.exit(1)

    elapsed = time.monotonic() - start_time

    # ── Print insights ──
    console.print(Rule(f"[bold green]Insights[/] [dim]({extraction_method})[/dim]"))
    console.print()
    console.print(Markdown(insights))
    console.print()
    console.print(Rule())

    # ── Save report ──
    if not no_save:
        from reporter import save_report as _save_report
        report = ScoutReport(
            goal=goal,
            insights=insights,
            pages=pages,
            model_used=cfg.lm_studio_model,
            run_duration_seconds=elapsed,
            structured_insights=structured,
            extraction_method=extraction_method,
            confidence=confidence,
        )
        try:
            saved = _save_report(report, out_dir)
            console.print()
            console.print(f"[bold green]Report saved:[/]")
            console.print(f"  Markdown : [link]{saved.md_path}[/link]")
            console.print(f"  JSON     : [link]{saved.json_path}[/link]")
        except Exception as e:
            err_console.print(f"[yellow]Warning:[/] Could not save report: {e}")
    else:
        console.print("[dim]--no-save: report not written to disk[/dim]")

    console.print()
    console.print(f"[dim]Done in {elapsed:.1f}s[/dim]")


# ─── SCHEDULE COMMAND ────────────────────────────────────────────────────────

@main.command()
@click.argument("goal")
@click.option("--every", default=None, metavar="INTERVAL",
              help="Repeat interval: 30m, 6h, 1d, 1w")
@click.option("--cron", default=None, metavar="EXPR",
              help='Cron expression e.g. "0 9 * * *"')
@click.option("--output", "-o", default=None,
              help="Folder to save reports (default: from .env or ./reports)")
@click.option("--run-now", is_flag=True, default=False,
              help="Run immediately in addition to the schedule")
@click.pass_context
def schedule(ctx, goal: str, every: str | None, cron: str | None,
             output: str | None, run_now: bool):
    """
    Schedule recurring research. Blocks until Ctrl-C.

    \b
    Examples:
      scout schedule "AI news" --every 6h
      scout schedule "RTX 4090 price" --cron "0 8 * * *" --output ./prices
      scout schedule "BTC updates" --every 30m --run-now
    """
    from config import load_config
    from scheduler import add_job, run_scheduler_loop, parse_trigger, describe_trigger
    from exceptions import ConfigError, SchedulerError

    if not every and not cron:
        raise click.UsageError("Provide --every INTERVAL or --cron EXPR\n"
                               "Example: --every 6h  or  --cron \"0 9 * * *\"")
    if every and cron:
        raise click.UsageError("Use either --every or --cron, not both")

    try:
        cfg = load_config()
    except ConfigError as e:
        err_console.print(f"[bold red]Config error:[/] {e}")
        sys.exit(1)

    out_dir = output or cfg.output_dir

    # Validate trigger before registering
    try:
        parse_trigger(every, cron)
    except SchedulerError as e:
        err_console.print(f"[bold red]Invalid schedule:[/] {e}")
        sys.exit(1)

    # Register job
    try:
        job_id = add_job(goal, out_dir, every, cron, cfg.data_dir)
    except SchedulerError as e:
        err_console.print(f"[bold red]Scheduler error:[/] {e}")
        sys.exit(1)

    trigger_desc = describe_trigger(every, cron)

    console.print()
    console.print(Panel.fit(
        f"[bold cyan]Goal:[/]    {goal}\n"
        f"[bold cyan]Schedule:[/] {trigger_desc}\n"
        f"[bold cyan]Output:[/]  {out_dir}\n"
        f"[bold cyan]Job ID:[/]  [dim]{job_id}[/dim]",
        title="[bold]Scout Scheduled[/]",
        border_style="green",
    ))
    console.print()

    # Optional immediate run
    if run_now:
        console.print("[bold]Running immediately (--run-now)...[/]")
        ctx.invoke(run, goal=goal, output=out_dir, max_results=None, no_save=False)
        console.print()

    console.print("[bold green]Scheduler running.[/] Press [bold]Ctrl-C[/] to stop.")
    console.print(f"[dim]Reports will be saved to: {out_dir}[/dim]")
    console.print()

    run_scheduler_loop(cfg.data_dir)


# ─── LIST COMMAND ────────────────────────────────────────────────────────────

@main.command(name="list")
@click.pass_context
def list_jobs_cmd(ctx):
    """List all scheduled jobs."""
    from config import load_config
    from scheduler import list_jobs
    from exceptions import ConfigError

    try:
        cfg = load_config()
    except ConfigError as e:
        err_console.print(f"[bold red]Config error:[/] {e}")
        sys.exit(1)

    jobs = list_jobs(cfg.data_dir)

    if not jobs:
        console.print("[dim]No scheduled jobs.[/dim]")
        console.print("Run [bold]scout schedule \"your goal\" --every 6h[/bold] to create one.")
        return

    table = Table(
        title=f"Scheduled Jobs ({len(jobs)})",
        box=box.ROUNDED,
        show_lines=True,
        border_style="cyan",
    )
    table.add_column("ID", style="dim", width=10)
    table.add_column("Goal", style="bold", max_width=40)
    table.add_column("Trigger", style="green")
    table.add_column("Next Run", style="yellow")
    table.add_column("Output", style="dim", max_width=30)

    for j in jobs:
        short_id = j["id"][:8]
        table.add_row(
            short_id,
            j["prompt"],
            j["trigger"],
            j["next_run"],
            j["output_dir"],
        )

    console.print()
    console.print(table)
    console.print()
    console.print("[dim]Use [bold]scout stop <ID>[/bold] to remove a job[/dim]")


# ─── STOP COMMAND ────────────────────────────────────────────────────────────

@main.command()
@click.argument("job_id")
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation")
@click.pass_context
def stop(ctx, job_id: str, yes: bool):
    """
    Stop (remove) a scheduled job by ID or short prefix.

    \b
    Example:
      scout stop a3f9b1c2
      scout stop a3f9b1c2 --yes
    """
    from config import load_config
    from scheduler import list_jobs, remove_job
    from exceptions import ConfigError, JobNotFoundError, SchedulerError

    try:
        cfg = load_config()
    except ConfigError as e:
        err_console.print(f"[bold red]Config error:[/] {e}")
        sys.exit(1)

    # Resolve prefix and get job details in a single DB read
    jobs = list_jobs(cfg.data_dir)
    matches = [j for j in jobs if j["id"].startswith(job_id)]
    if not matches:
        err_console.print(f"[bold red]Error:[/] No job found matching: {job_id!r}")
        sys.exit(1)
    if len(matches) > 1:
        ids = ", ".join(j["id"][:8] for j in matches)
        err_console.print(f"[bold red]Error:[/] Ambiguous prefix — matches: {ids}")
        sys.exit(1)
    job = matches[0]
    full_id = job["id"]

    if job:
        console.print(f"\nJob: [bold]{job['prompt']}[/bold]")
        console.print(f"Trigger: {job['trigger']}")
        console.print()

    if not yes:
        click.confirm("Remove this job?", default=False, abort=True)

    try:
        remove_job(full_id, cfg.data_dir)
        console.print(f"[bold green]Stopped job:[/] {full_id[:8]}")
    except (JobNotFoundError, SchedulerError) as e:
        err_console.print(f"[bold red]Error:[/] {e}")
        sys.exit(1)


# ─── PAUSE / RESUME ──────────────────────────────────────────────────────────

@main.command()
@click.argument("job_id")
@click.pass_context
def pause(ctx, job_id: str):
    """Pause a scheduled job (keeps it saved, stops it running)."""
    from config import load_config
    from scheduler import pause_job, resolve_job_id
    from exceptions import ConfigError, JobNotFoundError, SchedulerError

    try:
        cfg = load_config()
        full_id = resolve_job_id(job_id, cfg.data_dir)
        pause_job(full_id, cfg.data_dir)
        console.print(f"[yellow]Paused job:[/] {full_id[:8]}")
    except (ConfigError, JobNotFoundError, SchedulerError) as e:
        err_console.print(f"[bold red]Error:[/] {e}")
        sys.exit(1)


@main.command()
@click.argument("job_id")
@click.pass_context
def resume(ctx, job_id: str):
    """Resume a paused job."""
    from config import load_config
    from scheduler import resume_job, resolve_job_id
    from exceptions import ConfigError, JobNotFoundError, SchedulerError

    try:
        cfg = load_config()
        full_id = resolve_job_id(job_id, cfg.data_dir)
        resume_job(full_id, cfg.data_dir)
        console.print(f"[bold green]Resumed job:[/] {full_id[:8]}")
    except (ConfigError, JobNotFoundError, SchedulerError) as e:
        err_console.print(f"[bold red]Error:[/] {e}")
        sys.exit(1)


# ─── MODELS COMMAND ───────────────────────────────────────────────────────────

@main.command()
@click.pass_context
def models(ctx):
    """List available models from your LM Studio instance."""
    from config import load_config
    from llm import list_available_models
    from exceptions import ConfigError

    try:
        cfg = load_config()
    except ConfigError as e:
        err_console.print(f"[bold red]Config error:[/] {e}")
        sys.exit(1)

    console.print(f"[dim]Fetching models from {cfg.lm_studio_base_url}...[/dim]")
    model_ids = list_available_models(cfg)

    if not model_ids:
        err_console.print("[bold red]Could not fetch models.[/] Is LM Studio running?")
        sys.exit(1)

    table = Table(title="Available Models", box=box.SIMPLE, border_style="cyan")
    table.add_column("#", style="dim", width=4)
    table.add_column("Model ID", style="bold")
    table.add_column("", width=10)

    for i, mid in enumerate(model_ids, 1):
        active = "[bold green]← active[/bold green]" if mid == cfg.lm_studio_model else ""
        table.add_row(str(i), mid, active)

    console.print()
    console.print(table)
    console.print()
    console.print(f"[dim]Set [bold]LM_STUDIO_MODEL[/bold] in .env to use a different model[/dim]")


# ─── CHECK COMMAND ────────────────────────────────────────────────────────────

@main.command()
@click.pass_context
def check(ctx):
    """Verify LM Studio is reachable and the configured model is loaded."""
    from config import load_config
    from llm import check_lm_studio
    from exceptions import ConfigError

    console.print()

    try:
        cfg = load_config()
    except ConfigError as e:
        err_console.print(f"[bold red]Config error:[/] {e}")
        sys.exit(1)

    console.print(Panel.fit(
        cfg.describe(),
        title="[bold]Current Config[/]",
        border_style="dim",
    ))
    console.print()

    with Progress(
        SpinnerColumn(),
        TextColumn("Checking LM Studio..."),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("", total=None)
        ok, msg = check_lm_studio(cfg)

    if ok:
        console.print(f"[bold green]✓[/] {msg}")
        console.print(f"[bold green]✓[/] LM Studio reachable at {cfg.lm_studio_base_url}")
        console.print()
        console.print("[bold green]All good! Run:[/]")
        console.print(f'  [bold]scout run "your research goal"[/bold]')
    else:
        console.print(f"[bold red]✗[/] {msg}")
        console.print()
        console.print("[yellow]Troubleshooting:[/]")
        console.print("  1. Make sure LM Studio is running")
        console.print("  2. Load a model in LM Studio")
        console.print(f"  3. Verify LM_STUDIO_BASE_URL={cfg.lm_studio_base_url} is correct")
        console.print(f"  4. Run [bold]scout models[/bold] to see available models")
        sys.exit(1)


# ─── HISTORY COMMAND ─────────────────────────────────────────────────────────

@main.command()
@click.option("--output", "-o", default=None,
              help="Reports folder to scan (default: from .env or ./reports)")
@click.option("--limit", "-n", default=20, show_default=True,
              help="Number of recent reports to show")
@click.pass_context
def history(ctx, output: str | None, limit: int):
    """List recent saved reports."""
    from config import load_config
    from exceptions import ConfigError
    import json as _json

    try:
        cfg = load_config()
    except ConfigError as e:
        err_console.print(f"[bold red]Config error:[/] {e}")
        sys.exit(1)

    out_dir = Path(output or cfg.output_dir).expanduser()
    if not out_dir.is_absolute():
        out_dir = Path.cwd() / out_dir

    if not out_dir.exists():
        console.print(f"[dim]No reports directory found at {out_dir}[/dim]")
        return

    md_files = sorted(out_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)

    if not md_files:
        console.print(f"[dim]No reports found in {out_dir}[/dim]")
        return

    table = Table(
        title=f"Recent Reports in {out_dir}",
        box=box.ROUNDED,
        border_style="cyan",
        show_lines=False,
    )
    table.add_column("Date", style="dim", width=18)
    table.add_column("Goal", style="bold", max_width=50)
    table.add_column("Size", style="dim", width=8, justify="right")
    table.add_column("File", style="dim", max_width=40)

    for md in md_files[:limit]:
        # Try to read goal from JSON sidecar
        goal = "—"
        json_path = md.with_suffix(".json")
        if json_path.exists():
            try:
                data = _json.loads(json_path.read_text(encoding="utf-8"))
                goal = data.get("goal", "—")[:60]
            except Exception:
                pass

        size_kb = md.stat().st_size / 1024
        mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(md.stat().st_mtime))
        table.add_row(mtime, goal, f"{size_kb:.1f}KB", md.name)

    console.print()
    console.print(table)
    console.print(f"\n[dim]Showing {min(limit, len(md_files))} of {len(md_files)} reports[/dim]")


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _print_fetch_table(pages, con: Console) -> None:
    """Print a compact table of fetch results."""
    from models import FetchStatus

    status_styles = {
        FetchStatus.SUCCESS: ("✓", "green"),
        FetchStatus.SNIPPET_ONLY: ("~", "yellow"),
        FetchStatus.BLOCKED: ("✗", "red"),
        FetchStatus.TIMEOUT: ("⏱", "red"),
        FetchStatus.ERROR: ("✗", "red"),
    }

    table = Table(box=box.SIMPLE, show_header=True, border_style="dim")
    table.add_column("#", style="dim", width=3)
    table.add_column("", width=3)
    table.add_column("Title", max_width=45)
    table.add_column("Chars", justify="right", style="dim", width=7)

    for i, page in enumerate(pages, 1):
        sym, style = status_styles.get(page.fetch_status, ("?", "dim"))
        chars = f"{page.char_count:,}" if page.char_count else "—"
        title = page.title[:45] if page.title else page.url[:45]
        table.add_row(str(i), f"[{style}]{sym}[/{style}]", title, chars)

    con.print(table)


# ─── ENTRY POINT ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
