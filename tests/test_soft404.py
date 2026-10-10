"""Soft 404s: cited pages that answer, yet are gone (redirected home, to an error page or a
domain-sale page, or showing what their site shows for any address)."""

import re

import pytest
import responses

from scout.web.fetch import Document, FetchConfig, Fetcher, FetchStatus
from scout.web.soft404 import error_page, probe, screened
from tests.helpers import NOW, FakeFetcher

DEPOT = "https://news.example.com/2019/depot"
STORY = (
    "The depot opened in 1899 and served the line for decades. Its sheds held twelve engines, "
    "and its yard was the busiest in the county until the line closed in 1962."
)
MISSING = (
    "We looked everywhere but could not find that page. It may have been moved, renamed or "
    "removed. Try the search box above, browse the latest stories, or go back to the front page "
    "of the Daily Bugle, where the news of the day is waiting for you."
)


def read(
    url: str, final: str | None = None, *, text: str = STORY, title: str = "Depot"
) -> Document:
    return Document(
        url=url,
        status=FetchStatus.OK,
        fetched_at=NOW,
        final_url=final,
        title=title,
        text=text,
        content_hash="hash",
    )


def failed(url: str, status: FetchStatus, final: str | None = None) -> Document:
    return Document(url=url, status=status, fetched_at=NOW, final_url=final)


def screen(*documents: Document, web: dict | None = None) -> tuple[list[Document], FakeFetcher]:
    fetcher = FakeFetcher(web or {})
    return screened(fetcher, documents, deadline=5.0), fetcher


def why(document: Document) -> str | None:
    return document.error if document.status is FetchStatus.NOT_FOUND else None


@pytest.mark.parametrize(
    "home",
    [
        "https://news.example.com/",
        "http://www.news.example.com/index.html",
        "https://news.example.com/en/",
        "https://news.example.com/en-us/",
        "https://news.example.com/de/home",
    ],
)
def test_a_page_redirected_to_its_sites_home_page_is_gone(home):
    (page,), fetched = screen(read(DEPOT, home))
    assert (page.url, page.status, page.final_url, page.error) == (
        DEPOT,
        FetchStatus.NOT_FOUND,
        home,
        f"redirects to its site's home page, {home}",
    )
    assert (page.text, page.title, page.content_hash, page.fetched_at) == ("", None, None, NOW)
    assert fetched.fetched == []  # no probe needed


def test_a_page_redirected_to_another_sites_home_page_is_gone_and_names_it():
    (page,), _ = screen(read(DEPOT, "https://www.bugle.example/"))
    assert why(page) == "redirects to the home page of bugle.example, https://www.bugle.example/"


@pytest.mark.parametrize(
    ("cited", "final"),
    [
        ("https://news.example.com/", "https://news.example.com/en/"),
        ("https://news.example.com/en/index.html", "https://news.example.com/"),
        (DEPOT, "https://www.news.example.com/2019/depot/"),
        (DEPOT, "http://news.example.com/2019/Depot"),
        (DEPOT, f"{DEPOT}?utm_source=feed#top"),
        (DEPOT, "https://news.example.com/?next=/2019/depot"),
        ("https://tool.example/?ref=producthunt", "https://tool.example/"),
        ("https://news.example.com/?lang=fr", "https://news.example.com/"),
        ("https://t.co/abc", "https://news.example.com/"),
        ("https://bit.ly/depot", "https://news.example.com/"),
        ("https://doi.org/10.1000/182", "https://news.example.com/"),
        ("https://dx.doi.org/10.1000/182", "https://news.example.com/"),
    ],
    ids=[
        "a home page",
        "a home page, in a language",
        "www and a slash",
        "scheme and case",
        "tracking",
        "home with a query",
        "a referral dropped",
        "a language dropped",
        "t.co",
        "bit.ly",
        "doi.org",
        "a doi.org host",
    ],
)
def test_what_is_no_redirect_to_another_home_page_stands(cited, final):
    (page,), fetched = screen(read(cited, final))
    assert page.ok
    assert fetched.fetched == []


def test_an_address_with_a_query_landing_on_the_bare_home_page_is_gone():
    cited = "https://news.example.com/?p=123"
    (page,), _ = screen(read(cited, "https://news.example.com/"))
    assert why(page) == "redirects to its site's home page, https://news.example.com/"


