"""What every command shares: the consoles, settings, error handling and logging."""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime
from typing import Any, get_args
from urllib.parse import quote

import click
from rich.console import Console
from rich.markup import escape

from scout.errors import AnswerPending, ScoutError
from scout.research.schema import GoalKind, Recency
from scout.settings import Settings, load_settings

EXIT_ANSWER_PENDING = 75  # EX_TEMPFAIL: run the same command again once the answer exists

KINDS = list(get_args(GoalKind))
RECENCY = list(get_args(Recency))
_UNSAFE_IN_LINKS = re.compile(r"[\s\[\]\x00-\x1f\x7f-\x9f]")

out = Console()
err = Console(stderr=True, soft_wrap=True)  # never break a path in two


class ScoutGroup(click.Group):
    """Turns Scout's expected errors into one-line messages and exit codes, not tracebacks."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except AnswerPending as exc:
            err.print("[yellow]Waiting for the model's answer:[/]")
            # A path to copy: never wrapped or highlighted.
            err.print(str(exc.request_path), soft_wrap=True, markup=False, highlight=False)
            err.print(
                "Write the reply to the answer file it names, then run the same command again."
            )
            ctx.exit(EXIT_ANSWER_PENDING)
        except ScoutError as exc:
            err.print(f"[red]error:[/] {escape(str(exc))}")
            for note in getattr(exc, "__notes__", ()):
                err.print(escape(note))
            ctx.exit(1)


def settings() -> Settings:
    return load_settings()


def when(moment: datetime, *, date_only: bool = False) -> str:
    """A stored (UTC) time as the local time people expect."""
    return moment.astimezone().strftime("%Y-%m-%d" if date_only else "%Y-%m-%d %H:%M")


def linked(text: str, url: str) -> str:
    """*text* (markup) as a terminal hyperlink to *url*, when it is a web page. What could end
    the markup tag or the terminal's link sequence is percent-encoded: a page's address is not
    to be trusted."""
    if not url.startswith(("https://", "http://")):
        return text
    return f"[link={_UNSAFE_IN_LINKS.sub(lambda found: quote(found[0]), url)}]{text}[/link]"


def prepare_streams() -> None:
    # UTF-8 everywhere: on Windows, output redirected to a file or pipe would otherwise be in
    # the legacy ANSI code page (JSON must be UTF-8). Never crash on a character.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def configure_logging(verbose: int) -> None:
    level = logging.WARNING if verbose == 0 else logging.INFO if verbose == 1 else logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    # Third-party libraries only speak up at -vv.
    library_level = logging.DEBUG if verbose >= 2 else logging.WARNING
    for name in (
        "trafilatura",
        "htmldate",
        "courlan",
        "urllib3",
        "primp",
        "ddgs",
        "charset_normalizer",
        "apscheduler",
    ):
        logging.getLogger(name).setLevel(library_level)
