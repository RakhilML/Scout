"""`scout watch`: research goals that re-run on a schedule and say what changed."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import click
from rich.markup import escape
from rich.table import Table

from scout.app import App
from scout.cli._common import KINDS, RECENCY, err, out, settings, when
from scout.monitor.diff import Change
from scout.monitor.feed import FEED_ENTRIES, atom
from scout.monitor.packs import available
from scout.monitor.runner import DEFAULT_RULES, WatchRun, notifier_for, run_watch
from scout.monitor.trends import series, sparkline, write_csv
from scout.monitor.watches import Watch, WatchBook
from scout.store import AlertRecord
from scout.textutil import shorten


@click.group(name="watch")
def watch_group() -> None:
    """Research goals that re-run on a schedule and tell you what changed."""


@watch_group.command(name="add")
@click.argument("name")
@click.argument("goal")
@click.option("--every", help="How often it runs: 30m, 6h, 1d, 1w.  [default: 1d]")
@click.option("--cron", help='Or a cron schedule: "0 9 * * *" is 09:00 every day.')
@click.option(
    "--alert",
    "alerts",
    multiple=True,
    help='When to alert, e.g. "price below 1800 USD" (repeatable).  [default: new, changed]',
)
@click.option(
    "--notify", multiple=True, help="Where alerts go: an Apprise URL, e.g. ntfy://my-topic."
)
@click.option("--kind", type=click.Choice(KINDS), help="Skip classifying the goal.")
@click.option("--recency", type=click.Choice(RECENCY), help="How recent sources must be.")
@click.option("--region", help="Search region, e.g. us-en, in-en, de-de.")
@click.option("-n", "--max-results", type=click.IntRange(1, 20), help="Pages to read per run.")
@click.option("--stop-when-alerted", is_flag=True, help="Pause the watch once it alerts.")
def add(
    name: str,
    goal: str,
    every: str | None,
    cron: str | None,
    alerts: tuple[str, ...],
    notify: tuple[str, ...],
    kind: str | None,
    recency: str | None,
    region: str | None,
    max_results: int | None,
    stop_when_alerted: bool,
) -> None:
    """Watch GOAL under NAME (lowercase letters, digits and dashes)."""
    watch = Watch(
        name=name,
        goal=goal,
        every=every or (None if cron else "1d"),
        cron=cron,
        kind=kind,
        recency=recency,
        region=region,
        max_results=max_results,
        alerts=alerts,
        notify=notify,
        stop_when_alerted=stop_when_alerted,
    )
    notifier_for(watch)  # an unusable notification URL fails now, not at the first alert
    WatchBook(settings().watches_path).add(watch)
    out.print(f"Watching [bold]{name}[/] {_schedule(watch)}. Run it now: scout watch run {name}")


@watch_group.command(name="list")
def list_watches() -> None:
    """List the watches and how each one last ran."""
    config = settings()
    watches = WatchBook(config.watches_path).load()
    if not watches:
        out.print('No watches yet. Try: scout watch add gpu "cheapest RTX 5090" --every 6h')
        return
    table = Table("name", "schedule", "alerts", "last run", "state")
    with App(config) as app:
        for watch in watches:
            runs = app.store.recent_runs(limit=1, watch=watch.name)
            last = f"{when(runs[0].started_at)} ({runs[0].confidence})" if runs else "never"
            table.add_row(
                watch.name,
                _schedule(watch),
                escape(", ".join(watch.alerts or DEFAULT_RULES)),
                last,
                "paused" if watch.paused else "active",
            )
    out.print(table)


@watch_group.command(name="show")
@click.argument("name")
def show(name: str) -> None:
    """Show a watch, the facts it knows, and its latest alerts."""
    config = settings()
    watch = WatchBook(config.watches_path).get(name)
    out.print(f"[bold]{watch.name}[/]: {escape(watch.goal)}")
    out.print(f"  runs {_schedule(watch)}{' [yellow](paused)[/]' if watch.paused else ''}")
    out.print(f"  alerts on: {escape(', '.join(watch.alerts or DEFAULT_RULES))}")
    if watch.notify:
        out.print(f"  notifies: {escape(', '.join(watch.notify))}")
    with App(config) as app:
        facts = app.store.facts(name)
        alerts = app.store.alerts(name, limit=5)
    if facts:
        table = Table("fact", "value", "site", "since", title="What it knows")
        for fact in facts:
            table.add_row(
                escape(shorten(fact.claim, 90)),
                escape(fact.value or ""),
                fact.site,
                when(fact.seen, date_only=True),
            )
        out.print(table)
    _print_alerts(alerts)


@watch_group.command(name="remove")
@click.argument("name")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
def remove(name: str, yes: bool) -> None:
    """Stop watching NAME and forget its facts, prices and alerts (runs stay in the history)."""
    config = settings()
    book = WatchBook(config.watches_path)
    book.get(name)  # an unknown name fails before the question
    if not yes:
        click.confirm(f"Remove {name} with its facts, prices and alerts?", abort=True)
    book.remove(name)
    with App(config) as app:
        app.store.forget(name)
    config.feed_path(name).unlink(missing_ok=True)
    out.print(f"Removed {name}.")


@watch_group.command(name="pause")
@click.argument("name")
def pause(name: str) -> None:
    """Stop running NAME on its schedule (it keeps what it knows)."""
    WatchBook(settings().watches_path).update(name, paused=True)
    out.print(f"Paused {name}.")


@watch_group.command(name="resume")
@click.argument("name")
def resume(name: str) -> None:
    """Run NAME on its schedule again."""
    WatchBook(settings().watches_path).update(name, paused=False)
    out.print(f"Resumed {name}.")


@watch_group.command(name="run")
@click.argument("name")
@click.option("--llm", "llm_spec", help="Override SCOUT_LLM, e.g. exchange:./answers")
def run(name: str, llm_spec: str | None) -> None:
    """Run NAME now, alert on what changed, and report."""
    config = settings()
    book = WatchBook(config.watches_path)
    watch = book.get(name)
    with (
        App(config, llm=llm_spec) as app,
        err.status(f"Running {name}\N{HORIZONTAL ELLIPSIS}", spinner="dots"),
    ):
        outcome = run_watch(app, watch, notifier=notifier_for(watch), book=book)
    _print_run(outcome)


@watch_group.command(name="changes")
@click.argument("name")
@click.option("-n", "--limit", default=20, show_default=True, help="How many alerts to show.")
def changes(name: str, limit: int) -> None:
    """List what NAME alerted on, newest first."""
    config = settings()
    WatchBook(config.watches_path).get(name)
    with App(config) as app:
        _print_alerts(app.store.alerts(name, limit=limit))


@watch_group.command(name="trend")
@click.argument("name")
@click.option(
    "--csv",
    "csv_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Also write every observation to this CSV file.",
)
def trend(name: str, csv_path: Path | None) -> None:
    """How the prices and other numbers NAME follows have moved."""
    config = settings()
    WatchBook(config.watches_path).get(name)
    with App(config) as app:
        observations = app.store.observations(name)
    if not observations:
        out.print("No values yet: prices and other numbers show up here once runs find them.")
        return
    table = Table("value", "in", "first", "last", "low", "high", "change", "history")
    for line in series(observations):
        change = f"{line.change:+.1f}%" if line.change is not None else ""
        table.add_row(
            escape(shorten(line.label, 60)),
            escape(line.measure),
            f"{line.first:,}",
            f"{line.last:,}",
            f"{line.low:,}",
            f"{line.high:,}",
            change,
            sparkline([amount for _, amount in line.points]),
        )
    out.print(table)
    if csv_path is not None:
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            write_csv(observations, handle)
        out.print(
            f"Wrote {len(observations)} observations to {escape(str(csv_path))}", soft_wrap=True
        )


@watch_group.command(name="feed")
@click.argument("name")
def feed(name: str) -> None:
    """Print NAME's alerts as an Atom feed. Every run also rewrites data/feeds/NAME.xml."""
    config = settings()
    watch = WatchBook(config.watches_path).get(name)
    with App(config) as app:
        alerts = app.store.alerts(name, limit=FEED_ENTRIES)
    click.echo(atom(watch, alerts, now=datetime.now(UTC)))


