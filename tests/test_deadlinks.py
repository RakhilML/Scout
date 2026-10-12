"""Dead citations: what each dead cited page's archived copy said, and the text relinked to the
copies that back what it cites them for."""

from datetime import UTC, datetime

import pytest

from scout.report import render_markdown
from scout.research import factcheck
from scout.research.citations import relinked
from scout.research.deadlinks import (
    CONTRADICTED,
    COPY_UNREADABLE,
    NOT_ARCHIVED,
    NOT_FOUND,
    NOT_JUDGED,
    NOT_LOOKED_UP,
    REPLACE,
    DeadLink,
    dead_links,
    replacements,
    summary,
)
from scout.research.pipeline import Researcher
from scout.research.results import RunResult
from scout.web import soft404
from scout.web.archive import Snapshot
from scout.web.domains import canonical_url
from scout.web.fetch import Document, FetchStatus
from tests.helpers import (
    CHECK_RESULT,
    CITE_RESULT,
    NOW,
    Clock,
    FakeArchive,
    FakeFetcher,
    FakeSearch,
    ScriptedBackend,
)

GONE = "https://example.org/gone"
LINK = f"https://web.archive.org/web/20241102083000/{GONE}#:~:text=It%20runs%20on%20iOS."
WIKI = "https://en.wikipedia.org/wiki/Foo_(bar)"
WIKI_COPY = f"https://web.archive.org/web/20200101000000/{WIKI}"
CITING = "\r\n".join(
    [
        "Python 3.13 runs on iOS [1]. It is a tier 3 platform [2]. Android is next [^3].",
        'Read [the notes]({dead}), [the page]({dead} "Gone page"), <{dead}>, {dead}. Or [{dead}].',
        "[![a logo](https://img.example/logo.png)]({dead}) {slash} {tracked} {upper} "
        "[a wiki]({wiki})",
        "Words: `{kept}`, ![a picture]({kept}), {kept}/more, {kept}.html, https://example.org/kept.",
        "```",
        "curl {kept}",
        "```",
        "[1]: {dead}",
        "[2] {dead}",
        "[^3]: Lee, The gone page, {dead}.",
        "",
        "Sources",
        "1. The gone page {dead}",
        "",
    ]
)


def test_a_dead_address_is_replaced_wherever_and_however_the_text_cites_it():
    given = CITING.format(
        dead=GONE,
        slash=f"{GONE}/",
        tracked=f"{GONE}?utm_source=news",
        upper="https://EXAMPLE.org/gone",
        wiki=WIKI,
        kept=GONE,
    )
    links = {canonical_url(GONE): LINK, canonical_url(WIKI): WIKI_COPY}
    expected = CITING.format(
        dead=LINK, slash=LINK, tracked=LINK, upper=LINK, wiki=WIKI_COPY, kept=GONE
    )
    assert relinked(given, links) == (expected, frozenset(links))
    assert relinked(given, {}) == (given, frozenset())
    # An address as cited() reads it: a soft hyphen or a zero-width space in it is cleaned away.
    hidden = "See [the page](https://exam\N{SOFT HYPHEN}ple.org/go\N{ZERO WIDTH SPACE}ne)."
    assert relinked(hidden, links) == (f"See [the page]({LINK}).", frozenset({canonical_url(GONE)}))


STATIONS = ("https://gone1.example/stations", "https://gone2.example/stations")
TAKEN = datetime(2024, 11, 2, 8, 30, tzinfo=UTC)
OPENED = "Station 1 opened in 1901."


def dead(url: str, status: FetchStatus = FetchStatus.NOT_FOUND, error: str = "HTTP 404"):
    return Document(url=url, status=status, fetched_at=NOW, error=error)


def checked(urls, archive: FakeArchive, judges=(), web=None) -> RunResult:
    """A cite-check of one sentence per page in *urls*, "Station n opened in 190n [n].", each
    page gone unless *web* says otherwise."""
    numbered = list(enumerate(urls, start=1))
    text = " ".join(f"Station {n} opened in {1900 + n} [{n}]." for n, _ in numbered)
    text += "\n\n" + "\n".join(f"[{n}] {url}" for n, url in numbered)
    listed = {
        "claims": [
            {
                "claim": f"Station {n} opened in {1900 + n}.",
                "excerpt": f"Station {n} opened in {1900 + n} [{n}].",
                "query": "q",
            }
            for n, _ in numbered
        ]
    }
    backend = ScriptedBackend({"claims": [listed], "judge": list(judges)})
    return Researcher(
        search=FakeSearch({}),
        fetcher=FakeFetcher(web or {url: dead(url) for url in urls}),
        backend=backend,
        clock=Clock(),
        archive=archive,
    ).check(text, cited=True)


