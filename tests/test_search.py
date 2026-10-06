from datetime import date, timedelta
from typing import ClassVar

import pytest
import responses
from ddgs.exceptions import DDGSException

from scout.errors import ConfigError, SearchError
from scout.store import Store
from scout.web import search as search_module
from scout.web.search import (
    CachedSearch,
    DDGSBackend,
    SearchHit,
    make_search_backend,
    merge_hits,
)
from tests.helpers import Clock


class FakeDDGS:
    """Stands in for ddgs.DDGS. `script` holds what successive calls return (rows or exception)."""

    script: ClassVar[list] = []
    calls: ClassVar[list[dict]] = []

    def __init__(self, timeout: int) -> None:
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def _next(self, kind: str, query: str, **kwargs):
        FakeDDGS.calls.append({"kind": kind, "query": query, **kwargs})
        outcome = FakeDDGS.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def text(self, query, **kwargs):
        return self._next("text", query, **kwargs)

    def news(self, query, **kwargs):
        return self._next("news", query, **kwargs)


@pytest.fixture
def fake_ddgs(monkeypatch):
    FakeDDGS.script, FakeDDGS.calls = [], []
    monkeypatch.setattr(search_module, "DDGS", FakeDDGS)
    return FakeDDGS


def hit(url: str, rank: int = 1, query: str = "q") -> SearchHit:
    return SearchHit(url=url, title=url, snippet="", rank=rank, query=query)


def test_text_results_become_hits(fake_ddgs):
    fake_ddgs.script = [
        [
            {
                "title": "RTX\N{NARROW NO-BREAK SPACE}5090 review",
                "href": "https://a.example/r",
                "body": "Fast",
            },
            {"title": "no link", "href": "javascript:void(0)", "body": ""},
        ]
    ]
    hits = DDGSBackend().search(
        "rtx 5090", max_results=5, region="us-en", recency="month", news=False
    )
    assert hits == [
        SearchHit(
            url="https://a.example/r",
            title="RTX 5090 review",
            snippet="Fast",
            rank=1,
            query="rtx 5090",
        )
    ]
    assert fake_ddgs.calls[0]["timelimit"] == "m"


def test_news_results_carry_dates(fake_ddgs):
    fake_ddgs.script = [
        [
            {
                "title": "t",
                "url": "https://n.example/1",
                "body": "b",
                "date": "2026-09-24T08:00:00+00:00",
                "source": "Wire",
            }
        ]
    ]
    (news,) = DDGSBackend().search("q", max_results=3, region="us-en", recency=None, news=True)
    assert (news.published, news.source) == (date(2026, 9, 24), "Wire")
    assert fake_ddgs.calls[0]["kind"] == "news"


def test_throttling_is_retried_once_then_gives_up_quietly(fake_ddgs):
    fake_ddgs.script = [DDGSException("No results found."), DDGSException("No results found.")]
    pauses = []
    backend = DDGSBackend(retry_delay=2.0, sleep=pauses.append)
    assert backend.search("q", max_results=3, region="us-en", recency=None, news=False) == []
    assert pauses == [2.0]


class CountingBackend:
    name = "counting"

    def __init__(self, results):
        self.results = results
        self.calls = 0

    def search(self, query, **kwargs):
        self.calls += 1
        return self.results


def test_cached_search_reuses_recent_results_and_skips_empty_ones():
    clock = Clock()
    with Store(":memory:") as store:
        backend = CountingBackend([hit("https://a.example")])
        cached = CachedSearch(backend, store, max_age=600, clock=clock)
        args = {"max_results": 5, "region": "us-en", "recency": None, "news": False}
        assert cached.search("q", **args) == cached.search("q", **args)
        assert backend.calls == 1
        clock.advance(601)
        cached.search("q", **args)
        assert backend.calls == 2

        empty = CountingBackend([])
        cached_empty = CachedSearch(empty, store, clock=clock)
        cached_empty.search("q2", **args)
        cached_empty.search("q2", **args)
        assert empty.calls == 2


def test_merge_interleaves_by_rank_and_drops_duplicates():
    first = [
        hit("https://a.example/1", 1),
        hit("https://a.example/2", 2),
        hit("https://a.example/3", 3),
    ]
    second = [hit("https://b.example/1", 1), hit("https://a.example/1/?utm_source=x", 2)]
    merged = merge_hits([first, second], limit=10)
    assert [h.url for h in merged] == [
        "https://a.example/1",
        "https://b.example/1",
        "https://a.example/2",
        "https://a.example/3",
    ]
    assert len(merge_hits([first, second], limit=2)) == 2
    assert merge_hits([], limit=5) == []


def test_search_cache_expiry_is_relative_to_clock():
    clock = Clock()
    with Store(":memory:") as store:
        store.put_search("k", [hit("https://a.example")], clock.now - timedelta(hours=2))
        assert store.get_search("k", newer_than=clock.now - timedelta(hours=1)) is None


def titled(url: str, title: str, rank: int) -> SearchHit:
    return SearchHit(url=url, title=title, snippet="", rank=rank, query="q")


def test_merge_drops_ads_and_same_titled_pages_on_one_site():
    ad = titled("https://www.bing.com/aclick?ld=x", "RTX deal", 1)
    page = titled("https://shop.example/5090", "RTX 5090", 2)
    alias = titled("https://shop.example/p?id=5090", "RTX\N{NO-BREAK SPACE}5090", 1)
    other = titled("https://other.example/5090", "RTX 5090", 2)
    merged = merge_hits([[ad, page], [alias, other]], limit=10)
    # the ad is gone; shop.example's second URL for the same page is a duplicate
    assert [hit.url for hit in merged] == [alias.url, other.url]


SEARX = "http://searx.local:8080"


@responses.activate
def test_searxng_results_become_hits():
    responses.add(
        responses.GET,
        f"{SEARX}/search",
        json={
            "results": [
                {
                    "url": "https://news.example/5090",
                    "title": "RTX 5090 price cut",
                    "content": "Prices fell this week.",
                    "publishedDate": "2026-09-20T08:00:00",
                },
                {"url": "ftp://odd.example/file", "title": "not a web page"},
                {"url": "https://shop.example/5090", "title": "RTX 5090", "content": None},
            ]
        },
    )
    backend = make_search_backend(f"searxng:{SEARX}/", timeout=5, user_agent="scout-test")
    hits = backend.search("rtx 5090", max_results=5, region="in-en", recency="week", news=True)
    assert [(hit.url, hit.rank) for hit in hits] == [
        ("https://news.example/5090", 1),
        ("https://shop.example/5090", 3),
    ]
    assert hits[0].published == date(2026, 9, 20)
    sent = responses.calls[0].request
    assert sent.headers["User-Agent"] == "scout-test"
    assert "language=en-IN" in sent.url
    assert "time_range=week" in sent.url
    assert "categories=news" in sent.url
    backend.close()


@responses.activate
def test_a_misconfigured_searxng_says_what_to_fix():
    responses.add(responses.GET, f"{SEARX}/search", status=403)
    backend = make_search_backend(f"searxng:{SEARX}", timeout=5)
    with pytest.raises(SearchError, match=r"add json to search\.formats"):
        backend.search("rtx", max_results=5, region="wt-wt", recency=None, news=False)
    assert "language" not in responses.calls[0].request.url  # all languages


@pytest.mark.parametrize("spec", ["bing", "searxng:", "searxng:localhost:8080", "ddgs:x"])
def test_unknown_search_backends_are_refused(spec):
    with pytest.raises(ConfigError, match="SCOUT_SEARCH"):
        make_search_backend(spec, timeout=5)