@click.group(name="pack")
def pack_group() -> None:
    """Ready-made watches for common needs: a price, a release, news, a restock."""


@pack_group.command(name="list")
def pack_list() -> None:
    """The packs there are, and what each needs."""
    table = Table("pack", "what it does", "needs", "may take")
    for name, pack in available(_user_packs()).items():
        optional = [param for param in pack.params if param not in pack.required]
        table.add_row(name, escape(pack.description), ", ".join(pack.required), ", ".join(optional))
    out.print(table)
    out.print('Add one: scout pack add price product="RTX 5090" below="1800 USD"')


@pack_group.command(name="add")
@click.argument("name")
@click.argument("values", nargs=-1, metavar="PARAM=VALUE...")
@click.option(
    "--notify", multiple=True, help="Where alerts go: an Apprise URL, e.g. ntfy://my-topic."
)
def pack_add(name: str, values: tuple[str, ...], notify: tuple[str, ...]) -> None:
    """Add the watches of pack NAME, filled in with PARAM=VALUE pairs."""
    packs = available(_user_packs())
    if name not in packs:
        raise click.BadParameter(f"no pack {name!r}; see: scout pack list", param_hint="NAME")
    pairs = dict(_pair(value) for value in values)
    watches = [replace(watch, notify=notify) for watch in packs[name].expand(pairs)]
    if notify:
        notifier_for(watches[0])  # an unusable notification URL fails before anything is added
    book = WatchBook(settings().watches_path)
    taken = {watch.name for watch in book.load()} & {watch.name for watch in watches}
    if taken:
        raise click.UsageError(f"there is already a watch named {', '.join(sorted(taken))}")
    for watch in watches:
        book.add(watch)
        alerts = ", ".join(watch.alerts or DEFAULT_RULES)
        out.print(f"Watching [bold]{watch.name}[/] {_schedule(watch)}: {escape(watch.goal)}")
        out.print(f"  [dim]alerts on: {escape(alerts)}[/]")