def judged(stance: str | None = None, quote: str = OPENED) -> dict:
    evidence = [{"stance": stance, "quote": quote, "source": 2, "says": quote}] if stance else []
    return {"evidence": evidence, "note": ""}


COPY = f"https://web.archive.org/web/20241102083000/{STATIONS[0]}"
ON_COPY = f"- [1] <{STATIONS[0]}> (not found: HTTP 404): its archived copy of 2024-11-02"


@pytest.mark.parametrize(
    ("page", "judges", "state", "shown"),
    [
        (
            f"{OPENED} It closed in 1960.",
            [judged("supports")],
            REPLACE,
            f"- [1] <{STATIONS[0]}> (not found: HTTP 404): replace with its archived copy of "
            f'2024-11-02, which backs claim 1: "{OPENED}"\n  <{COPY}#:~:text=',
        ),
        (
            "Station 1 opened in 1911.",
            [judged("refutes", "Station 1 opened in 1911.")],
            CONTRADICTED,
            f'{ON_COPY} contradicts claim 1: "Station 1 opened in 1911."; correct the sentence or '
            "cite another source",
        ),
        (
            "Station 1 is on Main Street.",
            [judged()],
            NOT_FOUND,
            f"{ON_COPY} does not state claim 1; cite another source",
        ),
        (
            OPENED,
            ["not json"] * 4,
            NOT_JUDGED,
            f"{ON_COPY} could not be judged (the model's reply was unusable)",
        ),
    ],
    ids=["backed", "contradicted", "silent", "not judged"],
)
def test_a_copy_is_cited_instead_only_when_it_backs_what_the_page_was_cited_for(
    page, judges, state, shown
):
    result = checked(STATIONS[:1], FakeArchive({STATIONS[0]: (TAKEN, page)}), judges)
    (link,) = dead_links(result)
    assert (link.n, link.state, link.claims, link.copy.url) == (1, state, (1,), COPY)
    assert (link.link is not None) is (state == REPLACE)
    assert bool(replacements([link])) is (state == REPLACE)
    assert shown in render_markdown(result)


EMPTY = Document(
    url=Snapshot(STATIONS[0], TAKEN).raw,
    status=FetchStatus.EMPTY,
    fetched_at=NOW,
    error="no readable main text",
)
INTRANET = "http://wiki.corp/stations"
ARCHIVED = "https://web.archive.org/web/2019/https://old.example/stations"


@pytest.mark.parametrize(
    ("second", "archive", "limit", "found"),
    [
        (
            STATIONS[1],
            FakeArchive({}),
            30,
            [("not archived", None), ("not archived", None)],
        ),
        (
            STATIONS[1],
            FakeArchive({STATIONS[0]: (TAKEN, EMPTY)}),
            30,
            [("copy unreadable", "empty: no readable main text"), ("not archived", None)],
        ),
        (
            STATIONS[1],
            FakeArchive({}, down="HTTP 429"),
            30,
            [("not looked up", "the Wayback Machine did not answer (HTTP 429)")] * 2,
        ),
        (
            STATIONS[1],
            FakeArchive({}),
            1,
            [
                ("not archived", None),
                ("not looked up", "at most 1 cited pages are looked up in a run"),
            ],
        ),
        (
            INTRANET,
            FakeArchive({}),
            30,
            [("not archived", None), ("not looked up", "no public name")],
        ),
        (
            ARCHIVED,
            FakeArchive({}),
            30,
            [("not archived", None), ("not looked up", "it is an archived copy already")],
        ),
    ],
    ids=["no copy", "unreadable copy", "archive down", "past the limit", "intranet", "a copy"],
)
def test_a_dead_page_without_a_copy_to_judge_says_why(monkeypatch, second, archive, limit, found):
    monkeypatch.setattr(factcheck, "ARCHIVE_LIMIT", limit)
    unresolved = dead(INTRANET, FetchStatus.NETWORK_ERROR, "Failed to resolve 'wiki.corp'")
    web = {
        STATIONS[0]: dead(STATIONS[0]),
        second: unresolved if second == INTRANET else dead(second),
    }
    result = checked((STATIONS[0], second), archive, web=web)
    links = dead_links(result)
    assert [(link.state, link.note) for link in links] == found
    assert [link.link for link in links] == [None, None]
    assert [link.claims for link in links] == [(1,), (2,)]
    shown = {
        NOT_ARCHIVED: "not archived; cite another source (claim {n})",
        NOT_LOOKED_UP: "not looked up ({note})",
        COPY_UNREADABLE: "its archived copy of 2024-11-02 could not be read ({note})",
    }
    report = render_markdown(result)
    for link in links:
        assert f"): {shown[link.state].format(n=link.n, note=link.note)}\n" in report


