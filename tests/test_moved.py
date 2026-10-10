"""Moved, not dead: a dead cited page found live at a new address, proven by its own quote."""

import html
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from scout.app import App
from scout.cli import main
from scout.errors import AnswerPending, LLMUnavailable, SearchError
from scout.report import render_html, render_json, render_markdown
from scout.research.citations import relinked
from scout.research.deadlinks import MOVED, REPLACE, dead_links, replacements, summary
from scout.research.factcheck import cited_label, summarize
from scout.research.pipeline import Researcher, ResearchOptions
from scout.research.results import RunResult, Source
from scout.store import Store
from scout.web import moved
from scout.web.archive import Snapshot
from scout.web.fetch import Document, FetchStatus
from tests.helpers import (
    CITE_RESULT,
    NOW,
    Clock,
    FakeArchive,
    FakeFetcher,
    FakeResearcher,
    FakeSearch,
    ScriptedBackend,
)

OLD = "http://www.wpcentral.com/windows-phone-81-features"
HOME = "https://www.windowscentral.com/"
NEW = "https://www.windowscentral.com/windows-phone-81-features"
TAKEN = datetime(2014, 10, 19, 6, 0, tzinfo=UTC)
COPY = Snapshot(OLD, TAKEN).page
BACK = "Back button no longer closes apps, instead it suspends them."
SWIPE = "The keyboard now supports swiping across letters to type whole words."
CORTANA = "Cortana arrives as a personal assistant that learns what you care about."
ARTICLE = " ".join(
    [
        "Here is everything new in the update, from the notification center to the keyboard.",
        "The action center slides down from the top of the screen and holds quick settings.",
        BACK,
        "Apps resume where you left them when you come back to them later.",
        SWIPE,
        CORTANA,
        "The calendar gets a week view and the camera app opens faster than before.",
        "Battery saver shows which apps use the most power in the background.",
    ]
)
NEW_PAGE = f"Windows Central news and reviews. {ARTICLE} Share this story with your friends."
SUSPENDS = "The back button no longer closes apps; it suspends them."
REDIRECTED = Document(
    url=OLD,
    status=FetchStatus.NOT_FOUND,
    fetched_at=NOW,
    final_url=HOME,
    error=f"redirects where the site sends any unknown address, {HOME}",
)

MIX_OLD = "https://teamblog.example.org/wpdev/archive/2010/03/15/the-right-mix.aspx"
MIX_NEW = "https://blogs.example.org/windowsdeveloper/2010/03/15/the-right-mix/"
INDEX = "https://blogs.example.org/windowsdeveloper/page/2/"
MIX = (
    "At MIX we said that more than half a million Silverlight developers are now Windows Phone "
    "developers as well."
)
PHRASE = "At MIX we said that more than half a million Silverlight developers are now Windows Phone"
MIX_QUERY = f'"{PHRASE}" site:example.org'
SILVERLIGHT = "More than half a million Silverlight developers are now Windows Phone developers."
MIX_PAGE = " ".join(
    [
        "This week in Las Vegas we showed developers the tools for the new phone.",
        "The tools are free, and they work with the languages developers already know.",
        MIX,
        "Games can be written with the same framework that powers games on the console.",
        "The emulator runs on any recent laptop, so there is no need for a device to start.",
        "Applications reach customers through a single marketplace on every phone.",
    ]
)
STATIONS = "https://gone.example.net/stations"
STATION = "Station 2 opened in 1902 and served the line for sixty years."
OPENED = "Station 2 opened in 1902."
STATION_PAGE = (
    f"The railway society keeps the history of every station on the line. {STATION} Its "
    "building still stands beside the old goods yard near the river."
)
BOTH = f"{SILVERLIGHT[:-1]} [1]. {OPENED[:-1]} [2].\n\n[1]: {MIX_OLD}\n[2]: {STATIONS}\n"


def cites(sentence: str, url: str) -> str:
    return f"{sentence[:-1]} [1].\n\n[1]: {url}\n"