def _pair(text: str) -> tuple[str, str]:
    name, equals, value = text.partition("=")
    if not equals or not name.strip():
        raise click.BadParameter(f"{text!r} is not PARAM=VALUE", param_hint="PARAM=VALUE")
    return name.strip(), value


def _user_packs() -> Path:
    return settings().data_dir / "packs"


def _schedule(watch: Watch) -> str:
    return f"every {watch.every}" if watch.every else f'on cron "{watch.cron}"'


def _print_run(outcome: WatchRun) -> None:
    counts = outcome.counts
    summary = ", ".join(f"{counts[change]} {change.value}" for change in Change if counts[change])
    out.print(f"[bold]{outcome.watch}[/] run {outcome.run_id}: {summary or 'no facts found'}")
    if outcome.baseline:
        out.print("  [dim]first run: this is what later runs are compared with[/]")
    if outcome.result.carried_over:
        out.print("  [dim]no page changed, so the model was not asked again[/]")
    for alert in outcome.alerts:
        out.print(f"  [yellow]\N{BELL}[/] {escape(alert.reason)}")
    if outcome.notify_error:
        err.print(
            f"[red]notification failed:[/] {escape(outcome.notify_error)} "
            "(the next run tries again)"
        )
    elif outcome.delivered:
        out.print(f"  sent {outcome.delivered} alert(s)")


def _print_alerts(alerts: Sequence[AlertRecord]) -> None:
    if not alerts:
        out.print("No alerts yet.")
        return
    table = Table("when", "alert", "sent", title="Alerts")
    for alert in alerts:
        if alert.delivered_at is not None:
            sent = when(alert.delivered_at)
        else:
            sent = f"[red]failed:[/] {escape(alert.error)}" if alert.error else ""
        table.add_row(when(alert.created_at), escape(alert.reason), sent)
    out.print(table)