WELCOME = "Welcome to the Gone One railway society. Read our latest news and events. " * 4
HOME = "https://gone1.example/"
MOVED_HOME = "not found: redirects to its site's home page, https://gone1.example/"


def answered(url: str, text: str, *, final: str | None = None, title: str = "") -> Document:
    """A page that answers HTTP 200, as the site serves it now."""
    return Document(
        url=url,
        status=FetchStatus.OK,
        fetched_at=NOW,
        final_url=final,
        title=title,
        text=text,
        content_hash=text,
    )


def test_a_page_redirected_home_is_a_dead_link_its_copy_of_the_cited_address_can_replace():
    page = STATIONS[0]
    text = f"{OPENED[:-1]} [1].\n\n[1] {page}"
    listed = {"claims": [{"claim": OPENED, "excerpt": f"{OPENED[:-1]} [1].", "query": "q"}]}
    backend = ScriptedBackend({"claims": [listed], "judge": [judged("supports")]})
    archive = FakeArchive({page: (TAKEN, f"{OPENED} It closed in 1960.")})
    web = FakeFetcher({page: answered(page, WELCOME, final=HOME)})
    result = Researcher(
        search=FakeSearch({}), fetcher=web, backend=backend, clock=Clock(), archive=archive
    ).check(text, cited=True)

    assert backend.purposes() == ["claims", "judge"]  # the copy is judged, not the home page
    assert web.fetched == [page]
    assert archive.asked == [(page, None)]
    (claim,) = result.claims
    assert claim.problems == (f"could not read [1] gone1.example ({MOVED_HOME})",)
    assert factcheck.cited_label(claim, result.sources) == "unreadable"
    (link,) = dead_links(result)
    assert (link.state, link.why, link.copy.url) == (REPLACE, MOVED_HOME, COPY)
    assert (
        f"- [1] <{page}> ({MOVED_HOME}): replace with its archived copy of 2024-11-02, which "
        f'backs claim 1: "{OPENED}"\n  <{COPY}#:~:text='
    ) in render_markdown(result)
    fixed, replaced = relinked(text, replacements([link]))
    assert fixed == text.replace(f"[1] {page}", f"[1] {link.link}")
    assert replaced == {canonical_url(page)}


def test_a_page_showing_its_sites_error_page_is_replaced_by_the_copy_from_before_it_died():
    page = STATIONS[0]
    missing = "Page not found. The page you asked for is not here; try our search box. " * 4
    shown = answered(page, missing, title="Page not found")
    web = {page: shown, soft404.probe(page): answered("probe", missing, title="Page not found")}
    later = datetime(2025, 6, 1, tzinfo=UTC)
    archive = FakeArchive({page: [(TAKEN, OPENED), (later, missing)]})
    result = checked(STATIONS[:1], archive, [judged("supports")], web=web)

    (link,) = dead_links(result)
    assert (
        link.why == 'not found: shows what its site shows for any unknown address, "Page not found"'
    )
    assert (link.state, link.taken) == (REPLACE, "2024-11-02")
    assert archive.web.fetched == [Snapshot(page, later).raw, Snapshot(page, TAKEN).raw]


def test_only_a_cite_check_that_looked_copies_up_lists_dead_links():
    assert dead_links(CITE_RESULT) == []  # its [4] is dead, but nothing was looked up
    assert dead_links(CHECK_RESULT) == []
    assert "## Dead links" not in render_markdown(CITE_RESULT)


def test_the_list_starts_with_what_can_be_done():
    links = [
        DeadLink(n, f"https://gone{n}.example/", "not found: HTTP 404", state, (n,))
        for n, state in enumerate(
            [
                REPLACE,
                REPLACE,
                NOT_ARCHIVED,
                CONTRADICTED,
                NOT_JUDGED,
                NOT_LOOKED_UP,
                NOT_LOOKED_UP,
            ],
            start=1,
        )
    ]
    assert summary(links) == (
        "7 cited pages are gone: 2 can be replaced by their archived copies, which back claims the "
        "text cites them for; 2 need another source; 1 could not be judged; 2 were not looked up."
    )
    assert summary(links[2:3]) == "1 cited page is gone: 1 needs another source."
