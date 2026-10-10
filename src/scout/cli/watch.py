"""`scout watch`: research goals, texts whose claims are fact-checked, or texts and pages whose
citations are audited, that re-run on a schedule and say what changed."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import click
from rich.markup import escape
from rich.table import Table

from scout.app import App
from scout.cli._common import KINDS, RECENCY, err, linked, out, settings, when
from scout.monitor.claims import (
    PAGE,
    READ,
    departed,
    evidence_for,
    is_ruling,
    noticed_for,
    ruling_keys,
)
from scout.monitor.diff import Change, Delta, Fact
from scout.monitor.feed import FEED_ENTRIES, atom
from scout.monitor.packs import available
from scout.monitor.rules import dead_reason
from scout.monitor.runner import WatchRun, notifier_for, remedy, rules_of, run_watch
from scout.monitor.trends import series, sparkline, write_csv
from scout.monitor.watches import Watch, WatchBook
from scout.research.factcheck import CITED_LABELS, NOT_JUDGED, UNREADABLE
from scout.research.results import ClaimCheck, Ruling, RunResult
from scout.store import AlertRecord
from scout.textutil import shorten
from scout.web.archive import Copy

SHOWN = 20  # claims, or pages, a citation watch's run lists at most
_LABELS = (*CITED_LABELS.values(), UNREADABLE, NOT_JUDGED)


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
@click.option(
    "-n",
    "--max-results",
    type=click.IntRange(1, 20),
    help="Pages to read per run (per claim with --check, 3 by default).",
)
@click.option("--stop-when-alerted", is_flag=True, help="Pause the watch once it alerts.")
@click.option(
    "--check",
    is_flag=True,
    help="GOAL is a short text of claims: fact-check it on every run and alert when a claim's "
    "ruling changes.",
)
@click.option(
    "--cited",
    is_flag=True,
    help="GOAL is a web address or a text that cites pages: audit its citations on every run and "
    "alert when a cited page stops backing a claim or goes dead.",
)
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
    check: bool,
    cited: bool,
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
        check=check or cited,
        cited=cited,
    )
    notifier_for(watch)  # an unusable notification URL fails now, not at the first alert
    WatchBook(settings().watches_path).add(watch)
    out.print(
        f"Watching [bold]{name}[/] {_schedule(watch)}{_claims(watch)}. "
        f"Run it now: scout watch run {name}"
    )


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
                escape(", ".join(rules_of(watch))),
                last,
                "paused" if watch.paused else "active",
            )
    out.print(table)


@watch_group.command(name="show")
@click.argument("name")
def show(name: str) -> None:
    """Show a watch, the facts (or a claim watch's rulings) it knows, and its latest alerts."""
    config = settings()
    watch = WatchBook(config.watches_path).get(name)
    out.print(f"[bold]{watch.name}[/]{_claims(watch)}: {escape(watch.label)}")
    out.print(f"  runs {_schedule(watch)}{' [yellow](paused)[/]' if watch.paused else ''}")
    out.print(f"  alerts on: {escape(', '.join(rules_of(watch)))}")
    if watch.notify:
        out.print(f"  notifies: {escape(', '.join(watch.notify))}")
    with App(config) as app:
        facts = app.store.facts(name)
        alerts = app.store.alerts(name, limit=5)
        last = app.store.last_runs(name) if watch.cited else []
    if watch.cited:
        _print_citations(facts, last[0][1] if last else None)
    elif watch.check:
        _print_rulings(facts)
    elif facts:
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
    running = f"Running {name}\N{HORIZONTAL ELLIPSIS}"
    with App(config, llm=llm_spec) as app, err.status(running, spinner="dots") as status:
        outcome = run_watch(
            app,
            watch,
            notifier=notifier_for(watch),
            book=book,
            progress=lambda step: status.update(f"{running} {step}"),
        )
    if outcome.is_cited:
        _print_audit(outcome)
    else:
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
        alerts = ", ".join(rules_of(watch))
        out.print(f"Watching [bold]{watch.name}[/] {_schedule(watch)}: {escape(watch.label)}")
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


def _claims(watch: Watch) -> str:
    return " (citations)" if watch.cited else " (claims)" if watch.check else ""


def _print_rulings(facts: Sequence[Fact]) -> None:
    rulings = [fact for fact in facts if is_ruling(fact)]
    if not rulings:
        return
    table = Table("claim", "ruling", "since", "quotes", "noticed", title="What it knows")
    for fact in rulings:
        evidence = evidence_for(fact.key, facts)
        noticed = sum(1 for quote in evidence if quote.noticed)
        table.add_row(
            escape(shorten(fact.claim, 90)),
            fact.value or "",
            when(fact.seen, date_only=True),
            str(len(evidence) - noticed),
            str(noticed),
        )
    out.print(table)


def _print_run(outcome: WatchRun) -> None:
    counts = outcome.counts
    if outcome.is_check:
        checked = len(outcome.rulings)
        moved = [f"{counts[c]} {c.value}" for c in (Change.CHANGED, Change.NOTICED) if counts[c]]
        summary = ", ".join([f"{checked} claim{'s' if checked != 1 else ''} checked", *moved])
    else:
        summary = ", ".join(f"{counts[c]} {c.value}" for c in Change if counts[c])
    out.print(f"[bold]{outcome.watch}[/] run {outcome.run_id}: {summary or 'no facts found'}")
    if outcome.baseline:
        out.print("  [dim]first run: this is what later runs are compared with[/]")
    if outcome.result.carried_over:
        out.print("  [dim]no page changed, so the model was not asked again[/]")
    for alert in outcome.alerts:
        out.print(f"  [yellow]\N{BELL}[/] {escape(alert.reason)}")
    for number, ruling in enumerate(outcome.rulings, start=1):
        standing = ruling.change is Change.CHANGED  # what the pages also say, now it moved
        noticed = noticed_for(ruling.fact.key, outcome.deltas, standing=standing)
        _print_ruling(number, ruling, departed(ruling, outcome.deltas), noticed)
    _print_delivery(outcome)


def _print_delivery(outcome: WatchRun) -> None:
    if outcome.notify_error:
        err.print(
            f"[red]notification failed:[/] {escape(outcome.notify_error)} "
            "(the next run tries again)"
        )
    elif outcome.delivered:
        out.print(f"  sent {outcome.delivered} alert(s)")


def _print_ruling(
    number: int, delta: Delta, gone: bool, noticed: Sequence[Fact], label: str | None = None
) -> None:
    fact = delta.fact
    if label is None:
        label = (fact.value or "").capitalize()
        if delta.change is Change.CHANGED and delta.previous is not None:
            label += f" (was {delta.previous.value})"
    out.print(f"  {number}. {label}: {escape(fact.claim)}")
    if fact.quote and delta.change in (Change.NEW, Change.CHANGED):
        quote, site = escape(shorten(fact.quote, 200)), linked(fact.site, fact.link)
        if gone:
            out.print(f'     no longer on {site}: "{quote}"')
        else:
            out.print(f'     "{quote}" ({site})')
    for seen in noticed:  # the model's reading of pages that did not change: never alerted on
        quote = escape(shorten(seen.quote, 200))
        out.print(f'     [dim]noticed, not alerted: {seen.value} "{quote}" ({seen.site})[/]')


def _print_audit(outcome: WatchRun) -> None:
    """A citation watch's run: on the first, its labels and the claims not backed; later, what
    moved, and the claims that did. At most SHOWN of them: an audit may have hundreds."""
    result, counts = outcome.result, outcome.counts
    checked = f"{_counted(len(outcome.rulings), 'claim')} checked"
    same = not any(_state(page) for page in outcome.pages)  # no cited page went dead or back
    if outcome.baseline:
        summary = f"{checked}: {_tally(d.fact.value for d in outcome.rulings) or 'none'}"
        backed = CITED_LABELS[Ruling.SUPPORTED]
        shown = [d for d in outcome.rulings if d.fact.value != backed]
    else:
        judged = sum(1 for claim in result.claims if not claim.kept and _read(claim, result))
        kept = len(result.claims) - judged
        if same and all(claim.kept for claim in result.claims):
            checked += f" ({kept} kept: their pages read as before)"
        else:
            parts = [f"{kept} kept" if kept else "", f"{judged} judged" if judged else ""]
            checked += f" ({', '.join(filter(None, parts))})"
        moved = {"changed": counts[Change.CHANGED], "new": counts[Change.NEW]}
        moved["left the text"] = outcome.left
        for state, count in Counter(_state(page) for page in outcome.pages).items():
            if state:
                moved[f"{'cited page' if count == 1 else 'cited pages'} {state}"] = count
        summary = ", ".join(f"{count} {what}" for what, count in moved.items() if count)
        summary = f"{checked}: {summary or 'no change'}"
        shown = [d for d in outcome.rulings if d.change is Change.CHANGED]
        shown += [d for d in outcome.rulings if d.change is Change.NEW]
    out.print(f"[bold]{outcome.watch}[/] run {outcome.run_id}: {escape(summary)}")
    if outcome.baseline:
        out.print("  [dim]first run: this is what later runs are compared with[/]")
    missed = sum(1 for page in outcome.pages if page.fact.missed and page.fact.value == READ)
    if missed:
        pages = "page" if missed == 1 else "pages"
        out.print(
            f"  [dim]{missed} cited {pages} could not be read this time "
            "(not alerted unless it fails again)[/]"
        )
    elif not outcome.baseline and not outcome.asked and same:
        out.print("  [dim]no cited page changed, so the model was not asked again[/]")
    for alert in outcome.alerts:
        out.print(f"  [yellow]\N{BELL}[/] {escape(alert.reason)}")
        if _state(alert.delta) == "dead":
            if alert.delta.fact.quote:
                out.print(f'     it said: "{escape(shorten(alert.delta.fact.quote, 200))}"')
            _print_copy(outcome.copies.get(alert.delta.fact.key))
    alerted = {alert.delta.fact.key for alert in outcome.alerts}
    for page in [p for p in outcome.pages if _state(p) and p.fact.key not in alerted][:SHOWN]:
        state, fact = _state(page), page.fact
        if state == "dead":
            told = remedy(outcome.copies[fact.key]) if fact.key in outcome.copies else ""
            out.print(f"  {escape(dead_reason(fact, told))}")
            _print_copy(outcome.copies.get(fact.key))
        else:
            out.print(f"  {escape(f'{state}: {fact.url}, cited for: {fact.claim}')}")
    numbered = _numbered(result)
    for ruling in shown[:SHOWN]:
        number, claim = numbered[ruling.fact.key]
        label = (ruling.fact.value or "").capitalize()
        if ruling.change is Change.NEW and not outcome.baseline:
            label = f"New, {ruling.fact.value}"
        elif ruling.change is Change.CHANGED and ruling.previous is not None:
            label += f" (was {ruling.previous.value})"
        label += " " + "".join(f"[{n}]" for n in claim.pages)
        _print_ruling(number, ruling, departed(ruling, outcome.deltas), (), escape(label))
        if ruling.fact.value in (UNREADABLE, NOT_JUDGED):
            for problem in claim.problems:
                out.print(f"     {escape(problem)}")
    if len(shown) > SHOWN:
        out.print(f"  and {len(shown) - SHOWN} more: scout show {outcome.run_id}")
    _print_delivery(outcome)


def _print_copy(copy: Copy | None) -> None:
    if copy is not None:
        out.print(f"     archived copy: {linked(escape(copy.link), copy.link)}", soft_wrap=True)


def _print_citations(facts: Sequence[Fact], last: RunResult | None) -> None:
    """What a citation watch knows: its claims' labels, those not backed with the pages they
    cite (as its last run numbered them), and the cited pages it could not read."""
    rulings = [fact for fact in facts if is_ruling(fact)]
    if not rulings:
        return
    out.print(f"  {_counted(len(rulings), 'claim')}: {_tally(f.value for f in rulings)}")
    cites = {}
    if last is not None:
        for key, (_, claim) in _numbered(last).items():
            pages = filter(None, map(last.source, claim.pages))
            cites[key] = ", ".join(f"[{page.index}] {page.site}" for page in pages)
    backed = CITED_LABELS[Ruling.SUPPORTED]
    table = Table("label", "claim", "cites", "since", title="Not backed")
    for fact in rulings:
        if fact.value != backed:
            table.add_row(
                fact.value or "",
                escape(shorten(fact.claim, 90)),
                escape(cites.get(fact.key, "")),
                when(fact.seen, date_only=True),
            )
    if table.row_count:
        out.print(table)
    table = Table("page", "why", "since", title="Cited pages that could not be read")
    for fact in facts:
        if fact.key.startswith(PAGE) and fact.value != READ:
            table.add_row(
                escape(fact.url), escape(fact.value or ""), when(fact.seen, date_only=True)
            )
    if table.row_count:
        out.print(table)


def _numbered(result: RunResult) -> dict[str, tuple[int, ClaimCheck]]:
    """Each ruling key of a check, with the number (from 1) and the claim it was first given."""
    numbered: dict[str, tuple[int, ClaimCheck]] = {}
    for number, (key, claim) in enumerate(
        zip(ruling_keys(result), result.claims, strict=True), start=1
    ):
        numbered.setdefault(key, (number, claim))
    return numbered


def _read(claim: ClaimCheck, result: RunResult) -> bool:
    """Some page the claim cites could be read: it was judged then, or now."""
    return any((page := result.source(n)) and not page.snippet_only for n in claim.pages)


def _state(page: Delta) -> str:
    """How a cited page's state moved: "dead", "back", "read at last" (unreadable until now),
    or not at all ("")."""
    if not page.fact.key.startswith(PAGE) or page.change is not Change.CHANGED:
        return ""
    if page.fact.value != READ:
        return "dead"
    return "back" if page.previous is not None and page.previous.missed else "read at last"


def _tally(labels: Iterable[str | None]) -> str:
    counted = Counter(labels)
    return ", ".join(f"{counted[label]} {label}" for label in _LABELS if counted[label])


def _counted(count: int, thing: str) -> str:
    return f"{count} {thing}{'' if count == 1 else 's'}"


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
        table.add_row(when(alert.created_at), linked(escape(alert.reason), alert.link), sent)
    out.print(table)
