"""Run a watch once: research (or fact-check, or audit citations), compare with what it knew,
raise alerts, deliver them, and refresh its feed."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from urllib.parse import quote

from apscheduler.triggers.interval import IntervalTrigger

from scout.app import App
from scout.errors import FetchError, NotifyError, ScoutError
from scout.files import FileLock
from scout.monitor import claims
from scout.monitor.diff import Change, Delta, diff
from scout.monitor.feed import FEED_ENTRIES, write_feed
from scout.monitor.notify import AppriseNotifier, Notifier
from scout.monitor.rules import Trigger, cleared, dead_reason, parse_rule, triggers
from scout.monitor.watches import Watch, WatchBook, trigger
from scout.research.factcheck import ARCHIVE_LIMIT, PAGES_PER_CLAIM, web_address
from scout.research.results import CHECK_KIND, RunResult, Source
from scout.research.schema import ModelFinding
from scout.research.verify import evidence, verify
from scout.store import AlertRecord
from scout.textutil import clean, fold, shorten
from scout.web.archive import Archive, Copy, Snapshot, in_archive, working
from scout.web.domains import canonical_url
from scout.web.fetch import Document
from scout.web.soft404 import error_page

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
CLAIM_DEFAULT_RULES = ("changed",)
CITED_DEFAULT_RULES = ("changed", "dead")
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
    rulings: tuple[Delta, ...] = ()  # a claim watch's rulings, in the order of its claims
    pages: tuple[Delta, ...] = ()  # a citation watch's cited pages, read or not
    left: int = 0  # a citation watch's claims whose sentence left the text
    asked: int = 0  # model requests the run made
    # The archive's copy of each cited page that died in this run, by page fact key (None: it
    # has none); a page the archive did not answer for is missing.
    copies: Mapping[str, Copy | None] = field(default_factory=dict)

    @property
    def is_check(self) -> bool:
        return self.result.plan.kind == CHECK_KIND

    @property
    def is_cited(self) -> bool:
        return self.result.cited

    @property
    def counts(self) -> Counter[Change]:
        """How the facts changed; for a claim watch, how its rulings did."""
        return Counter(delta.change for delta in (self.rulings if self.is_check else self.deltas))


def rules_of(watch: Watch) -> tuple[str, ...]:
    """The alert rules a watch runs on: its own, or the defaults for its kind of watch."""
    if watch.alerts:
        return watch.alerts
    if watch.cited:
        return CITED_DEFAULT_RULES
    return CLAIM_DEFAULT_RULES if watch.check else DEFAULT_RULES


def notifier_for(watch: Watch) -> Notifier | None:
    return AppriseNotifier(watch.notify) if watch.notify else None


def run_watch(
    app: App,
    watch: Watch,
    *,
    notifier: Notifier | None = None,
    book: WatchBook | None = None,
    public_only: bool = False,
    progress: Callable[[str], None] | None = None,
) -> WatchRun:
    """Run *watch* once; one run of a watch at a time. Given its *book*, a watch that stops when
    alerted is paused; *public_only* reads only the public internet (a run an assistant asked
    for, and every citation watch's: a text may cite anything). *progress* hears of each step of
    a check."""
    lock = FileLock(
        app.settings.data_dir / "locks" / f"{watch.name}.lock",
        wait=False,
        busy=f"watch {watch.name!r} is already running",
    )
    with lock:
        return _run(app, watch, notifier, book, public_only or watch.cited, progress)


def _run(
    app: App,
    watch: Watch,
    notifier: Notifier | None,
    book: WatchBook | None,
    public_only: bool,
    progress: Callable[[str], None] | None,
) -> WatchRun:
    history = [result for _, result in app.store.last_runs(watch.name, limit=EARLIER_RUNS)]
    previous = continued(app, watch, history[0]) if history else None

    researcher = app.researcher(
        fresh=True, public_only=public_only, scope=f"watch:{watch.name}", **_options(watch)
    )
    if watch.check:
        result = researcher.check(
            watch.goal, reuse=previous, cited=watch.cited, audit=watch.cited, progress=progress
        )
    else:
        # The first run plans the searches; later runs repeat them, so results stay comparable.
        plan = previous.plan if previous else None
        result = researcher.run(watch.goal, plan=plan, reuse=previous)
    app.learn_from(researcher)
    if previous is None and history:
        app.store.retire(watch.name)

    rules = [parse_rule(text) for text in rules_of(watch)]
    known = app.store.facts(watch.name)
    baseline = previous is None
    pages: list[Delta] = []
    gone: list[Delta] = []
    copies: dict[str, Copy | None] = {}
    if watch.check:
        # Only rulings, and cited pages, alert; the evidence is remembered to rule on the next
        # run. Until the watch has ruled on a claim, every ruling is a first one, no change.
        earlier, published = _read_before(app, watch, result)
        since = previous.started_at if previous else None
        rulings, evidence = claims.compare(
            known, result, earlier=earlier, published=published, since=since
        )
        pages = claims.pages(known, result, evidence)
        gone = claims.left(known, result)
        deltas = [*rulings, *evidence, *pages, *gone]
        first = baseline or not any(map(claims.is_ruling, known))
        dead = [rule for rule in rules if rule.kind == "dead"]
        judged = [rule for rule in rules if rule.kind != "dead"]
        fired = [
            *triggers(judged, rulings, baseline=first),
            *triggers(dead, pages, baseline=baseline),
        ]
        fired, lifted = _one_per_change(fired, deltas), []
        if app.archive is not None:
            copies = _copies(app.archive, pages)
            fired = [_remedied(alert, copies) for alert in fired]
    else:
        rulings, deltas = [], diff(known, result, earlier=earlier_pages(app, history))
        fired, lifted = triggers(rules, deltas, baseline=baseline), cleared(rules, deltas)
    run_id, ids = app.store.record(watch.name, result, deltas, raised=fired, cleared=lifted)
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
        baseline=baseline,
        delivered=delivered,
        notify_error=error,
        rulings=tuple(rulings),
        pages=tuple(pages),
        left=sum(1 for d in gone if d.change is Change.GONE and claims.is_ruling(d.fact)),
        asked=researcher.asked,
        copies=copies,
    )


def _read_before(app: App, watch: Watch, result: RunResult) -> tuple[dict[str, str], dict]:
    """The pages the run read in full, by canonical URL: each folded as the watch last read it
    (within the time page versions are kept), and the date each page gives itself."""
    read = [source for source in result.sources if not source.snippet_only]
    before = app.store.last_readings(watch.name, [source.url for source in read])
    earlier, published = {}, {}
    for source in read:
        page = canonical_url(source.url)
        if (doc := before.get(source.url)) is not None:
            then = replace(source, text=doc.text, offers=doc.offers)
            earlier[page] = fold(evidence(then))
        if (now := app.store.get_page(source.url)) is not None and now.published:
            published[page] = now.published
    return earlier, published


def _one_per_change(fired: Sequence[Trigger], deltas: Sequence[Delta]) -> list[Trigger]:
    """One alert per ruling change, the first rule's, saying so when its quote left the page:
    that quote explains the change, it does not back the new ruling."""
    told: dict[str, Trigger] = {}
    for alert in fired:
        if alert.delta.fact.key not in told:
            if claims.departed(alert.delta, deltas):
                site = alert.delta.fact.site
                alert = replace(alert, reason=f"{alert.reason} (no longer on {site})")
            told[alert.delta.fact.key] = alert
    return list(told.values())


def _copies(archive: Archive, pages: Sequence[Delta]) -> dict[str, Copy | None]:
    """The archive's newest working copy of each cited page that died in this run, taken
    before the run that first found it gone, and whether it holds what the page said. Once the
    archive does not answer, no other page is looked up, and those left out alert as dead."""
    dead = [
        page
        for page in pages
        if page.change is Change.CHANGED
        and page.fact.value != claims.READ
        and not in_archive(page.fact.url)
    ]
    copies: dict[str, Copy | None] = {}
    for page in dead[:ARCHIVE_LIMIT]:
        fact, missed = page.fact, page.previous.seen if page.previous is not None else None
        try:
            found = working(archive, fact.url, rejected=error_page, before=missed)
        except FetchError as exc:
            log.warning("the archive did not answer for %s: %s", fact.url, exc)
            break
        copies[fact.key] = None if found is None else _copy(*found, fact.quote)
    return copies


def _copy(snapshot: Snapshot, document: Document, quote: str) -> Copy:
    """What *document*, the copy *snapshot* is of, holds of *quote*, word for word."""
    if not document.ok:
        return Copy(snapshot, unread=document.error or document.status.value)
    if not quote:
        return Copy(snapshot)
    page = Source(1, snapshot.page, "", "", document.status.value, "", text=document.text)
    (found,) = verify([ModelFinding(claim=quote, quote=quote, source=1)], [page], strict=True)
    return Copy(snapshot, holds=found.trusted, anchor=found.anchor)


def _remedied(alert: Trigger, copies: Mapping[str, Copy | None]) -> Trigger:
    """A dead page's alert, saying what the archive holds of it, and leading to its copy. The
    reason names the dead page; the ledger keeps its fact as it was."""
    fact = alert.delta.fact
    if alert.rule.kind != "dead" or fact.key not in copies:
        return alert
    copy = copies[fact.key]
    alert = replace(alert, reason=dead_reason(fact, remedy(copy)))
    if copy is None:
        return alert
    moved = replace(fact, url=copy.snapshot.page, anchor=copy.anchor)
    return replace(alert, delta=replace(alert.delta, fact=moved))


def remedy(copy: Copy | None) -> str:
    """What the archive holds of a dead page: "archived 2026-09-28 with the quote"."""
    if copy is None:
        return "not archived"
    taken = f"archived {copy.snapshot.taken_on}"
    if copy.unread is not None:
        return f"{taken}, copy unreadable"
    if copy.holds is None:
        return taken
    return f"{taken} {'with' if copy.holds else 'without'} the quote"


def continued(app: App, watch: Watch, last: RunResult) -> RunResult | None:
    """*last*, the watch's last run, when its next run continues from it; else None, and the
    next run plans again and starts a new baseline. A citation watch removed and added again
    starts anew too: what it knew is forgotten, though its runs stay."""
    if not continues(watch, last):
        return None
    if watch.cited and not any(map(claims.is_ruling, app.store.facts(watch.name))):
        return None
    return last


def continues(watch: Watch, previous: RunResult) -> bool:
    """Whether *previous* answered what the watch asks now: a goal edited, or switched between
    research, fact-checking and auditing citations, starts a new baseline. A citation watch's
    text may change, as the page it watches does: its unchanged sentences keep their claims;
    switching between a page and a text starts anew."""
    goal = clean(watch.goal)
    if watch.cited:
        audited = previous.goal.partition(": ")[2]  # "citation audit: <address or text>"
        return previous.audit and (
            audited == goal if web_address(goal) else not web_address(audited)
        )
    if watch.check:
        return (
            previous.plan.kind == CHECK_KIND
            and not previous.cited
            and previous.checked_text == goal
        )
    return previous.plan.kind != CHECK_KIND and previous.goal == goal


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
    lines = [_inert(watch.label), ""]
    for alert in alerts:
        lines.append(f"- {_inert(alert.reason)}")
        if alert.quote:
            lines.append(f'  "{_inert(shorten(alert.quote, 200))}"')
        lines.append(f"  {quote(alert.link, safe=_URL_SAFE)}")
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
        "max_results": watch.max_results or (PAGES_PER_CLAIM if watch.check else None),
    }
    return {name: value for name, value in chosen.items() if value is not None}
