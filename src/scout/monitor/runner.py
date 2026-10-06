"""Run a watch once: research, compare with what it knew, raise alerts, deliver them, and
refresh its feed."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote

from apscheduler.triggers.interval import IntervalTrigger

from scout.app import App
from scout.errors import NotifyError, ScoutError
from scout.files import FileLock
from scout.monitor.diff import Change, Delta, diff
from scout.monitor.feed import FEED_ENTRIES, write_feed
from scout.monitor.notify import AppriseNotifier, Notifier
from scout.monitor.rules import Trigger, cleared, parse_rule, triggers
from scout.monitor.watches import Watch, WatchBook, trigger
from scout.research.results import RunResult, Source
from scout.store import AlertRecord
from scout.textutil import clean, shorten

log = logging.getLogger(__name__)

# Slack reads <!channel> and <url|text>; Markdown targets read [text](url).
_INERT = str.maketrans(
    {
        "<": "\N{SINGLE LEFT-POINTING ANGLE QUOTATION MARK}",
        ">": "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}",
        "[": "(",
        "]": ")",
    }
)
_URL_SAFE = ":/?#@!$&'()*+,;=%~"

# A watch without rules still tells you when something appears or changes.
DEFAULT_RULES = ("new", "changed")
# Earlier runs that show how the pages looked before: a page missing from the last run's search
# results is still known from the run before it.
EARLIER_RUNS = 5
# Alerts that could not be delivered are retried by later runs for this long (at least the
# next two runs).
REDELIVERY_WINDOW = timedelta(days=3)


@dataclass(frozen=True, slots=True)
class WatchRun:
    watch: str
    run_id: int
    result: RunResult
    deltas: tuple[Delta, ...]
    alerts: tuple[Trigger, ...]  # raised by this run
    baseline: bool  # the watch's first run
    delivered: int = 0  # alerts sent in this run's notification, earlier undelivered ones included
    notify_error: str | None = None

    @property
    def counts(self) -> Counter[Change]:
        return Counter(delta.change for delta in self.deltas)


def notifier_for(watch: Watch) -> Notifier | None:
    return AppriseNotifier(watch.notify) if watch.notify else None


def run_watch(
    app: App, watch: Watch, *, notifier: Notifier | None = None, book: WatchBook | None = None
) -> WatchRun:
    """Run *watch* once; one run of a watch at a time. Given its *book*, a watch that stops when
    alerted is paused."""
    lock = FileLock(
        app.settings.data_dir / "locks" / f"{watch.name}.lock",
        wait=False,
        busy=f"watch {watch.name!r} is already running",
    )
    with lock:
        return _run(app, watch, notifier, book)


def _run(app: App, watch: Watch, notifier: Notifier | None, book: WatchBook | None) -> WatchRun:
    history = [result for _, result in app.store.last_runs(watch.name, limit=EARLIER_RUNS)]
    earlier = earlier_pages(app, history)
    previous = history[0] if history else None
    if previous is not None and previous.goal != clean(watch.goal):
        previous = None  # the question was edited: plan again, and start a new baseline

    researcher = app.researcher(fresh=True, scope=f"watch:{watch.name}", **_options(watch))
    # The first run plans the searches; later runs repeat them, so results stay comparable.
    result = researcher.run(watch.goal, plan=previous.plan if previous else None, reuse=previous)
    app.learn_from(researcher)
    if previous is None and history:
        app.store.retire(watch.name)

    deltas = diff(app.store.facts(watch.name), result, earlier=earlier)
    rules = [parse_rule(text) for text in watch.alerts or DEFAULT_RULES]
    fired = triggers(rules, deltas, baseline=previous is None)
    run_id, ids = app.store.record(
        watch.name, result, deltas, raised=fired, cleared=cleared(rules, deltas)
    )
    raised = tuple(t for t, alert in zip(fired, ids, strict=True) if alert is not None)

    delivered, error = deliver(app, watch, notifier, now=result.finished_at)
    if raised and watch.stop_when_alerted and book is not None:
        try:
            book.update(watch.name, paused=True)
        except (ScoutError, OSError) as exc:
            log.error("could not pause watch %s after it alerted: %s", watch.name, exc)
    alerts = app.store.alerts(watch.name, limit=FEED_ENTRIES)
    try:
        write_feed(app.settings.feed_path(watch.name), watch, alerts, now=result.finished_at)
    except OSError as exc:
        log.warning("could not write the feed of watch %s: %s", watch.name, exc)
    return WatchRun(
        watch=watch.name,
        run_id=run_id,
        result=result,
        deltas=tuple(deltas),
        alerts=raised,
        baseline=previous is None,
        delivered=delivered,
        notify_error=error,
    )


def earlier_pages(app: App, runs: Sequence[RunResult]) -> dict[str, Source]:
    """The pages as these runs (newest first) last read them, by URL, with their text."""
    pages: dict[str, Source] = {}
    for run in runs:
        for source in run.sources:
            if source.snippet_only or source.url in pages:
                continue
            page = app.restore_source(source)
            if page is not None:
                pages[source.url] = page
    return pages


def deliver(
    app: App, watch: Watch, notifier: Notifier | None, *, now: datetime
) -> tuple[int, str | None]:
    """Send the watch's undelivered alerts in one notification: (how many, error)."""
    if notifier is None:
        return 0, None
    pending = app.store.undelivered(watch.name, since=now - _redelivery(watch))
    if not pending:
        return 0, None
    ids = [alert.id for alert in pending]
    try:
        notifier.send(*digest(watch, pending))
    except NotifyError as exc:
        app.store.mark_alerts(ids, at=now, error=str(exc))
        return 0, str(exc)
    app.store.mark_alerts(ids, at=now)
    return len(pending), None


def digest(watch: Watch, alerts: Sequence[AlertRecord]) -> tuple[str, str]:
    """One notification for several alerts: (title, plain-text body). Plain text, because the
    quotes come from web pages and must not be read as markup."""
    title = f"Scout {watch.name}: {len(alerts)} alert{'s' if len(alerts) != 1 else ''}"
    lines = [_inert(watch.goal), ""]
    for alert in alerts:
        lines.append(f"- {_inert(alert.reason)}")
        if alert.quote:
            lines.append(f'  "{_inert(shorten(alert.quote, 200))}"')
        lines.append(f"  {quote(alert.url, safe=_URL_SAFE)}")
    lines += ["", f"details: scout watch changes {watch.name}"]
    return title, "\n".join(lines)


def _inert(text: str) -> str:
    """Web text that chat services cannot read as a mention, a link or markup."""
    return text.translate(_INERT)


def _redelivery(watch: Watch) -> timedelta:
    schedule = trigger(watch)
    if isinstance(schedule, IntervalTrigger):
        return max(REDELIVERY_WINDOW, 2 * schedule.interval)
    return REDELIVERY_WINDOW


def _options(watch: Watch) -> dict[str, object]:
    chosen = {
        "kind": watch.kind,
        "recency": watch.recency,
        "region": watch.region,
        "max_results": watch.max_results,
    }
    return {name: value for name, value in chosen.items() if value is not None}