TEXT = cites(SUSPENDS, OLD)


def claim(text: str, n: int = 1) -> dict:
    return {"claim": text, "excerpt": f"{text[:-1]} [{n}].", "query": "q"}


SUSPENDING = [claim(SUSPENDS)]


def judged(stance: str, quote: str, source: int = 2) -> dict:
    evidence = [{"stance": stance, "quote": quote, "source": source, "says": quote}]
    return {"evidence": evidence, "note": ""}


def gone(url: str) -> Document:
    return Document(url=url, status=FetchStatus.NOT_FOUND, fetched_at=NOW, error="HTTP 404")


def check(
    web: FakeFetcher,
    judges,
    *,
    text: str = TEXT,
    claims=SUSPENDING,
    archive: FakeArchive | None = None,
    search: FakeSearch | None = None,
    pins=None,
    audit: bool = False,
    **options,
) -> tuple[RunResult, ScriptedBackend]:
    """A cite-check of *text* on *web*, its dead pages looked up in *archive*."""
    backend = ScriptedBackend({"claims": [{"claims": list(claims)}], "judge": list(judges)})
    researcher = Researcher(
        search=search or FakeSearch({}),
        fetcher=web,
        backend=backend,
        options=ResearchOptions(**options),
        clock=Clock(),
        pins=pins,
        archive=archive or FakeArchive({OLD: (TAKEN, ARTICLE)}),
    )
    return researcher.check(text, cited=True, audit=audit), backend


def test_the_same_path_on_the_host_it_redirects_to():
    assert (
        moved.same_path("http://www.wpcentral.com/x?id=3", HOME)
        == "https://www.windowscentral.com/x?id=3"
    )
    assert (
        moved.same_path("https://blog.example.com/a", "https://www.example.com/")
        == "https://www.example.com/a"
    )


@pytest.mark.parametrize(
    ("url", "landed"),
    [
        (OLD, None),
        ("http://wpcentral.com/x", "https://www.wpcentral.com/"),  # its own host
        ("http://old.example/", "https://new.example/"),  # the very address it was sent to
    ],
    ids=["no redirect", "its own host", "where it was sent"],
)
def test_no_same_path_without_a_redirect_to_another_host(url, landed):
    assert moved.same_path(url, landed) is None


def test_the_phrase_searched_is_the_copys_own_words_unquoted_and_capped():
    copy = (
        'On stage he said "the back button no longer closes apps, instead it suspends them so '
        'they resume where you left off" to applause.'
    )
    quote = (
        "\N{LEFT DOUBLE QUOTATION MARK}the back button no longer closes apps, it suspends them "
        "so they resume where you left off\N{RIGHT DOUBLE QUOTATION MARK} to applause."
    )
    assert moved.phrase(copy, quote) == (
        "the back button no longer closes apps, instead it suspends them so they resume where you"
    )
    assert moved.phrase(copy, "the back button no longer") is None


def test_the_site_it_redirects_to_is_searched_before_its_own():
    old = "http://windowsteamblog.com/windows_phone/b/wpdev/archive/2010/03/15/the-right-mix.aspx"
    assert moved.sites(old, "https://blogs.windows.com/") == ["windows.com", "windowsteamblog.com"]
    assert moved.sites(MIX_OLD, None) == ["example.org"]
    assert moved.sites(MIX_OLD, "https://www.example.org/404") == ["example.org"]


def post(n: int) -> str:
    return f"Story {n}: " + " ".join(f"w{n}x{i}" for i in range(80))


