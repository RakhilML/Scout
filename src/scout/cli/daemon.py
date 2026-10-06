"""Long-running Scout: the watch daemon, its login service and logs, and the MCP server."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from collections import deque
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import click
from rich.markup import escape

from scout.app import App
from scout.cli._common import err, out, settings
from scout.files import FileLock
from scout.monitor import service
from scout.monitor.daemon import Daemon
from scout.monitor.watches import WatchBook

_LOG_BYTES = 1_000_000
_LOG_BACKUPS = 3


@click.command()
def daemon() -> None:
    """Run the watches on their schedules until stopped (Ctrl+C)."""
    config = settings()
    _log_to_file(config.log_path)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())  # how service managers stop it
    lock = FileLock(
        config.data_dir / "daemon.lock", wait=False, busy="another scout daemon is already running"
    )
    with lock, App(config) as app:
        scheduler = Daemon(app, WatchBook(config.watches_path))
        err.print(f"Scout daemon started. Log: {config.log_path}. Stop with Ctrl+C.")
        try:
            scheduler.serve(stop)
        except KeyboardInterrupt:
            err.print("Stopped.")


@click.command()
@click.option("-n", "--lines", default=40, show_default=True, help="How many lines to show.")
def logs(lines: int) -> None:
    """Show the end of the daemon's log."""
    path = settings().log_path
    if not path.exists():
        out.print(f"No log yet ({path}). The daemon writes it: scout daemon")
        return
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in deque(handle, maxlen=lines):
            out.print(escape(line.rstrip("\n")), highlight=False)


@click.command()
def prune() -> None:
    """Drop old cached searches and pages, keeping what recent runs read (the daemon does this
    daily)."""
    with App(settings()) as app:
        removed = app.store.prune(now=datetime.now(UTC))
    out.print(f"Removed {removed} old cache entr{'y' if removed == 1 else 'ies'}.")


@click.command(name="mcp")
def mcp_server() -> None:
    """Serve Scout's tools to AI assistants over MCP (stdio)."""
    from scout.server import build  # the mcp package is an optional extra

    with App(settings()) as app:
        build(app).run("stdio")


@click.group(name="service")
def service_group() -> None:
    """Start the daemon automatically when you log in."""


@service_group.command(name="install")
@click.option("--dry-run", is_flag=True, help="Only show what would be done.")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
def install(dry_run: bool, yes: bool) -> None:
    """Start the daemon at login (no administrator rights needed)."""
    plan = _plan()
    _show(plan, install=True)
    if dry_run:
        return
    if not yes:
        click.confirm("Go ahead?", abort=True)
    service.install(plan)
    out.print("Installed. The daemon starts at your next login; start it now with: scout daemon")


@service_group.command(name="uninstall")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
def uninstall(yes: bool) -> None:
    """Stop starting the daemon at login."""
    plan = _plan()
    _show(plan, install=False)
    if not yes:
        click.confirm("Go ahead?", abort=True)
    service.uninstall(plan)
    out.print("Uninstalled.")


def _plan() -> service.ServicePlan:
    config = settings()
    appdata = os.environ.get("APPDATA")
    return service.plan(
        sys.platform,
        python=Path(sys.executable),
        home=Path.home(),
        env_file=config.env_file,
        appdata=Path(appdata) if appdata else None,
    )


def _show(plan: service.ServicePlan, *, install: bool) -> None:
    for path, text in plan.files.items():
        out.print(
            f"[bold]{'write' if install else 'delete'}[/] {escape(str(path))}", soft_wrap=True
        )
        if install:
            out.print(escape(text.rstrip()), highlight=False)
    for command in plan.install if install else plan.uninstall:
        out.print(f"[bold]run[/] {escape(' '.join(command))}")


def _log_to_file(path: Path) -> None:
    """The daemon's progress goes to a rotating file (and to the console, if there is one)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path, maxBytes=_LOG_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger("scout").setLevel(logging.INFO)
