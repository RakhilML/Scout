"""Claim watches over several runs of a changing (fake) web: a ruling alerts once, when a page
changed it, and never because the model read unchanged pages differently."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import chain
from pathlib import Path

import pytest

from scout.app import App
from scout.errors import AnswerPending
from scout.monitor.claims import claim_key, compare, noticed_for
from scout.monitor.diff import Change
from scout.monitor.runner import EARLIER_RUNS, WatchRun, run_watch
from scout.monitor.watches import Watch
from scout.research.results import (
    CHECK_KIND,
    ClaimCheck,
    Confidence,
    Finding,
    Plan,
    Ruling,
    RunResult,
    Source,
    Verdict,
)
from scout.settings import Settings
from scout.textutil import fold
from scout.web.domains import hostname
from scout.web.fetch import Document, FetchStatus
from tests.helpers import NOW, FakeFetcher, FakeSearch, ScriptedBackend

CLAIM = "Python 3.14 is the latest stable version of Python."
QUERY = "latest stable python version"
LATEST = "Python 3.14.0 is the latest stable release of Python."
NEWER = "Python 3.15.0 is the latest stable release of Python."
ALPHA = "Python 3.15.0 alpha 1 was released in October 2025."
SINCE = "Python 3.14 is the latest stable version, out since 7 October 2025."

DOWNLOADS = "https://www.python.org/downloads/"
HISTORY = "https://en.wikipedia.org/wiki/History_of_Python"
WHATSNEW = "https://docs.python.org/3/whatsnew/3.14.html"
PAGES = {
    DOWNLOADS: f"Download Python. {LATEST} Older releases are listed below.",
    HISTORY: f"Python was first released in 1991. {ALPHA} It is a preview.",
    WHATSNEW: Document(  # published since the watch last ran
        url=WHATSNEW,
        status=FetchStatus.OK,
        fetched_at=NOW,
        text=f"What's new. {SINCE}",
        content_hash="whatsnew",
        published=datetime.now(UTC).date(),
    ),
}
HITS = [
    (DOWNLOADS, "Download Python", "The latest stable version of Python"),
    (HISTORY, "History of Python", "Python versions and the latest stable release"),
]
LISTED = {"claims": [{"claim": CLAIM, "excerpt": CLAIM, "query": QUERY}]}
CHANGED = f"changed: {CLAIM.removesuffix('.')}: supported \N{RIGHTWARDS ARROW}"

WATCH = Watch(name="py-latest", goal=CLAIM, every="1d", check=True)
EVERY_RULE = replace(WATCH, alerts=("changed", "supported", "refuted"))


def judged(*evidence: tuple[str, str, int]) -> dict:
    return {
        "evidence": [
            {"stance": stance, "quote": quote, "source": source, "says": quote}
            for stance, quote, source in evidence
        ],
        "note": "The pages settle it.",
    }


SUPPORTED = judged(("supports", LATEST, 1))


class Inbox:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, title: str, body: str) -> None:
        self.sent.append((title, body))


class World:
    """An App whose web, search results, model and notifier the test controls."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.model = ScriptedBackend({"claims": [], "judge": []})
        monkeypatch.setattr("scout.app.make_backend", lambda settings: self.model)
        self.app = App(Settings(data_dir=tmp_path, reports_dir=tmp_path / "reports"))
        self.results = {QUERY: list(HITS)}  # edit between runs to change the search results
        self.app.watch_search = FakeSearch(self.results)
        self.web = FakeFetcher(dict(PAGES), cache=self.app.store)
        self.app.watch_fetcher = self.web
        self.inbox = Inbox()
        self.last_calls: list[str] = []

    def run(self, watch: Watch = WATCH, *, claims: dict | None = None, judge=()) -> WatchRun:
        if claims is not None:
            self.model.add("claims", claims)
        self.model.add("judge", *judge)
        before = len(self.model.requests)
        outcome = run_watch(self.app, watch, notifier=self.inbox)
        self.last_calls = self.model.purposes()[before:]
        return outcome

    def ledger(self, name: str = WATCH.name) -> list[tuple[str, str | None, str, str]]:
        return [
            (fact.key.split("|")[0], fact.value, fact.quote, fact.url)
            for fact in self.app.store.facts(name)
        ]


