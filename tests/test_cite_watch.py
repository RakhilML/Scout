"""Citation watches over several runs of a changing (fake) web and text: a claim keeps its
evidence while its cited pages read the same, a ruling alerts once when a page changed it, and a
cited page that stops being readable alerts once, after two runs in a row."""

from dataclasses import replace
from datetime import timedelta
from functools import partial
from pathlib import Path

import pytest
import responses
from click.testing import CliRunner

from scout.app import App
from scout.cli import main
from scout.clock import utcnow
from scout.errors import AnswerPending
from scout.monitor.claims import is_ruling
from scout.monitor.diff import Change
from scout.monitor.feed import atom
from scout.monitor.runner import WatchRun, digest, run_watch
from scout.monitor.watches import Watch, WatchBook
from scout.research.pipeline import Researcher
from scout.settings import Settings
from scout.store import AlertRecord
from scout.web.archive import Copy, Snapshot
from scout.web.fetch import Document, FetchStatus
from tests.helpers import NOW, Clock, FakeArchive, FakeFetcher, ScriptedBackend

A, B, C, D = (f"https://{site}.example/stations" for site in ("one", "two", "three", "four"))


def fact(n: int) -> str:
    return f"Station {n} opened in {1900 + n} with {n + 2} platforms"


def page(*facts: int, more: str = "") -> str:
    return " ".join(["The records of the line.", *(f"{fact(n)}." for n in facts), more]).strip()


PAGES = {A: page(1), B: page(2), C: page(3), D: page(4)}


def text(*cited: tuple[int, str]) -> str:
    """A text stating fact n, citing its url, per pair, its sources numbered in order."""
    numbers: dict[str, int] = {}
    for _, url in cited:
        numbers.setdefault(url, len(numbers) + 1)
    sentences = " ".join(f"{fact(n)} [{numbers[url]}]." for n, url in cited)
    return f"{sentences}\n\n" + "\n".join(f"[{n}] {url}" for url, n in numbers.items())


def claim(n: int, cite: int) -> dict:
    return {"claim": f"{fact(n)}.", "excerpt": f"{fact(n)} [{cite}].", "query": "q"}


def listed(*pairs: tuple[int, int]) -> dict:
    return {"claims": [claim(n, cite) for n, cite in pairs]}


def judged(*evidence: tuple[str, str, int]) -> dict:
    return {
        "evidence": [
            {"stance": stance, "quote": quote, "source": source, "says": quote}
            for stance, quote, source in evidence
        ],
        "note": "",
    }


def backed(n: int, cite: int) -> dict:
    return judged(("supports", f"{fact(n)}.", cite))


NOTHING = judged()
THREE = text((1, A), (2, B), (3, C))
WATCH = Watch(name="stations", goal=THREE, every="1d", check=True, cited=True)
BASELINE = {
    "claims": listed((1, 1), (2, 2), (3, 3)),
    "judge": [backed(1, 1), backed(2, 2), backed(3, 3)],
}
ARROW = "\N{RIGHTWARDS ARROW}"