@pytest.mark.parametrize(
    "final",
    [
        "https://news.example.com/404.html",
        "https://news.example.com/errors/404",
        "https://news.example.com/page-not-found",
        "https://news.example.com/error.aspx?aspxerrorpath=/2019/depot",
        "https://news.example.com/search?code=404",
    ],
)
def test_a_page_redirected_to_an_error_page_is_gone(final):
    (page,), _ = screen(read(DEPOT, final))
    assert why(page) == f"redirects to an error page, {final}"


def test_an_error_word_inside_a_name_or_already_in_the_cited_address_is_no_error_page():
    docs = "https://docs.example.com/tutorial/errors"
    pages, _ = screen(
        read(docs, "https://docs.example.com/tutorial/errors-and-exceptions"),
        read("https://docs.example.com/http/404", "https://docs.example.com/http/404.html"),
    )
    assert [page.status for page in pages] == [FetchStatus.OK] * 2


def test_a_short_link_still_leads_to_an_error_or_domain_sale_page():
    pages, _ = screen(
        read("https://doi.org/10.1000/182", "https://press.example/404.html"),
        read("https://bit.ly/depot", "https://www.hugedomains.com/domain_profile.cfm?d=depot"),
    )
    assert [why(page) for page in pages] == [
        "redirects to an error page, https://press.example/404.html",
        "redirects to a domain-sale page, https://www.hugedomains.com/domain_profile.cfm?d=depot",
    ]


def test_a_page_redirected_to_a_domain_sale_page_is_gone_however_that_page_answered():
    lot = "https://www.hugedomains.com/domain_profile.cfm?d=oldrail.example"
    cited = "http://oldrail.example/history/line"
    pages, _ = screen(read(cited, lot), failed(cited, FetchStatus.BLOCKED, lot))
    assert [why(page) for page in pages] == [f"redirects to a domain-sale page, {lot}"] * 2


@pytest.mark.parametrize("status", [FetchStatus.EMPTY, FetchStatus.NOT_FOUND])
def test_a_page_that_could_not_be_read_is_gone_when_it_was_redirected_home(status):
    (page,), _ = screen(failed(DEPOT, status, "https://news.example.com/"))
    assert why(page) == "redirects to its site's home page, https://news.example.com/"


@pytest.mark.parametrize("status", [FetchStatus.SERVER_ERROR, FetchStatus.TIMEOUT])
def test_a_failure_at_the_end_of_a_redirect_home_says_nothing(status):
    (page,), _ = screen(failed(DEPOT, status, "https://news.example.com/"))
    assert page.status is status


def test_the_probe_is_a_made_up_address_in_the_cited_pages_folder():
    made_up = probe("https://news.example.com/2019/depot.html?id=4#top")
    assert re.fullmatch(r"https://news\.example\.com/2019/scout-[0-9a-f]{12}\.html", made_up)
    assert probe("https://news.example.com/2019/depot.html") == made_up
    assert probe("https://news.example.com/2019/station.html") == made_up
    assert probe("https://news.example.com/2020/depot.html") != made_up
    assert re.fullmatch(
        r"https://x\.example/a/scout-[0-9a-f]{12}/", probe("https://x.example/a/b/")
    )
    assert re.fullmatch(r"https://x\.example/scout-[0-9a-f]{12}", probe("https://x.example/"))
    assert re.fullmatch(r"https://x\.example/scout-[0-9a-f]{12}", probe("https://x.example"))


NOT_FOUND_TITLE = "Page not found | Daily Bugle"


def template(path: str) -> str:
    """The site's page for any unknown address, which names the address asked for."""
    return f"Sorry, {path} is not here. {MISSING}"


def test_a_page_showing_what_its_site_shows_for_any_address_is_gone():
    line = "https://news.example.com/2019/line"
    made_up, above = probe(DEPOT), probe(DEPOT, 2)
    pages, fetched = screen(
        read(DEPOT, text=template("/2019/depot"), title=NOT_FOUND_TITLE),
        read(line, text=template("/2019/line"), title=NOT_FOUND_TITLE),
        web={url: read(url, text=template(url), title=NOT_FOUND_TITLE) for url in (made_up, above)},
    )
    assert [why(page) for page in pages] == [
        'shows what its site shows for any unknown address, "Page not found | Daily Bugle"'
    ] * 2
    assert fetched.fetched == [made_up, above]  # one folder: one probe beside, one above


