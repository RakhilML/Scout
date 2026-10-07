"""Command-line interface."""

from __future__ import annotations

import click

from scout import __version__
from scout.cli import daemon, research, watch
from scout.cli._common import EXIT_ANSWER_PENDING, ScoutGroup, configure_logging, prepare_streams

__all__ = ["EXIT_ANSWER_PENDING", "main"]


@click.group(
    cls=ScoutGroup,
    context_settings={"max_content_width": 120, "help_option_names": ["-h", "--help"]},
)
@click.version_option(__version__, prog_name="scout")
@click.option("-v", "--verbose", count=True, help="-v for progress, -vv for debug output.")
def main(verbose: int) -> None:
    """Scout: private, verifiable web research and monitoring with your own LLM."""
    prepare_streams()
    configure_logging(verbose)


for _command in (
    research.run,
    research.factcheck,
    research.ask,
    research.rate,
    research.ratings,
    research.sites,
    research.check,
    research.models,
    research.history,
    research.show,
    research.replay,
    research.export,
    research.eval_group,
    watch.watch_group,
    watch.pack_group,
    daemon.daemon,
    daemon.logs,
    daemon.prune,
    daemon.service_group,
    daemon.mcp_server,
):
    main.add_command(_command)