def test_a_candidate_holds_most_of_the_copy_and_is_mostly_the_copy():
    assert moved.same_page(ARTICLE, NEW_PAGE)
    index = "\n\n".join([*(post(n) for n in range(1, 6)), ARTICLE])
    assert moved.same_page(ARTICLE, index) is False  # it carries the article among others
    words = ARTICLE.split()
    assert moved.same_page(ARTICLE, " ".join(words[: len(words) * 4 // 10])) is False


def test_a_page_moved_to_another_domain_is_found_at_the_same_path_without_a_search():
    web, search = FakeFetcher({OLD: REDIRECTED, NEW: NEW_PAGE}), FakeSearch({})
    result, backend = check(web, [judged("supports", BACK)], search=search)

    assert backend.purposes() == ["claims", "judge"]
    assert (search.calls, web.fetched) == ([], [OLD, NEW])
    (checked,) = result.claims
    assert cited_label(checked, result.sources) == "unreadable"  # its link is still dead
    page = result.sources[-1]
    assert (page.index, page.url, page.moved_from, page.query) == (3, NEW, 1, "new address of [1]")
    assert checked.archived.pages == (2,)  # judged on the copy alone
    on_copy, on_new = (result.numbered[n - 1] for n in checked.archived.supports)
    assert (on_copy.source, on_new.source, on_new.quote, on_new.trusted) == (2, 3, BACK, True)
    (link,) = dead_links(result)
    assert (link.state, link.backs, link.moved) == (MOVED, (1,), page)
    assert link.link == f"{NEW}#:~:{on_new.anchor}"
    assert result.answer.endswith(
        "1 of 1 unreadable claims was judged on archived copies of the pages it cites: 1 backed. "
        "1 dead cited page lives on at a new address."
    )
    assert f'did: "{BACK}" Cite it instead.' in render_markdown(result)


def test_memory_learns_the_quote_where_the_page_lives_now():
    result, _ = check(FakeFetcher({OLD: REDIRECTED, NEW: NEW_PAGE}), [judged("supports", BACK)])
    with Store(":memory:") as store:
        store.add_run(result)
        learned = store.recall("back button suspends apps", limit=5)
    assert [(memory.quote, memory.url) for memory in learned] == [(BACK, NEW)]


def test_relocation_asks_the_model_nothing():
    found, asked = check(FakeFetcher({OLD: REDIRECTED, NEW: NEW_PAGE}), [judged("supports", BACK)])
    missed, unasked = check(FakeFetcher({OLD: REDIRECTED}), [judged("supports", BACK)])
    assert [link.state for link in dead_links(found) + dead_links(missed)] == [MOVED, REPLACE]
    assert [request.messages for request in asked.requests] == [
        request.messages for request in unasked.requests
    ]


def test_find_moved_searches_the_site_for_a_sentence_of_the_copy():
    index = "\n\n".join([*(post(n) for n in range(1, 6)), MIX_PAGE])
    web = FakeFetcher({MIX_OLD: gone(MIX_OLD), INDEX: index, MIX_NEW: f"Blog. {MIX_PAGE}"})
    search = FakeSearch({MIX_QUERY: [(INDEX, "Page 2", ""), (MIX_NEW, "The right mix", "")]})
    result, _ = check(
        web,
        [judged("supports", MIX)],
        text=cites(SILVERLIGHT, MIX_OLD),
        claims=[claim(SILVERLIGHT)],
        archive=FakeArchive({MIX_OLD: (TAKEN, MIX_PAGE)}),
        search=search,
        find_moved=True,
    )

    assert search.calls == [{"query": MIX_QUERY, "recency": None, "news": False, "region": "us-en"}]
    assert web.fetched == [MIX_OLD, INDEX, MIX_NEW]  # the index page read, and passed over
    (link,) = dead_links(result)
    assert (link.state, link.moved.url) == (MOVED, MIX_NEW)


def test_a_new_page_that_no_longer_states_the_quote_leaves_the_copy_to_cite():
    edited = NEW_PAGE.replace(BACK, "Pressing back now quits the app you are in.")
    assert moved.same_page(ARTICLE, edited)  # the page it moved to, but edited since
    result, _ = check(FakeFetcher({OLD: REDIRECTED, NEW: edited}), [judged("supports", BACK)])
    assert [source.moved_from for source in result.sources] == [None, None]
    (link,) = dead_links(result)
    assert (link.state, link.moved, link.link.startswith(COPY)) == (REPLACE, None, True)


def test_a_copy_that_backs_no_claim_is_never_looked_for_elsewhere():
    closes = "Back button still closes apps, as it always did."
    web, search = FakeFetcher({OLD: REDIRECTED, NEW: NEW_PAGE}), FakeSearch({})
    result, _ = check(
        web,
        [judged("refutes", closes)],
        archive=FakeArchive({OLD: (TAKEN, ARTICLE.replace(BACK, closes))}),
        search=search,
        find_moved=True,
    )
    assert (web.fetched, search.calls) == ([OLD], [])
    assert [link.state for link in dead_links(result)] == ["contradicted"]


def test_a_candidate_gone_though_it_answers_is_not_where_the_page_moved():
    home = Document(
        url=NEW,
        status=FetchStatus.OK,
        fetched_at=NOW,
        final_url=HOME,
        title="Windows Central",
        text=NEW_PAGE,  # its home page features the article
        content_hash="home",
    )
    web = FakeFetcher({OLD: REDIRECTED, NEW: home})
    result, _ = check(web, [judged("supports", BACK)])
    assert web.fetched == [OLD, NEW]
    assert [link.state for link in dead_links(result)] == [REPLACE]


def test_a_page_on_the_checked_pages_own_site_is_never_where_the_page_moved():
    notes, old = "https://www.example.com/notes", "https://teamblog.example.net/2010/the-right-mix"
    same = "https://www.example.com/2010/the-right-mix"
    page = Document(
        url=notes,
        status=FetchStatus.OK,
        fetched_at=NOW,
        title="Notes",
        text=f"{SILVERLIGHT[:-1]} [1].\n\n[1] {old}",
    )
    landed = Document(
        url=old,
        status=FetchStatus.NOT_FOUND,
        fetched_at=NOW,
        final_url="https://www.example.com/",
        error="HTTP 404",
    )
    web = FakeFetcher({notes: page, old: landed, same: f"Notes. {MIX_PAGE}"})
    search = FakeSearch({f'"{PHRASE}" site:example.com': [(same, "The right mix", "")]})
    result, _ = check(
        web,
        [judged("supports", MIX)],
        text=notes,
        claims=[claim(SILVERLIGHT)],
        archive=FakeArchive({old: (TAKEN, MIX_PAGE)}),
        search=search,
        find_moved=True,
    )
    assert same not in web.fetched
    assert [call["query"] for call in search.calls] == [
        f'"{PHRASE}" site:example.com',
        f'"{PHRASE}" site:example.net',
    ]
    assert [link.state for link in dead_links(result)] == [REPLACE]


class DownSearch(FakeSearch):
    def search(self, query, **options):
        self.calls.append({"query": query})
        raise SearchError("SearXNG at https://searx.example/search answered HTTP 502")


TWO_DEAD = {MIX_OLD: gone(MIX_OLD), STATIONS: gone(STATIONS), MIX_NEW: f"Blog. {MIX_PAGE}"}
COPIES = {MIX_OLD: (TAKEN, MIX_PAGE), STATIONS: (TAKEN, STATION_PAGE)}
LISTED = [claim(SILVERLIGHT, 1), claim(OPENED, 2)]


def test_a_search_that_fails_is_told_and_tried_again_by_the_next_run():
    down = DownSearch({})
    judges = [judged("supports", MIX, 3), judged("supports", STATION, 4)]
    result, _ = check(
        FakeFetcher(TWO_DEAD),
        judges,
        text=BOTH,
        claims=LISTED,
        archive=FakeArchive(COPIES),
        search=down,
        find_moved=True,
    )
    assert [call["query"] for call in down.calls] == [MIX_QUERY]  # [2] is not searched for
    assert (
        "claim 1: the search engine did not answer (SearXNG at https://searx.example/search "
        "answered HTTP 502): later dead pages were not looked for at new addresses"
    ) in result.warnings
    assert [link.state for link in dead_links(result)] == [REPLACE, REPLACE]

    web, archive = FakeFetcher(TWO_DEAD), FakeArchive(COPIES)
    with Store(":memory:") as store:
        with pytest.raises(AnswerPending):
            check(
                web,
                [judges[0], AnswerPending(Path("requests/x.md"))],
                text=BOTH,
                claims=LISTED,
                archive=archive,
                search=DownSearch({}),
                pins=store,
                find_moved=True,
            )
        search = FakeSearch({MIX_QUERY: [(MIX_NEW, "The right mix", "")]})
        result, _ = check(
            web,
            [judges[0], judged("supports", STATION, 5)],
            text=BOTH,
            claims=LISTED,
            archive=archive,
            search=search,
            pins=store,
            find_moved=True,
        )
    assert search.calls[0]["query"] == MIX_QUERY  # the failed search was not kept
    assert [link.state for link in dead_links(result)] == [MOVED, REPLACE]


def stopped_audit(store: Store, web: FakeFetcher, search: FakeSearch) -> None:
    """An audit of BOTH that looked for where [1] moved, then stopped: the model went down."""
    with pytest.raises(LLMUnavailable):
        check(
            web,
            [judged("supports", MIX, 3), LLMUnavailable("the model server is down")],
            text=BOTH,
            claims=LISTED,
            archive=FakeArchive(COPIES),
            search=search,
            pins=store,
            audit=True,
            find_moved=True,
        )


def test_a_resumed_audit_neither_searches_nor_reads_where_a_page_moved_again():
    web, search = FakeFetcher(TWO_DEAD), FakeSearch({MIX_QUERY: [(MIX_NEW, "The right mix", "")]})
    with Store(":memory:") as store:
        stopped_audit(store, web, search)
        assert ([call["query"] for call in search.calls], web.fetched.count(MIX_NEW)) == (
            [MIX_QUERY],
            1,
        )
        resumed, backend = check(
            web,
            [judged("supports", STATION, 5)],
            text=BOTH,
            claims=LISTED,
            archive=FakeArchive(COPIES),
            search=search,
            pins=store,
            audit=True,
            find_moved=True,
        )
    assert backend.purposes() == ["judge"]
    assert [call["query"] for call in search.calls] == [
        MIX_QUERY,
        f'"{moved.phrase(STATION_PAGE, STATION)}" site:example.net',
    ]
    assert web.fetched.count(MIX_NEW) == 1
    assert [link.state for link in dead_links(resumed)] == [MOVED, REPLACE]

    # Without find_moved, the page found by a search is not served from the pins.
    with Store(":memory:") as store:
        stopped_audit(store, FakeFetcher(TWO_DEAD), search)
        unsearched, _ = check(
            FakeFetcher(TWO_DEAD),
            [judged("supports", STATION, 4)],
            text=BOTH,
            claims=LISTED,
            archive=FakeArchive(COPIES),
            pins=store,
            audit=True,
        )
    assert [link.state for link in dead_links(unsearched)] == [REPLACE, REPLACE]


NOT_SWIPING = "The keyboard does not support swiping across letters."
ASSISTANT = "Cortana arrives as a personal assistant."
THREE = (
    " ".join(f"{sentence[:-1]} [1]." for sentence in (SUSPENDS, NOT_SWIPING, ASSISTANT))
    + f"\n\n[1]: {OLD}\n"
)
WHY = f"not found: redirects where the site sends any unknown address, {HOME}"


def three_claims() -> RunResult:
    """THREE checked where [1] moved: its new page still states claim 1, its copy contradicts
    claim 2, and claim 3's quote was edited away from the new page."""
    edited = NEW_PAGE.replace(CORTANA, "Cortana, the new assistant, comes to more countries soon.")
    result, _ = check(
        FakeFetcher({OLD: REDIRECTED, NEW: edited}),
        [judged("supports", BACK[:-1]), judged("refutes", SWIPE), judged("supports", CORTANA)],
        text=THREE,
        claims=[claim(SUSPENDS), claim(NOT_SWIPING), claim(ASSISTANT)],
    )
    return result


def test_a_page_that_moved_is_cited_at_its_new_address_for_what_it_still_states():
    result = three_claims()
    (link,) = dead_links(result)
    assert (link.state, link.claims, link.backs, link.contradicts) == (MOVED, (1, 2, 3), (1,), (2,))
    assert (link.copy.url, link.quote.quote) == (COPY, BACK[:-1])
    assert link.link.startswith(f"{NEW}#:~:text=")
    assert link.to_dict()["moved"] == NEW
    assert summary([link]) == (
        "1 cited page is gone: 1 moved, and its new address still states what the text cites it "
        "for."
    )
    assert (
        f"- [1] <{OLD}> ({WHY}): moved to <{NEW}>, which still states claim 1 word for word, as "
        f'its archived copy of 2014-10-19 did: "{BACK[:-1]}". Cite it instead. Its archived copy '
        "contradicts claim 2: correct that sentence. It does not state claim 3 word for word: "
        f"check that sentence or cite another source.\n  <{link.link}>"
    ) in render_markdown(result)


def test_a_fix_cites_the_new_address_and_changes_nothing_else():
    (link,) = dead_links(three_claims())
    fixed = relinked(THREE, replacements([link]))
    assert fixed.text == THREE.replace(f"[1]: {OLD}", f"[1]: {link.link}")


def test_the_report_shows_where_the_page_moved():
    result = three_claims()
    (link,) = dead_links(result)
    on_new = result.numbered[1]
    markdown = render_markdown(result)
    assert f"   - archived copy of [1] (2014-10-19): backed; [1] moved to <{NEW}>\n" in markdown
    assert (
        f'     - confirms (finding 2): "{BACK[:-1]}" ([[1] at its new address]'
        f"({NEW}#:~:{on_new.anchor}), windowscentral.com)\n"
    ) in markdown
    assert "| new address of \\[1\\] |" in markdown

    page = render_html(result)
    assert f'moved to <a href="{NEW}">{NEW}</a>' in page.split("<h2>Claims</h2>")[1]
    dead = page.split("<h2>Dead links</h2>")[1].split("<h2>Sources</h2>")[0]
    assert f'): moved to <a href="{NEW}">{NEW}</a>, which still states claim 1' in dead
    assert f'<br><a href="{html.escape(link.link)}">' in dead
    assert "windowscentral.com, new address of [1]</td>" in page

    data = json.loads(render_json(result))
    assert [source["moved_from"] for source in data["sources"]] == [None, None, 1]
    (dead_link,) = data["dead_links"]
    assert (dead_link["state"], dead_link["moved"], dead_link["link"]) == ("moved", NEW, link.link)


def test_the_verdict_counts_the_pages_that_live_on_at_new_addresses():
    def new_address(index: int, of: int) -> Source:
        url = f"https://new.example/{of}"
        return Source(index, url, url, "new.example", "ok", f"new address of [{of}]", moved_from=of)

    claims, findings = CITE_RESULT.claims, CITE_RESULT.findings
    two = [*CITE_RESULT.sources, new_address(5, 4), new_address(6, 1)]
    answer, _ = summarize(claims, findings, two, cited=True)
    assert answer == f"{CITE_RESULT.answer} 2 dead cited pages live on at new addresses."
    answer, _ = summarize(claims, findings, CITE_RESULT.sources, cited=True)
    assert answer == CITE_RESULT.answer


def test_runs_kept_before_pages_could_move_read_as_they_did():
    result, _ = check(FakeFetcher({OLD: REDIRECTED}), [judged("supports", BACK)])
    data = json.loads(render_json(result))
    for source in data["sources"]:
        del source["moved_from"]
    older = RunResult.from_dict(data)
    assert [source.moved_from for source in older.sources] == [None, None]
    assert [link.to_dict() for link in dead_links(older)] == [
        link.to_dict() for link in dead_links(result)
    ]

    kept = RunResult.from_dict(json.loads(render_json(three_claims())))
    assert [source.moved_from for source in kept.sources] == [None, None, 1]
    assert [link.state for link in dead_links(kept)] == [MOVED]


def command_line(monkeypatch, web: FakeFetcher, judges) -> None:
    """The command line's model, web and archive, for runs of TEXT."""
    archive = FakeArchive({OLD: (TAKEN, ARTICLE)})
    monkeypatch.setattr("scout.app.make_archive", lambda spec, fetcher: archive)
    monkeypatch.setattr("scout.app.Fetcher", lambda *args, **options: web)
    backend = ScriptedBackend({"claims": [{"claims": [claim(SUSPENDS)]}], "judge": judges})
    monkeypatch.setattr("scout.app.make_backend", lambda settings: backend)
    Path("wp.md").write_text(TEXT, encoding="utf-8", newline="")


def test_the_command_line_fixes_a_moved_citation_with_its_new_address(workspace, monkeypatch):
    command_line(
        monkeypatch, FakeFetcher({OLD: REDIRECTED, NEW: NEW_PAGE}), [judged("supports", BACK)]
    )
    args = ["factcheck", "--cited", "--fix", "wp.fixed.md", "--json", "-f", "wp.md"]
    done = CliRunner().invoke(main, args)
    assert done.exit_code == 0, done.output

    data = json.loads(done.stdout)
    (link,) = data["dead_links"]
    assert (link["state"], data["fixed"]["replaced"]) == ("moved", [1])
    fixed = Path("wp.fixed.md").read_text(encoding="utf-8")
    assert fixed == TEXT.replace(f"[1]: {OLD}", f"[1]: {link['link']}")
    told = " ".join(done.stderr.split())
    assert "Fixed text: wp.fixed.md (1 dead citation replaced by its new address)" in told
    assert "--find-moved" not in told


HINT = (
    "1 dead citation can cite an archived copy: --find-moved also searches its site "
    "(SCOUT_SEARCH) for one sentence of the copy, never your text, to cite the page where it moved"
)


def test_the_command_line_offers_find_moved_and_checks_its_rules(workspace, monkeypatch):
    for args, message in (
        (["--find-moved", "Some text."], "--find-moved applies to --cited only"),
        (
            ["--cited", "--find-moved", "--allow-private", TEXT],
            "--archive sends the addresses of unreadable cited pages to web.archive.org",
        ),
    ):
        refused = CliRunner().invoke(main, ["factcheck", *args])
        assert refused.exit_code == 2
        assert message in " ".join(refused.output.split())
    monkeypatch.setenv("SCOUT_ARCHIVE", "off")
    off = CliRunner().invoke(main, ["factcheck", "--cited", "--find-moved", TEXT])
    assert off.exit_code == 1
    assert "error: archive lookups are off (SCOUT_ARCHIVE=off)" in off.output

    copied, _ = check(FakeFetcher({OLD: REDIRECTED}), [judged("supports", BACK)])
    asked = []
    monkeypatch.setattr(
        App, "researcher", lambda self, **options: asked.append(options) or FakeResearcher(copied)
    )
    archived = CliRunner().invoke(main, ["factcheck", "--cited", "--archive", "--no-save", TEXT])
    assert HINT in " ".join(archived.stderr.split())
    searched = CliRunner().invoke(main, ["factcheck", "--cited", "--find-moved", "--no-save", TEXT])
    assert searched.exit_code == 0, searched.output
    assert "--find-moved" not in searched.stderr
    assert [(options["archive"], options["find_moved"]) for options in asked] == [
        (True, False),
        (True, True),
    ]