@pytest.mark.parametrize(
    "answer",
    [
        failed("x", FetchStatus.NOT_FOUND),
        failed("x", FetchStatus.BLOCKED),
        failed("x", FetchStatus.TIMEOUT),
        read("x", text=STORY, title="The Daily Bugle"),
    ],
    ids=["a real 404", "blocked", "timeout", "another page"],
)
def test_a_suspect_stands_unless_the_made_up_address_gets_the_same_answer(answer):
    howto = read(
        "https://news.example.com/help/fixing-404-errors",
        text=f"A 404 error means the server could not find the page. {MISSING}",
        title="Fixing 404 errors",
    )
    (page,), fetched = screen(howto, web={probe(howto.url): answer})
    assert page == howto
    assert fetched.fetched == [probe(howto.url)]


def test_a_page_sent_where_the_site_sends_any_address_is_gone():
    lander = "https://news.example.com/lander"
    made_up, above = probe(DEPOT), probe(DEPOT, 2)
    web = {made_up: read(made_up, f"{lander}/"), above: read(above, lander)}
    (page,), _ = screen(read(DEPOT, lander), web=web)
    assert why(page) == f"redirects where the site sends any unknown address, {lander}"


def test_a_page_moved_elsewhere_on_its_site_stands_when_unknown_addresses_are_not_sent_there():
    moved = "https://news.example.com/stories/depot-1899"
    made_up = probe(DEPOT)
    for answer in (
        failed(made_up, FetchStatus.NOT_FOUND),
        read(made_up, "https://news.example.com/lander"),
    ):
        (page,), fetched = screen(read(DEPOT, moved), web={made_up: answer})
        assert page.ok
        assert fetched.fetched == [made_up]


@pytest.mark.parametrize(
    ("cited", "final"),
    [
        ("https://shop.example/dp/B0X/ref=x", "https://shop.example/dp/B0X"),
        ("https://news.example.com/a", "https://news.example.com/a/index.html"),
        (DEPOT, "https://news.example.com/login?next=/2019/depot"),
        (DEPOT, "https://news.example.com/subscribe/"),
        (DEPOT, "https://consent.example.com/?continue=x"),
        (DEPOT, "https://bugle.example/stories/depot"),
    ],
    ids=["above it", "within it", "login", "subscribe", "consent", "another site"],
)
def test_a_redirect_to_a_related_path_a_wall_or_another_site_is_not_probed(cited, final):
    (page,), fetched = screen(read(cited, final))
    assert page.ok
    assert fetched.fetched == []


def test_ordinary_pages_and_archived_copies_are_never_examined():
    archived = "https://web.archive.org/web/2019/https://news.example.com/2019/depot"
    pages = [
        read(DEPOT),
        failed(DEPOT, FetchStatus.EMPTY),
        read(archived, "https://web.archive.org/web/20190301000000/https://news.example.com/"),
        read(archived, text=template("/2019/depot"), title=NOT_FOUND_TITLE),
    ]
    screened_pages, fetched = screen(*pages)
    assert screened_pages == pages
    assert fetched.fetched == []


@pytest.mark.parametrize(
    ("title", "text", "says"),
    [
        ("Page Not Found", STORY, True),
        ("Error 404", STORY, True),
        ("Depot", "This page doesn\N{RIGHT SINGLE QUOTATION MARK}t exist anymore. " + STORY, True),
        ("oldrail.example", "The domain oldrail.example may be for sale. Buy it now.", True),
        ("Depot", f"{STORY} {STORY} The old page is not found anywhere.", False),
        ("Depot", STORY, False),
        ("Route 4040 timetable", STORY, False),
    ],
)
def test_an_error_page_says_so_in_its_title_or_where_its_text_starts(title, text, says):
    assert error_page(read(DEPOT, text=text, title=title)) is says


def html(title: str, text: str) -> bytes:
    body = f"<body><article><p>{text}</p></article></body>"
    return f"<html><head><title>{title}</title></head>{body}</html>".encode()


@responses.activate
def test_redirects_and_probes_on_the_live_web():
    line = "https://news.example.com/2019/line"
    responses.add(
        responses.GET, DEPOT, status=301, headers={"Location": "https://news.example.com/"}
    )
    home = html("The Daily Bugle", f"{STORY} " * 2)
    responses.add(responses.GET, "https://news.example.com/", body=home, content_type="text/html")
    for url in (line, probe(line), probe(line, 2)):
        missing = html("Page not found", template(url) * 2)
        responses.add(responses.GET, url, body=missing, content_type="text/html")
    with Fetcher(FetchConfig(retries=0)) as fetcher:
        pages = screened(fetcher, fetcher.fetch_many([DEPOT, line]), deadline=10.0)

    assert [why(page) for page in pages] == [
        "redirects to its site's home page, https://news.example.com/",
        'shows what its site shows for any unknown address, "Page not found"',
    ]
    assert [call.request.url for call in responses.calls[-2:]] == [probe(line), probe(line, 2)]
    assert len(responses.calls) == 5