class World:
    """An App whose web, model and inbox the test controls."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.model = ScriptedBackend({"claims": [], "judge": []})
        monkeypatch.setattr("scout.app.make_backend", lambda settings: self.model)
        self.clock = Clock(utcnow())  # runs hours apart, as a schedule has them
        monkeypatch.setattr("scout.app.Researcher", partial(Researcher, clock=self.clock))
        self.app = App(Settings(data_dir=tmp_path, reports_dir=tmp_path / "reports"))
        self.web = FakeFetcher(dict(PAGES), cache=self.app.store)
        self.app.public_fetcher = self.web  # a citation watch reads only the public internet
        self.last_calls: list[str] = []

    def run(
        self, watch: Watch = WATCH, *, claims: dict | None = None, judge=(), hours: float = 2
    ) -> WatchRun:
        if claims is not None:
            self.model.add("claims", claims)
        self.model.add("judge", *judge)
        before = len(self.model.requests)
        self.clock.advance(hours * 3600)
        outcome = run_watch(self.app, watch)
        self.last_calls = self.model.purposes()[before:]
        return outcome

    def baseline(self, watch: Watch = WATCH) -> WatchRun:
        return self.run(watch, **BASELINE)

    def labels(self, outcome: WatchRun) -> list[tuple[str, str | None]]:
        return [(delta.change.value, delta.fact.value) for delta in outcome.rulings]


@pytest.fixture
def world(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    yield world
    world.app.close()


def reasons(outcome: WatchRun) -> list[str]:
    return [trigger.reason for trigger in outcome.alerts]


def test_a_day_on_which_no_cited_page_changed_asks_the_model_nothing(world, workspace, monkeypatch):
    first = world.baseline()
    assert world.last_calls == ["claims", "judge", "judge", "judge"]
    assert first.baseline
    assert first.alerts == ()
    assert world.labels(first) == [("new", "backed")] * 3
    assert [delta.change for delta in first.pages] == [Change.NEW] * 3

    second = world.run()
    assert world.last_calls == []
    assert second.result.carried_over
    assert all(check.kept for check in second.result.claims)
    placed = [finding.anchor for finding in second.result.findings]
    assert placed == [finding.anchor for finding in first.result.findings]
    assert placed[0] == "text=Station%201%20opened%20in%201901%20with%203%20platforms."
    assert world.labels(second) == [("same", "backed")] * 3
    assert second.alerts == ()
    assert "3 of 3 claims kept their evidence" in second.result.warnings[0]

    WatchBook(workspace / "data" / "watches.yaml").add(WATCH)
    monkeypatch.setattr("scout.cli.watch.run_watch", lambda app, watch, **options: second)
    printed = CliRunner().invoke(main, ["watch", "run", WATCH.name]).output
    assert " ".join(printed.split()) == (
        f"stations run {second.run_id}: 3 claims checked (3 kept: their pages read as before): "
        "no change no cited page changed, so the model was not asked again"
    )


def test_renumbered_references_list_and_judge_only_the_new_sentence(world):
    first = world.baseline()
    renumbered = replace(WATCH, goal=text((4, D), (1, A), (2, B), (3, C)))
    every = listed((4, 1), (1, 2), (2, 3), (3, 4))  # the model lists the whole part again
    second = world.run(renumbered, claims=every, judge=[backed(4, 1)])

    assert world.last_calls == ["claims", "judge"]
    assert [check.kept for check in second.result.claims] == [False, True, True, True]
    assert [check.pages for check in second.result.claims] == [(1,), (2,), (3,), (4,)]
    kept = [finding.anchor for finding in second.result.findings[1:]]
    assert kept == [finding.anchor for finding in first.result.findings]
    assert all(kept)
    old = {delta.fact.key: delta for delta in first.rulings}
    assert [(d.change, d.fact.key in old) for d in second.rulings] == [
        (Change.NEW, False),
        (Change.SAME, True),
        (Change.SAME, True),
        (Change.SAME, True),
    ]
    remembered = {d.fact.key for d in first.deltas if d.fact.key.startswith("evidence|")}
    assert remembered <= {d.fact.key for d in second.deltas if d.change is Change.SAME}
    assert second.alerts == ()
    assert second.left == 0


def test_markers_in_another_order_are_no_change(world):
    both = {A: page(1), B: page(1)}
    world.web.pages.update(both)
    sources = f"\n\n[1] {A}\n[2] {B}"
    watch = replace(WATCH, goal=f"{fact(1)} [1][2].{sources}")
    world.run(
        watch,
        claims={"claims": [{**claim(1, 1), "excerpt": f"{fact(1)} [1][2]."}]},
        judge=[judged(("supports", f"{fact(1)}.", 1), ("supports", f"{fact(1)}.", 2))],
    )
    again = world.run(replace(watch, goal=f"{fact(1)} [2][1].{sources}"))

    assert world.last_calls == []
    assert world.labels(again) == [("same", "backed")]
    assert again.alerts == ()


def test_a_quote_that_leaves_its_cited_page_alerts_once(world):
    world.baseline()
    world.web.pages[B] = page(more="Its platforms were rebuilt.")
    once = world.run(judge=[NOTHING])
    assert world.last_calls == ["judge"]
    assert once.alerts == ()  # one reading without the quote may be a bad fetch
    assert world.labels(once)[1] == ("same", "backed")

    gone = world.run()
    assert world.last_calls == []  # the page reads as it did: the model is not asked again
    claim_2 = fact(2)
    assert reasons(gone) == [
        f"changed: {claim_2}: backed {ARROW} not found (no longer on two.example)"
    ]
    (alert,) = world.app.store.alerts(WATCH.name)
    assert (alert.quote, alert.url, alert.link) == (f"{claim_2}.", B, B)

    for _ in range(2):
        later = world.run()
        assert later.alerts == ()
        assert world.labels(later)[1] == ("same", "not found")
    assert len(world.app.store.alerts(WATCH.name)) == 1


def test_a_page_that_now_says_otherwise_changes_the_ruling_at_once(world):
    world.baseline()
    rebuilt = "Station 2 opened in 1912 with 4 platforms."
    world.web.pages[B] = page(more=rebuilt)
    changed = world.run(judge=[judged(("refutes", rebuilt, 2))])

    assert reasons(changed) == [f"changed: {fact(2)}: backed {ARROW} contradicted"]
    (alert,) = world.app.store.alerts(WATCH.name)
    assert alert.link == f"{B}#:~:text=Station%202%20opened%20in%201912%20with%204%20platforms."


def test_the_model_reading_a_page_differently_is_never_alerted(world):
    alike = "The register says station 1 opened in 1901 with 3 platforms."
    world.web.pages[A] = page(1, more=alike)
    watch = replace(WATCH, alerts=("changed", "backed", "contradicted", "dead"))
    world.run(watch, **BASELINE)

    world.web.pages[A] += " Tickets were sold there."  # unrelated: judged again
    silent = world.run(watch, judge=[NOTHING])
    assert world.labels(silent)[0] == ("same", "backed")

    world.web.pages[A] += " The line closed later."
    other = world.run(watch, judge=[judged(("supports", alike, 1))])
    assert world.labels(other)[0] == ("noticed", "backed")
    assert [silent.alerts, other.alerts] == [(), ()]


def test_a_cited_page_that_dies_alerts_once_after_two_runs_in_a_row(world):
    world.baseline()
    world.web.pages[C] = Document(
        url=C, status=FetchStatus.NOT_FOUND, fetched_at=NOW, error="HTTP 404"
    )
    missed = world.run()
    assert world.last_calls == []
    assert missed.alerts == ()
    assert [(d.change, d.fact.value, d.fact.missed) for d in missed.pages][2] == (
        Change.SAME,
        "read",
        True,
    )

    dead = world.run()
    assert reasons(dead) == [f"dead: {C} (not found: HTTP 404), cited for: {fact(3)}."]
    (alert,) = world.app.store.alerts(WATCH.name)
    assert (alert.url, alert.quote) == (C, f"{fact(3)}.")  # what the page said

    assert world.run().alerts == ()
    world.web.pages[C] = PAGES[C]
    back = world.run(judge=[backed(3, 3)])
    assert back.alerts == ()
    assert [(d.change, d.fact.value) for d in back.pages][2] == (Change.CHANGED, "read")
    for outcome in (missed, dead, back):
        assert world.labels(outcome)[2][1] == "backed"
    assert len(world.app.store.alerts(WATCH.name)) == 1


def test_a_page_unreadable_from_the_start_is_unreadable_and_never_dead(world):
    watch = replace(WATCH, alerts=("changed", "dead", "contradicted"))
    world.web.pages[C] = Document(
        url=C, status=FetchStatus.NOT_FOUND, fetched_at=NOW, error="HTTP 404"
    )
    first = world.run(
        watch, claims=listed((1, 1), (2, 2), (3, 3)), judge=[backed(1, 1), backed(2, 2)]
    )
    assert world.labels(first)[2] == ("new", "unreadable")
    assert first.result.claims[2].problems == (
        "could not read [3] three.example (not found: HTTP 404)",
    )
    assert world.run(watch).alerts == ()
    assert world.run(watch).alerts == ()

    other = "Station 3 opened in 1913 with 5 platforms."
    world.web.pages[C] = page(more=other)
    judged_now = world.run(watch, judge=[judged(("refutes", other, 3))])
    assert world.labels(judged_now)[2] == ("new", "contradicted")
    assert reasons(judged_now) == [f"now contradicted: {fact(3)}."]


@pytest.mark.parametrize(
    ("alerts", "expected"),
    [((), []), (("contradicted",), [f"now contradicted: {fact(4)}."])],
    ids=["default-rules", "contradicted"],
)
def test_a_sentence_added_later_alerts_only_when_its_page_contradicts_it(world, alerts, expected):
    watch = replace(WATCH, alerts=alerts)
    world.baseline(watch)
    added = replace(watch, goal=text((1, A), (2, B), (3, C), (4, D)))
    other = "Station 4 opened in 1914 with 6 platforms."
    world.web.pages[D] = page(more=other)
    outcome = world.run(added, claims=listed((4, 4)), judge=[judged(("refutes", other, 4))])

    assert world.labels(outcome)[3] == ("new", "contradicted")
    assert reasons(outcome) == expected


def test_a_sentence_removed_from_the_text_leaves_quietly_once_it_stays_away(world):
    first = world.baseline()
    shorter = replace(WATCH, goal=text((1, A), (2, B)))
    once = world.run(shorter)
    assert (world.last_calls, once.left, once.alerts) == ([], 0, ())
    kept = [f for f in world.app.store.facts(WATCH.name) if is_ruling(f)]
    assert [(f.claim, f.missed) for f in kept][2] == (f"{fact(3)}.", True)  # missing once

    outcome = world.run(shorter)
    assert world.last_calls == []
    assert outcome.left == 1
    assert outcome.alerts == ()
    facts = world.app.store.facts(WATCH.name)
    assert [f.claim for f in facts if is_ruling(f)] == [f"{fact(1)}.", f"{fact(2)}."]
    assert not any(f.url == C for f in facts)
    gone = first.rulings[2].fact.key.removeprefix("claim|")
    assert not any(gone in f.key for f in facts)


def test_a_sentence_back_after_one_run_without_it_rules_as_before(world):
    contradicted = replace(WATCH, alerts=("changed", "contradicted"))
    other = "Station 3 opened in 1913 with 5 platforms."
    world.web.pages[C] = page(more=other)
    world.run(
        contradicted,
        claims=listed((1, 1), (2, 2), (3, 3)),
        judge=[backed(1, 1), backed(2, 2), judged(("refutes", other, 3))],
    )
    world.run(replace(contradicted, goal=text((1, A), (2, B))))
    back = world.run(
        contradicted, claims=listed((1, 1), (2, 2), (3, 3)), judge=[judged(("refutes", other, 3))]
    )
    assert back.alerts == ()
    assert world.labels(back)[2] == ("same", "contradicted")


def test_a_watch_removed_and_added_again_starts_a_new_baseline(world):
    world.baseline()
    world.app.store.forget(WATCH.name)
    again = world.run(
        replace(WATCH, goal=text((4, D))), claims=listed((4, 1)), judge=[backed(4, 1)]
    )
    assert again.baseline


def test_one_fact_cited_to_two_pages_is_two_claims(world):
    world.web.pages[B] = page(1)
    twice = replace(WATCH, goal=text((1, A), (1, B)))
    first = world.run(twice, claims=listed((1, 1), (1, 2)), judge=[backed(1, 1), backed(1, 2)])
    keys = [delta.fact.key for delta in first.rulings]
    assert len(set(keys)) == 2

    other = "Station 1 opened in 1911 with 3 platforms."
    world.web.pages[B] = page(more=other)
    second = world.run(twice, judge=[judged(("refutes", other, 2))])
    assert world.last_calls == ["judge"]
    assert world.labels(second) == [("same", "backed"), ("changed", "contradicted")]
    assert [delta.fact.key for delta in second.rulings] == keys


def test_a_claim_the_model_could_not_judge_is_judged_again(world):
    first = world.run(
        claims=listed((1, 1), (2, 2), (3, 3)),
        judge=["not json", "not json", backed(2, 2), backed(3, 3)],
    )
    assert world.labels(first)[0] == ("new", "not judged")

    again = world.run(judge=[backed(1, 1)])
    assert world.last_calls == ["judge"]
    assert world.labels(again) == [("new", "backed"), ("same", "backed"), ("same", "backed")]
    assert again.alerts == ()


def test_a_sentence_the_model_passed_over_is_not_listed_again(world):
    first = world.run(claims=listed((1, 1), (2, 2)), judge=[backed(1, 1), backed(2, 2)])
    assert first.result.skipped == (f"{fact(3)} [3].",)
    again = world.run()
    assert world.last_calls == []
    assert again.result.skipped == first.result.skipped

    edited = replace(
        WATCH, goal=THREE.replace(fact(3), "Station 3 opened in 1903 with 7 platforms")
    )
    world.run(edited, claims={"claims": []})
    assert world.last_calls == ["claims"]


def test_a_run_reads_every_cited_page_at_once(world, monkeypatch):
    world.baseline()
    calls = []
    fetch_many = world.web.fetch_many
    monkeypatch.setattr(
        world.web,
        "fetch_many",
        lambda urls, **options: calls.append(urls) or fetch_many(urls, **options),
    )
    world.run()
    assert calls == [[A, B, C]]


def test_a_first_run_waiting_for_an_answer_continues_where_it_stopped(world):
    pending = AnswerPending(Path("requests/x.md"))
    with pytest.raises(AnswerPending):
        world.run(claims=BASELINE["claims"], judge=[backed(1, 1), pending])
    assert world.app.store.last_runs(WATCH.name) == []

    resumed = world.run(judge=[backed(2, 2), backed(3, 3)])
    assert world.last_calls == ["judge", "judge"]
    assert resumed.baseline
    assert world.labels(resumed) == [("new", "backed")] * 3


@responses.activate
def test_a_citation_watch_reads_only_the_public_internet(tmp_path, monkeypatch):
    public, private = "http://93.184.216.34/stations", "http://127.0.0.1:8080/stations"
    body = f"{fact(1)}. " * 12
    responses.add(
        responses.GET,
        public,
        body=f"<html><body><article><p>{body}</p></article></body></html>",
        content_type="text/html",
    )
    model = ScriptedBackend({"claims": [listed((1, 1), (2, 2))], "judge": [backed(1, 1)]})
    monkeypatch.setattr("scout.app.make_backend", lambda settings: model)
    asked = []
    researcher = App.researcher
    monkeypatch.setattr(
        App,
        "researcher",
        lambda self, **options: asked.append(options) or researcher(self, **options),
    )
    goal = f"{fact(1)} [1]. {fact(2)} [2].\n\n[1] {public}\n[2] {private}"
    with App(Settings(data_dir=tmp_path, reports_dir=tmp_path / "reports")) as app:
        outcome = run_watch(app, replace(WATCH, goal=goal))

    assert [options["public_only"] for options in asked] == [True]
    assert [delta.fact.value for delta in outcome.rulings] == ["backed", "unreadable"]
    assert outcome.result.claims[1].problems == (
        "could not read [2] 127.0.0.1 (refused: it is on a private network (127.0.0.1))",
    )
    assert [call.request.url for call in responses.calls] == [public]


def test_a_page_is_dead_only_when_gone_on_two_runs_an_hour_apart(world):
    world.baseline()
    world.web.pages[C] = Document(
        url=C, status=FetchStatus.NOT_FOUND, fetched_at=NOW, error="HTTP 404"
    )
    assert world.run().alerts == ()  # gone once: missed
    soon = world.run(hours=0.05)  # a run minutes later may be served the same failure
    assert soon.alerts == ()
    assert [(d.change, d.fact.value, d.fact.missed) for d in soon.pages][2] == (
        Change.SAME,
        "read",
        True,
    )
    assert reasons(world.run()) == [f"dead: {C} (not found: HTTP 404), cited for: {fact(3)}."]


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (FetchStatus.TIMEOUT, "abandoned at the fetch deadline"),
        (FetchStatus.SERVER_ERROR, "HTTP 503"),
        (FetchStatus.BLOCKED, "HTTP 429"),
        (FetchStatus.NETWORK_ERROR, "Connection reset by peer"),
    ],
)
def test_a_failure_that_says_nothing_of_the_page_never_kills_it(world, status, error):
    world.baseline()
    world.web.pages[C] = Document(url=C, status=status, fetched_at=NOW, error=error)
    for _ in range(3):
        outcome = world.run()
        assert outcome.alerts == ()
        assert [(d.fact.value, d.fact.missed) for d in outcome.pages][2] == ("read", False)


def test_a_site_that_no_longer_resolves_is_gone(world):
    world.baseline()
    world.web.pages[C] = Document(
        url=C,
        status=FetchStatus.NETWORK_ERROR,
        fetched_at=NOW,
        error="NameResolutionError: Failed to resolve 'three.example'",
    )
    world.run()
    assert [reason.split(" (")[0] for reason in reasons(world.run())] == [f"dead: {C}"]


def test_cited_pages_are_read_a_batch_at_a_time(world):
    many = [f"https://site{n}.example/page" for n in range(1, 31)]
    for url in many:
        world.web.pages[url] = page(1)
    goal = " ".join(f"{fact(1)} [{n}]." for n in range(1, 31))
    goal += "\n\n" + "\n".join(f"[{n}] {url}" for n, url in enumerate(many, start=1))
    batches: list[int] = []
    fetch_many = world.web.fetch_many
    world.web.fetch_many = lambda urls, **options: (
        batches.append(len(urls)) or fetch_many(urls, **options)
    )
    claims = [claim(1, n) for n in range(1, 31)]
    world.model.add("claims", *({"claims": claims[i : i + 8]} for i in range(0, 30, 8)))
    world.run(replace(WATCH, goal=goal), judge=[backed(1, n) for n in range(1, 31)])
    batches.clear()
    world.run(replace(WATCH, goal=goal))
    assert sum(batches) == 30
    assert max(batches) <= 12  # each batch under its own fetch deadline


def test_a_notification_shows_its_alerts_however_long_the_watched_text(world):
    sentences, sources = THREE.split("\n\n")
    long = replace(WATCH, goal=" ".join([sentences] * 80) + "\n\n" + sources)
    record = AlertRecord(
        id=1,
        watch=long.name,
        run_id=1,
        created_at=NOW,
        reason=f"dead: {C} (not found: HTTP 404), cited for: {fact(3)}.",
        url=C,
        quote=f"{fact(3)}.",
        delivered_at=None,
        error=None,
    )
    _, body = digest(long, [record])
    assert len(long.goal) > 10_000
    assert len(body) < 600
    assert body.split("\n")[2].startswith("- dead:")


def dies(world: World, archive: FakeArchive) -> tuple[WatchRun, WatchRun]:
    """C read, then gone on two runs hours apart: (the run that first missed it, the one that
    found it dead)."""
    world.app.archive = archive
    world.baseline()
    world.web.pages[C] = Document(
        url=C, status=FetchStatus.NOT_FOUND, fetched_at=NOW, error="HTTP 404"
    )
    return world.run(), world.run()


def test_a_dead_page_alert_says_whether_its_archived_copy_still_holds_the_quote(
    world, workspace, monkeypatch
):
    taken = world.clock.now - timedelta(days=10)
    archive = FakeArchive({C: (taken, PAGES[C])})
    missed, dead = dies(world, archive)

    assert archive.asked == [(C, missed.result.started_at)]  # before the page was first missed
    replay = Snapshot(C, taken).page
    link = f"{replay}#:~:text=Station%203%20opened%20in%201903%20with%205%20platforms."
    reason = (
        f"dead: {C} (not found: HTTP 404; archived {taken:%Y-%m-%d} with the quote), "
        f"cited for: {fact(3)}."
    )
    assert reasons(dead) == [reason]
    assert dead.copies == {
        f"page|{C}": Copy(Snapshot(C, taken), holds=True, anchor=link.split("#:~:")[1])
    }
    (alert,) = world.app.store.alerts(WATCH.name)
    assert (alert.reason, alert.url, alert.link, alert.quote) == (
        reason,
        replay,
        link,
        f"{fact(3)}.",
    )
    assert link in digest(WATCH, [alert])[1]
    assert f'href="{link}"' in atom(WATCH, [alert], now=NOW)
    (page,) = (f for f in world.app.store.facts(WATCH.name) if f.key == f"page|{C}")
    assert (page.url, page.value) == (C, "not found: HTTP 404")  # the ledger keeps the page

    assert world.run().alerts == ()
    assert len(archive.asked) == 1  # still dead: not looked up again

    WatchBook(workspace / "data" / "watches.yaml").add(WATCH)
    monkeypatch.setattr("scout.cli.watch.run_watch", lambda app, watch, **options: dead)
    printed = CliRunner().invoke(main, ["watch", "run", WATCH.name]).output
    assert f"archived copy: {link}" in printed
    assert f'it said: "{fact(3)}."' in printed


@pytest.mark.parametrize(
    ("copy", "told", "linked"),
    [
        (page(more="Its platforms were rebuilt."), "archived {day} without the quote", "replay"),
        (
            Document(url="x", status=FetchStatus.EMPTY, fetched_at=NOW, error="no readable text"),
            "archived {day}, copy unreadable",
            "replay",
        ),
        (None, "not archived", "page"),
    ],
    ids=["without the quote", "unreadable", "not archived"],
)
def test_a_dead_page_alert_says_what_else_the_archive_holds(world, copy, told, linked):
    taken = world.clock.now - timedelta(days=10)
    _, dead = dies(world, FakeArchive({} if copy is None else {C: (taken, copy)}))
    day = f"{taken:%Y-%m-%d}"
    assert reasons(dead) == [
        f"dead: {C} (not found: HTTP 404; {told.format(day=day)}), cited for: {fact(3)}."
    ]
    (alert,) = world.app.store.alerts(WATCH.name)
    assert alert.link == (Snapshot(C, taken).page if linked == "replay" else C)


def test_an_archive_that_does_not_answer_leaves_the_alert_as_it_was(world):
    archive = FakeArchive({}, down="HTTP 429")
    _, dead = dies(world, archive)
    assert reasons(dead) == [f"dead: {C} (not found: HTTP 404), cited for: {fact(3)}."]
    assert (len(archive.asked), dead.copies) == (1, {})


def test_only_a_page_that_dies_is_looked_up(world, workspace, monkeypatch):
    archive = FakeArchive({})
    world.app.archive = archive
    watch = replace(WATCH, alerts=("changed", "dead", "contradicted"))
    world.web.pages[D] = Document(
        url=D, status=FetchStatus.NOT_FOUND, fetched_at=NOW, error="HTTP 404"
    )
    four = replace(watch, goal=text((1, A), (2, B), (3, C), (4, D)))
    world.run(
        four, claims=listed((1, 1), (2, 2), (3, 3), (4, 4)), judge=[backed(n, n) for n in (1, 2, 3)]
    )
    world.run(four)
    world.run(four)
    assert archive.asked == []  # unreadable from the start: never dead

    alerted = replace(four, alerts=("changed",))
    world.web.pages[C] = Document(
        url=C, status=FetchStatus.NOT_FOUND, fetched_at=NOW, error="HTTP 404"
    )
    world.run(alerted)
    assert archive.asked == []  # missed once
    dead = world.run(alerted)
    assert dead.alerts == ()
    assert [url for url, _ in archive.asked] == [C]  # no dead rule: still told in the run
    assert dead.copies == {f"page|{C}": None}

    WatchBook(workspace / "data" / "watches.yaml").add(alerted)
    monkeypatch.setattr("scout.cli.watch.run_watch", lambda app, watch, **options: dead)
    printed = " ".join(CliRunner().invoke(main, ["watch", "run", WATCH.name]).output.split())
    assert f"dead: {C} (not found: HTTP 404; not archived), cited for: {fact(3)}." in printed


def test_a_watch_run_every_half_hour_finds_a_page_dead_an_hour_after_it_was_first_missed(world):
    archive = FakeArchive({})
    world.app.archive = archive
    world.baseline()
    world.web.pages[C] = Document(
        url=C, status=FetchStatus.NOT_FOUND, fetched_at=NOW, error="HTTP 404"
    )
    first = world.run(hours=0.5)
    assert world.run(hours=0.5).alerts == ()  # half an hour after the first miss
    dead = world.run(hours=0.5)
    assert reasons(dead) == [
        f"dead: {C} (not found: HTTP 404; not archived), cited for: {fact(3)}."
    ]
    assert archive.asked == [(C, first.result.started_at)]


def test_a_copy_the_archive_did_not_serve_leaves_the_alert_as_it_was(world):
    throttled = Document(url="x", status=FetchStatus.BLOCKED, fetched_at=NOW, error="HTTP 429")
    _, dead = dies(world, FakeArchive({C: (world.clock.now - timedelta(days=10), throttled)}))
    assert reasons(dead) == [f"dead: {C} (not found: HTTP 404), cited for: {fact(3)}."]
    assert dead.copies == {}


def test_a_cited_page_redirected_home_alerts_as_dead_with_its_copy_from_before(world):
    older, newer = (world.clock.now - timedelta(days=days) for days in (30, 10))
    missing = "Page not found. Sorry, the page you asked for is not here. " * 4
    archive = FakeArchive({C: [(older, PAGES[C]), (newer, missing)]})
    world.app.archive = archive
    world.baseline()
    world.web.pages[C] = Document(
        url=C,
        status=FetchStatus.OK,
        fetched_at=NOW,
        final_url="https://three.example/",
        title="Three",
        text="Welcome to the line's home page. Plan your journey with us. " * 5,
        content_hash="home",
    )
    missed = world.run()
    assert (missed.alerts, world.last_calls) == ((), [])
    dead = world.run()
    assert world.last_calls == []
    moved = "not found: redirects to its site's home page, https://three.example/"
    assert reasons(dead) == [
        f"dead: {C} ({moved}; archived {older:%Y-%m-%d} with the quote), cited for: {fact(3)}."
    ]
    assert archive.asked == [(C, missed.result.started_at)]
    assert archive.web.fetched == [Snapshot(C, newer).raw, Snapshot(C, older).raw]
    later = world.run()
    assert later.alerts == ()
    for outcome in (missed, dead, later):
        assert world.labels(outcome)[2] == ("same", "backed")
    assert len(world.app.store.alerts(WATCH.name)) == 1