@pytest.fixture
def world(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    yield world
    world.app.close()


def reasons(outcome: WatchRun) -> list[str]:
    return [trigger.reason for trigger in outcome.alerts]


def test_a_claim_watch_lists_its_claims_once_and_rules_on_them(world):
    first = world.run(claims=LISTED, judge=[SUPPORTED])

    assert world.last_calls == ["claims", "judge"]
    assert first.baseline
    assert first.alerts == ()
    assert first.counts == {Change.NEW: 1}
    assert world.ledger() == [
        ("claim", "supported", LATEST, DOWNLOADS),
        ("evidence", "supports", LATEST, DOWNLOADS),
    ]


def test_unchanged_pages_cost_no_model_time(world):
    first = world.run(claims=LISTED, judge=[SUPPORTED])
    second = world.run()

    assert world.last_calls == []
    assert second.result.carried_over
    assert (
        "claim 1: no page changed since the last check; its evidence was kept"
        in second.result.warnings
    )
    (ruling,) = second.rulings
    assert (ruling.change, ruling.fact.value) == (Change.SAME, "supported")
    assert ruling.fact.seen == first.result.started_at
    assert second.alerts == ()
    # The ruling and its evidence still know where their quote is.
    placed = "text=Python%203.14.0%20is%20the,stable%20release%20of%20Python."
    assert [fact.anchor for fact in world.app.store.facts(WATCH.name)] == [placed, placed]


def test_a_claim_that_stops_being_true_alerts_once_with_the_new_quote(world):
    world.run(claims=LISTED, judge=[SUPPORTED])
    world.web.pages[DOWNLOADS] = PAGES[DOWNLOADS].replace(LATEST, NEWER)
    changed = world.run(judge=[judged(("refutes", NEWER, 1))])

    assert world.last_calls == ["judge"]  # the claims are not listed again
    assert reasons(changed) == [f"{CHANGED} refuted"]
    (alert,) = world.app.store.alerts(WATCH.name)
    assert (alert.quote, alert.url) == (NEWER, DOWNLOADS)
    at_quote = f"{DOWNLOADS}#:~:text=Python%203.15.0%20is%20the,stable%20release%20of%20Python."
    assert alert.link == at_quote
    assert len(world.inbox.sent) == 1
    assert NEWER in world.inbox.sent[0][1]
    assert f"\n  {at_quote}\n" in world.inbox.sent[0][1]
    feed = world.app.settings.feed_path(WATCH.name).read_text(encoding="utf-8")
    assert feed.count("<entry>") == 1
    assert f'href="{at_quote}"' in feed

    again = world.run()
    assert world.last_calls == []
    assert again.alerts == ()
    assert again.rulings[0].change is Change.SAME
    assert len(world.inbox.sent) == 1


def test_a_model_reading_unchanged_pages_differently_is_noticed_not_alerted(world):
    world.run(EVERY_RULE, claims=LISTED, judge=[SUPPORTED])
    world.web.pages[HISTORY] += " It is not meant for production."  # forces judging again
    noticed = world.run(EVERY_RULE, judge=[judged(("supports", LATEST, 1), ("refutes", ALPHA, 2))])

    (ruling,) = noticed.rulings
    assert (ruling.change, ruling.previous.value, ruling.fact.value) == (
        Change.NOTICED,
        "supported",
        "supported",  # what the model noticed is shown, but rules nothing
    )
    assert [(f.quote, f.url) for f in noticed_for(claim_key(CLAIM), noticed.deltas)] == [
        (ALPHA, HISTORY)
    ]
    assert noticed.alerts == ()
    assert world.app.store.alerts(WATCH.name) == []
    assert world.ledger()[0][:2] == ("claim", "supported")


def test_a_page_the_search_no_longer_returns_keeps_its_evidence(world):
    world.run(EVERY_RULE, claims=LISTED, judge=[SUPPORTED])
    world.results[QUERY] = HITS[1:]
    dropped = world.run(EVERY_RULE, judge=[judged()])

    assert dropped.result.claims[0].ruling is Ruling.UNCLEAR  # this run alone settles nothing
    (ruling,) = dropped.rulings
    assert (ruling.change, ruling.fact.value, ruling.fact.quote) == (
        Change.SAME,
        "supported",
        LATEST,
    )
    assert dropped.alerts == ()

    world.results[QUERY] = []
    nothing = world.run(EVERY_RULE)
    assert world.last_calls == []
    assert "claim 1: no search results for: latest stable python version" in (
        nothing.result.warnings
    )
    assert [(r.change, r.fact.value) for r in nothing.rulings] == [(Change.SAME, "supported")]


def test_a_text_without_claims_is_listed_again_and_its_first_claims_alert_nobody(world):
    empty = world.run(EVERY_RULE, claims={"claims": []})
    assert (empty.rulings, world.last_calls) == ((), ["claims"])

    later = world.run(EVERY_RULE, claims=LISTED, judge=[SUPPORTED])
    assert world.last_calls == ["claims", "judge"]
    assert not later.baseline
    assert [r.change for r in later.rulings] == [Change.NEW]
    assert later.alerts == ()


def test_a_quote_that_leaves_its_page_can_leave_a_claim_unclear(world):
    world.run(EVERY_RULE, claims=LISTED, judge=[SUPPORTED])
    world.web.pages[DOWNLOADS] = "Download Python. Older releases are listed below."
    once = world.run(EVERY_RULE, judge=[judged()])
    assert once.alerts == ()  # one reading without the quote may be a bad fetch
    gone = world.run(EVERY_RULE)

    assert reasons(gone) == [f"{CHANGED} unclear (no longer on python.org)"]
    (alert,) = world.app.store.alerts(WATCH.name)
    assert (alert.quote, alert.url, alert.link) == (LATEST, DOWNLOADS, DOWNLOADS)
    assert f"\n  {DOWNLOADS}\n" in world.inbox.sent[-1][1]


def _new_page(world: World) -> None:
    world.results[QUERY].append((WHATSNEW, "What's new in Python 3.14", "latest stable version"))


def _refuting_line(world: World) -> None:
    world.web.pages[HISTORY] += f" {NEWER}"


@pytest.mark.parametrize(
    ("baseline", "change", "judgment", "expected"),
    [
        (judged(), _new_page, judged(("supports", SINCE, 3)), f"now supported: {CLAIM}"),
        (
            SUPPORTED,
            _refuting_line,
            judged(("supports", LATEST, 1), ("refutes", NEWER, 2)),
            f"now disputed: {CLAIM}",
        ),
    ],
    ids=["unclear-to-supported", "supported-to-disputed"],
)
def test_refuted_and_supported_rules_alert_only_in_their_direction(
    world, baseline, change, judgment, expected
):
    watch = replace(WATCH, alerts=("supported", "refuted"))
    world.run(watch, claims=LISTED, judge=[baseline])
    change(world)
    outcome = world.run(watch, judge=[judgment])

    assert outcome.rulings[0].change is Change.CHANGED
    assert reasons(outcome) == [expected]


def test_editing_a_claim_watchs_text_starts_a_new_baseline(world):
    world.run(claims=LISTED, judge=[SUPPORTED])
    world.web.pages[DOWNLOADS] = PAGES[DOWNLOADS].replace(LATEST, NEWER)
    edited = replace(WATCH, goal=f"{CLAIM} Upgrade soon.")
    outcome = world.run(edited, claims=LISTED, judge=[judged(("refutes", NEWER, 1))])

    assert world.last_calls == ["claims", "judge"]
    assert outcome.baseline
    assert outcome.alerts == ()
    assert world.ledger() == [
        ("claim", "refuted", NEWER, DOWNLOADS),
        ("evidence", "refutes", NEWER, DOWNLOADS),
    ]

    research = Watch(name="py-switch", goal=CLAIM, every="1d")
    world.model.add("plan", {"queries": [QUERY], "kind": "general", "recency": "any"})
    world.model.add(
        "extract", {"answer": "", "findings": [{"claim": NEWER, "quote": NEWER, "source": 1}]}
    )
    world.run(research)
    switched = world.run(replace(research, check=True), claims=LISTED, judge=[judged()])
    assert world.last_calls == ["claims", "judge"]
    assert switched.baseline
    assert world.ledger("py-switch") == [("claim", "unclear", "", "")]


def test_a_claim_watch_waiting_for_an_answer_resumes_with_the_same_prompts(world):
    world.run(claims=LISTED, judge=[SUPPORTED])
    world.web.pages[DOWNLOADS] = PAGES[DOWNLOADS].replace(LATEST, NEWER)
    with pytest.raises(AnswerPending):
        world.run(judge=[AnswerPending(Path("requests/x.md"))])
    assert len(world.app.store.last_runs(WATCH.name, limit=5)) == 1
    assert world.ledger()[0][:2] == ("claim", "supported")
    asked = world.model.requests[-1].messages

    world.web.pages[DOWNLOADS] = "Download Python."  # meanwhile: the waiting run read it before
    resumed = world.run(judge=[judged(("refutes", NEWER, 1))])
    assert world.last_calls == ["judge"]
    assert world.model.requests[-1].messages == asked
    assert reasons(resumed) == [f"{CHANGED} refuted"]


def test_a_snippet_the_model_newly_cites_is_noticed_not_alerted(world):
    blog = ("https://blog.example/py", "The latest stable version of Python", NEWER)
    world.results[QUERY] = [blog, *HITS]  # the blog is never readable: a snippet only
    world.run(EVERY_RULE, claims=LISTED, judge=[judged(("supports", LATEST, 2))])
    world.web.pages[DOWNLOADS] += " Mirrors are listed too."  # forces judging again
    noticed = world.run(EVERY_RULE, judge=[judged(("supports", LATEST, 2), ("refutes", NEWER, 1))])

    (ruling,) = noticed.rulings
    assert (ruling.change, ruling.fact.value) == (Change.NOTICED, "supported")
    assert [fact.quote for fact in noticed_for(claim_key(CLAIM), noticed.deltas)] == [NEWER]
    assert noticed.alerts == ()


def test_a_page_read_long_ago_still_tells_what_was_on_it(world):
    world.run(EVERY_RULE, claims=LISTED, judge=[SUPPORTED])
    world.results[QUERY] = HITS[:1]
    world.run(EVERY_RULE, judge=[SUPPORTED])
    for _ in range(EARLIER_RUNS):
        world.run(EVERY_RULE)
    world.results[QUERY] = list(HITS)  # back, as it was six runs ago
    back = world.run(EVERY_RULE, judge=[judged(("supports", LATEST, 1), ("refutes", ALPHA, 2))])

    assert [(r.change, r.fact.value) for r in back.rulings] == [(Change.NOTICED, "supported")]
    assert back.alerts == ()


def test_a_real_change_is_told_by_its_page_never_by_a_misreading(world):
    world.run(EVERY_RULE, claims=LISTED, judge=[SUPPORTED])
    world.web.pages[DOWNLOADS] = "Download Python. Older releases are listed below."
    world.run(EVERY_RULE, judge=[judged(("refutes", ALPHA, 2))])
    outcome = world.run(EVERY_RULE)

    # The support left its page; the "refutation" was on its page all along: unclear, not refuted.
    assert reasons(outcome) == [f"{CHANGED} unclear (no longer on python.org)"]
    noticed = noticed_for(claim_key(CLAIM), outcome.deltas, standing=True)
    assert [fact.quote for fact in noticed] == [ALPHA]  # shown beside the change, never ruling


def test_a_page_that_now_says_otherwise_changes_the_ruling_at_once(world):
    world.run(EVERY_RULE, claims=LISTED, judge=[SUPPORTED])
    world.web.pages[DOWNLOADS] = PAGES[DOWNLOADS].replace(LATEST, NEWER)
    changed = world.run(EVERY_RULE, judge=[judged(("refutes", NEWER, 1))])

    # The page that lost the support verified the refutation: it was read soundly.
    assert reasons(changed) == [f"{CHANGED} refuted"]  # one alert, though three rules match


def test_one_quote_on_two_pages_of_a_site_counts_on_each(world):
    windows = "https://www.python.org/downloads/windows/"
    world.results[QUERY] = [(windows, "The latest stable version of Python", LATEST), *HITS]
    world.web.pages[windows] = f"Windows installers. {LATEST}"
    both = judged(("supports", LATEST, 1), ("supports", LATEST, 2))
    world.run(EVERY_RULE, claims=LISTED, judge=[both])
    world.web.pages[DOWNLOADS] = "Download Python. Older releases are listed below."
    for _ in range(2):
        later = world.run(EVERY_RULE, judge=[judged(("supports", LATEST, 1))])

    # The quote left one page of python.org; another page of it still carries it.
    assert [(r.change, r.fact.value) for r in later.rulings] == [(Change.SAME, "supported")]
    assert later.alerts == ()


def test_a_run_that_asked_the_model_nothing_says_so(world):
    mascot = "Python has a mascot called Pythonista."
    watch = replace(WATCH, goal=f"{CLAIM} {mascot}")
    listed = {"claims": [*LISTED["claims"], {"claim": mascot, "excerpt": mascot, "query": "zzz"}]}
    world.run(watch, claims=listed, judge=[SUPPORTED])
    again = world.run(watch)

    assert world.last_calls == []
    assert again.result.carried_over  # one claim found nothing to read: still nothing asked


A, B, C = "https://a.example/py", "https://b.example/py", "https://c.example/py"
READ = {A: LATEST, B: f"{ALPHA} Try it."}
LATER = NOW + timedelta(days=1)


def checked(pages, *, supports=(), refutes=(), started=NOW, dated=(), snippets=()) -> RunResult:
    """A check of CLAIM that read *pages* ({url: text}) and trusts the (url, quote) evidence
    *supports* and *refutes*; pages in *dated* were published on its day, *snippets* only
    known by their search snippet."""
    sources = tuple(
        Source(
            n,
            url,
            url,
            hostname(url),
            "ok",
            QUERY,
            text=text,
            content_hash=None if url in snippets else text,
            snippet_only=url in snippets,
            published=started.date() if url in dated else None,
        )
        for n, (url, text) in enumerate(pages.items(), start=1)
    )
    number = {source.url: source.index for source in sources}
    cited = [*supports, *refutes]
    return RunResult(
        goal=f"fact-check: {CLAIM}",
        started_at=started,
        finished_at=started,
        model="m",
        plan=Plan((QUERY,), CHECK_KIND, None, "model"),
        sources=sources,
        answer="",
        findings=tuple(
            Finding(claim=quote, quote=quote, source=number[url], verdict=Verdict.VERIFIED)
            for url, quote in cited
        ),
        confidence=Confidence("medium", ""),
        claims=(
            ClaimCheck(
                CLAIM,
                CLAIM,
                QUERY,
                supports=tuple(range(1, len(supports) + 1)),
                refutes=tuple(range(len(supports) + 1, len(cited) + 1)),
            ),
        ),
        checked_text=CLAIM,
    )


@pytest.mark.parametrize(
    ("run", "change", "ruling", "behind"),
    [
        (
            checked({**READ, C: NEWER}, supports=[(A, LATEST)], refutes=[(C, NEWER)], dated=[C]),
            Change.CHANGED,
            "disputed",
            (NEWER, C),
        ),
        (
            checked({**READ, C: NEWER}, supports=[(A, LATEST)], refutes=[(C, NEWER)]),
            Change.NOTICED,
            "supported",
            (LATEST, A),
        ),
        (
            checked(READ, supports=[(A, LATEST)], refutes=[(B, ALPHA)]),
            Change.NOTICED,
            "supported",
            (LATEST, A),
        ),
        (
            checked({**READ, C: NEWER}, supports=[(A, LATEST)], refutes=[(C, NEWER)], snippets=[C]),
            Change.NOTICED,
            "supported",
            (LATEST, A),
        ),
        (checked({**READ, A: "Downloads."}), Change.SAME, "supported", (LATEST, A)),
        (
            checked({**READ, A: f"Downloads. {NEWER}"}, refutes=[(A, NEWER)]),
            Change.CHANGED,
            "refuted",
            (NEWER, A),
        ),
        (checked({B: READ[B]}), Change.SAME, "supported", (LATEST, A)),
        (checked(READ), Change.SAME, "supported", (LATEST, A)),
        (
            checked({**READ, C: SINCE}, supports=[(A, LATEST), (C, SINCE)], dated=[C]),
            Change.SAME,
            "supported",
            (LATEST, A),
        ),
        (
            checked({**READ, A: "Downloads."}, refutes=[(B, ALPHA)]),
            Change.NOTICED,
            "supported",
            (LATEST, A),
        ),
    ],
    ids=[
        "refuting-page-published-since",
        "refuting-page-first-read-but-old",
        "unchanged-page-read-differently",
        "refuting-snippet",
        "support-missing-once",
        "support-left-a-page-that-now-refutes",
        "support-page-not-read-again",
        "model-forgot-the-support",
        "new-support-keeps-the-ruling",
        "support-missing-once-and-misreading",
    ],
)
def test_compare_rules_on_evidence_a_page_proves(run, change, ruling, behind):
    baseline = checked(READ, supports=[(A, LATEST)])
    rulings, evidence = compare([], baseline, earlier={}, published={}, since=None)
    assert [d.change for d in (*rulings, *evidence)] == [Change.NEW, Change.NEW]
    remembered = [delta.fact for delta in (*rulings, *evidence)]
    earlier = {url: fold(text) for url, text in READ.items()}

    later = replace(run, started_at=LATER, finished_at=LATER)
    published = {s.url: LATER.date() for s in later.sources if s.published}
    (delta,), _ = compare(remembered, later, earlier=earlier, published=published, since=NOW)

    assert (delta.change, delta.previous.value, delta.fact.value) == (change, "supported", ruling)
    assert (delta.fact.quote, delta.fact.url) == behind
    assert delta.fact.seen == (LATER if change is Change.CHANGED else NOW)


def test_a_quote_leaves_after_a_second_reading_without_it():
    baseline = checked(READ, supports=[(A, LATEST)])
    remembered = [
        d.fact for d in chain(*compare([], baseline, earlier={}, published={}, since=None))
    ]
    earlier = {url: fold(text) for url, text in READ.items()}
    lost = checked({**READ, A: "Downloads."}, started=LATER)

    (once,), evidence = compare(remembered, lost, earlier=earlier, published={}, since=NOW)
    assert (once.change, once.fact.value) == (Change.SAME, "supported")
    kept = [delta.fact for delta in (once, *evidence)]
    assert [fact.missed for fact in kept if fact.key.startswith("evidence|")] == [True]

    again = replace(lost, started_at=LATER + timedelta(days=1))
    (twice,), _ = compare(kept, again, earlier=earlier, published={}, since=LATER)
    assert (twice.change, twice.fact.value) == (Change.CHANGED, "unclear")


def test_a_url_read_twice_in_a_run_is_judged_on_every_reading():
    baseline = checked(READ, supports=[(A, LATEST)])
    remembered = [
        d.fact for d in chain(*compare([], baseline, earlier={}, published={}, since=None))
    ]
    earlier = {url: fold(text) for url, text in READ.items()}
    run = checked({**READ, A: f"Downloads. {NEWER}"}, refutes=[(A, NEWER)], started=LATER)
    stale = replace(run.sources[0], index=3, text=LATEST, content_hash=LATEST)  # an older copy
    twice = replace(run, sources=(*run.sources, stale))

    (delta,), _ = compare(remembered, twice, earlier=earlier, published={}, since=NOW)
    # The older copy still carries the support: it stands, beside the new refutation.
    assert (delta.change, delta.fact.value) == (Change.CHANGED, "disputed")