@responses.activate
def test_a_probe_is_read_by_the_fetchers_rules():
    public = "http://93.184.216.34/2019/depot"
    missing = html("Page not found", template("/2019/depot") * 2)
    responses.add(responses.GET, public, body=missing, content_type="text/html")
    responses.add(
        responses.GET, probe(public), status=302, headers={"Location": "http://10.0.0.5/admin"}
    )
    with Fetcher(FetchConfig(public_only=True, retries=0)) as fetcher:
        (page,) = screened(fetcher, fetcher.fetch_many([public]), deadline=10.0)
    assert page.ok
    assert [call.request.url for call in responses.calls] == [public, probe(public)]


# Live pages that moved, as real sites answer them: none is gone.
NPR = (
    "https://www.npr.org/blogs/alltechconsidered/2014/07/21/332678802/one-million-comments",
    "https://www.npr.org/sections/alltechconsidered/2014/07/21/332678802/one-million-comments",
)
REGISTER = (
    "https://www.federalregister.gov/articles/2015/04/13/2015-07841/open-internet",
    "https://www.federalregister.gov/documents/2015/04/13/2015-07841/open-internet",
)


@pytest.mark.parametrize(("cited", "final"), [NPR, REGISTER], ids=["npr", "federal register"])
def test_a_site_that_sends_any_title_to_the_article_its_folder_names_has_not_lost_it(cited, final):
    beside, above = probe(cited), probe(cited, 2)
    web = {beside: read(beside, final), above: failed(above, FetchStatus.NOT_FOUND)}
    (page,), fetched = screen(read(cited, final), web=web)
    assert page.ok
    assert fetched.fetched == [beside, above]


def test_a_page_whose_title_says_404_on_a_site_that_ignores_the_title_in_its_address_stands():
    game = "https://store.steampowered.com/app/1384960/Unit_404/"
    title = "Save 52% on Unit 404 on Steam"
    beside, above = probe(game), probe(game, 2)
    web = {
        beside: read(beside, text=STORY, title=title),  # the same product page, any title
        above: read(above, "https://store.steampowered.com/", text="Featured and recommended"),
    }
    (page,), _ = screen(read(game, text=STORY, title=title), web=web)
    assert page.ok


@pytest.mark.parametrize(
    ("cited", "final"),
    [
        ("https://www.mozilla.org/firefox/", "https://www.firefox.com/en-US/"),
        ("https://www.mozilla.org/en-US/thunderbird/", "https://www.thunderbird.net/en-US/"),
        ("https://github.com/blog", "https://github.blog/"),
        ("https://pypi.python.org/pypi", "https://pypi.org/"),
    ],
    ids=["firefox", "thunderbird", "github blog", "pypi"],
)
def test_a_section_that_became_a_site_of_its_own_stands_when_its_old_site_tells_pages_apart(
    cited, final
):
    beside = probe(cited)
    (page,), fetched = screen(
        read(cited, final), web={beside: failed(beside, FetchStatus.NOT_FOUND)}
    )
    assert page.ok
    assert fetched.fetched == [beside]


def test_a_dead_site_sending_every_address_to_its_successors_home_is_gone():
    cited, final = "https://old.example/product", "https://new.example/"
    beside = probe(cited)
    (page,), _ = screen(read(cited, final), web={beside: read(beside, final)})
    assert why(page) == f"redirects where the site sends any unknown address, {final}"


@pytest.mark.parametrize("query", ["_ga=2.1234.5678", "mod=hp_lead", "CMP=share_btn", "amp=1"])
def test_a_home_page_cited_with_a_tracking_query_is_a_home_page(query):
    (page,), fetched = screen(read(f"https://tool.example/?{query}", "https://tool.example/"))
    assert page.ok
    assert fetched.fetched == []


@pytest.mark.parametrize(
    "final",
    [
        "https://www.congress.gov/bill/113th-congress/house-bill/410",
        "https://blog.example.com/?p=404",
    ],
    ids=["bill 410", "post 404"],
)
def test_a_number_404_or_410_in_an_address_is_no_error_page_out_of_an_error_context(final):
    cited = "https://blog.example.com/2010/05/my-post"
    (page,), _ = screen(read(cited, final))
    assert page.ok
