import http.server
import importlib.util
import threading
import time
from dataclasses import replace

import brotli
import pytest
import requests
import responses
from urllib3.response import HTTPResponse

from scout.errors import ConfigError, RenderError
from scout.store import Store
from scout.web.extract import EXTRACTOR_VERSION
from scout.web.fetch import Document, FetchConfig, Fetcher, FetchStatus
from scout.web.render import make_renderer
from tests.helpers import NOW, Clock, make_pdf

URL = "https://site.example/article"
ARTICLE = (
    "<html><head><title>Specs</title></head><body><article><p>"
    + "The card ships with 32 GB of GDDR7 memory. " * 20
    + "</p></article></body></html>"
).encode()


class MemoryCache:
    def __init__(self) -> None:
        self.pages: dict[str, Document] = {}

    def get_page(self, url: str) -> Document | None:
        return self.pages.get(url)

    def put_page(self, doc: Document) -> None:
        self.pages[doc.url] = doc


def make_fetcher(clock: Clock, cache: MemoryCache | None = None, **config) -> Fetcher:
    return Fetcher(FetchConfig(**config), cache, clock=clock, sleep=lambda _seconds: None)


@responses.activate
def test_brotli_page_is_decoded_instead_of_becoming_garbage(clock):
    responses.add(
        responses.GET,
        URL,
        body=brotli.compress(ARTICLE),
        headers={"Content-Encoding": "br", "Content-Type": "text/html; charset=utf-8"},
    )
    doc = make_fetcher(clock).fetch(URL)
    assert doc.status is FetchStatus.OK
    assert "32 GB of GDDR7 memory" in doc.text


@responses.activate
def test_only_decodable_encodings_are_advertised(clock):
    responses.add(responses.GET, URL, body=ARTICLE, content_type="text/html")
    make_fetcher(clock).fetch(URL)
    offered = {
        part.strip() for part in responses.calls[0].request.headers["Accept-Encoding"].split(",")
    }
    assert offered <= set(HTTPResponse.CONTENT_DECODERS)


@pytest.mark.parametrize("content_type", ["application/pdf", "application/octet-stream"])
@responses.activate
def test_pdfs_are_read_as_pdfs(clock, content_type):
    responses.add(
        responses.GET,
        URL,
        body=make_pdf("Attention is all you need " * 10),
        content_type=content_type,
    )
    doc = make_fetcher(clock).fetch(URL)
    assert (doc.status, doc.kind) == (FetchStatus.OK, "pdf")
    assert "Attention is all you need" in doc.text


@responses.activate
def test_binary_body_is_rejected_as_junk(clock):
    responses.add(
        responses.GET,
        URL,
        body=bytes(range(128, 256)) * 30,
        content_type="text/plain; charset=utf-8",
    )
    assert make_fetcher(clock).fetch(URL).status is FetchStatus.JUNK


@responses.activate
def test_unreadable_content_types_are_unsupported(clock):
    responses.add(responses.GET, URL, body=b"\x89PNG....", content_type="image/png")
    doc = make_fetcher(clock).fetch(URL)
    assert doc.status is FetchStatus.UNSUPPORTED
    assert "image/png" in doc.error


@responses.activate
def test_page_without_main_text_is_empty_unless_it_publishes_offers(clock):
    stub = b"<html><head><title>x</title></head><body><p>Hi</p></body></html>"
    responses.add(responses.GET, URL, body=stub, content_type="text/html")
    assert make_fetcher(clock).fetch(URL).status is FetchStatus.EMPTY

    with_offer = stub.replace(
        b"</head>",
        b'<script type="application/ld+json">'
        b'{"@type":"Offer","price":"19.99","priceCurrency":"USD"}</script></head>',
    )
    responses.replace(responses.GET, URL, body=with_offer, content_type="text/html")
    doc = make_fetcher(clock).fetch(URL)
    assert doc.status is FetchStatus.OK
    assert str(doc.offers[0].price) == "19.99"


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (404, FetchStatus.NOT_FOUND),
        (403, FetchStatus.BLOCKED),
        (429, FetchStatus.BLOCKED),
        (400, FetchStatus.HTTP_ERROR),
    ],
)
@responses.activate
def test_client_errors_are_not_retried(clock, code, status):
    responses.add(responses.GET, URL, status=code)
    doc = make_fetcher(clock, retries=3).fetch(URL)
    assert doc.status is status
    assert len(responses.calls) == 1


@responses.activate
def test_server_errors_are_retried(clock):
    responses.add(responses.GET, URL, status=503)
    responses.add(responses.GET, URL, body=ARTICLE, content_type="text/html")
    doc = make_fetcher(clock, retries=1).fetch(URL)
    assert doc.status is FetchStatus.OK
    assert len(responses.calls) == 2


@responses.activate
def test_timeouts_are_retried_then_reported(clock):
    responses.add(responses.GET, URL, body=requests.ConnectTimeout())
    responses.add(responses.GET, URL, body=requests.ConnectTimeout())
    doc = make_fetcher(clock, retries=1).fetch(URL)
    assert doc.status is FetchStatus.TIMEOUT
    assert len(responses.calls) == 2


@responses.activate
def test_skip_listed_sites_are_never_requested(clock):
    doc = make_fetcher(clock).fetch("https://www.wsj.com/articles/x")
    assert doc.status is FetchStatus.SKIPPED
    assert len(responses.calls) == 0


@responses.activate
def test_fresh_cache_hits_skip_the_network(clock):
    cache = MemoryCache()
    responses.add(responses.GET, URL, body=ARTICLE, content_type="text/html")
    fetcher = make_fetcher(clock, cache)
    first = fetcher.fetch(URL)
    clock.advance(60)
    second = fetcher.fetch(URL)
    assert len(responses.calls) == 1
    assert second.from_cache
    assert second.text == first.text


@responses.activate
def test_stale_pages_are_revalidated_with_conditional_requests(clock):
    cache = MemoryCache()
    responses.add(
        responses.GET,
        URL,
        body=ARTICLE,
        content_type="text/html",
        headers={"ETag": '"v1"', "Last-Modified": "Mon, 01 Sep 2026 00:00:00 GMT"},
    )
    fetcher = make_fetcher(clock, cache, fresh_for=60)
    fetcher.fetch(URL)
    clock.advance(3600)
    responses.replace(responses.GET, URL, status=304)
    doc = fetcher.fetch(URL)

    sent = responses.calls[1].request.headers
    assert (sent["If-None-Match"], sent["If-Modified-Since"]) == (
        '"v1"',
        "Mon, 01 Sep 2026 00:00:00 GMT",
    )
    assert doc.status is FetchStatus.OK
    assert doc.from_cache
    assert doc.fetched_at == clock.now
    assert "32 GB" in doc.text


@responses.activate
def test_pages_read_by_an_older_extractor_are_read_again(clock):
    cache = MemoryCache()
    responses.add(
        responses.GET, URL, body=ARTICLE, content_type="text/html", headers={"ETag": '"v1"'}
    )
    fetcher = make_fetcher(clock, cache)
    fetcher.fetch(URL)
    cache.put_page(replace(cache.get_page(URL), extractor=EXTRACTOR_VERSION - 1, text="old"))

    doc = fetcher.fetch(URL)  # fresh, but read differently back then: no cache, no 304
    assert "If-None-Match" not in responses.calls[1].request.headers
    assert (doc.extractor, doc.from_cache) == (EXTRACTOR_VERSION, False)
    assert "32 GB" in doc.text


class Browser:
    """A renderer that returns prepared HTML (or fails), and remembers what it was asked."""

    def __init__(self, html: str = "", error: str | None = None) -> None:
        self.html, self.error, self.urls = html, error, []

    def render(self, url: str) -> str:
        self.urls.append(url)
        if self.error:
            raise RenderError(self.error)
        return self.html

    def close(self) -> None:
        pass


APP_SHELL = b'<html><head><title>Shop</title></head><body><div id="root"></div></body></html>'


@responses.activate
def test_pages_built_by_scripts_are_rendered_when_a_browser_is_set(clock):
    responses.add(responses.GET, URL, body=APP_SHELL, content_type="text/html")
    assert make_fetcher(clock).fetch(URL).status is FetchStatus.EMPTY

    browser = Browser(ARTICLE.decode())
    doc = Fetcher(FetchConfig(), renderer=browser, clock=clock).fetch(URL)
    assert (doc.status, browser.urls) == (FetchStatus.OK, [URL])
    assert "32 GB of GDDR7 memory" in doc.text

    broken = Browser(error="Timeout 24000ms exceeded")
    failed = Fetcher(FetchConfig(), renderer=broken, clock=clock).fetch(URL)
    assert failed.status is FetchStatus.UNSUPPORTED  # our browser failed, not the site
    assert failed.error == "no readable main text, and rendering failed: Timeout 24000ms exceeded"


@responses.activate
def test_readable_pages_never_start_the_browser(clock):
    responses.add(responses.GET, URL, body=ARTICLE, content_type="text/html")
    browser = Browser()
    Fetcher(FetchConfig(), renderer=browser, clock=clock).fetch(URL)
    assert browser.urls == []


def test_render_setting():
    assert make_renderer("off", timeout=5) is None
    assert make_renderer("", timeout=5) is None
    with pytest.raises(ConfigError, match="SCOUT_RENDER"):
        make_renderer("chrome", timeout=5)


@pytest.mark.skipif(importlib.util.find_spec("playwright") is not None, reason="installed")
def test_rendering_without_playwright_says_how_to_get_it():
    with pytest.raises(ConfigError, match=r"scout\[render\]"):
        make_renderer("playwright", timeout=5)


@responses.activate
def test_sessions_are_reused_across_batches(clock):
    urls = [f"https://site.example/{n}" for n in range(4)]
    for url in urls:
        responses.add(responses.GET, url, body=ARTICLE, content_type="text/html")
    fetcher = make_fetcher(clock)
    for _ in range(5):
        fetcher.fetch_many(urls, workers=4)
    assert len(fetcher._sessions) <= 4
    fetcher.close()


def test_fetch_many_honours_its_deadline(clock, monkeypatch):
    fetcher = make_fetcher(clock)

    def fake_fetch(url: str) -> Document:
        if "slow" in url:
            time.sleep(1.5)
        return Document(url=url, status=FetchStatus.OK, fetched_at=clock.now, text="x")

    monkeypatch.setattr(fetcher, "fetch", fake_fetch)
    started = time.monotonic()
    docs = fetcher.fetch_many(
        ["https://a.example", "https://slow.example", "https://b.example"], deadline=0.3
    )
    assert time.monotonic() - started < 1.0
    assert [d.status for d in docs] == [FetchStatus.OK, FetchStatus.TIMEOUT, FetchStatus.OK]


@responses.activate
def test_document_round_trips_through_dict(clock):
    responses.add(
        responses.GET,
        URL,
        body=ARTICLE.replace(
            b"</head>",
            b'<script type="application/ld+json">{"@type":"Product","name":"Card","offers":'
            b'{"@type":"Offer","price":"499","priceCurrency":"EUR"}}</script></head>',
        ),
        content_type="text/html",
    )
    doc = make_fetcher(clock).fetch(URL)
    assert Document.from_dict(doc.to_dict()) == doc


@responses.activate
@pytest.mark.parametrize("location", ["http://10.0.0.5/admin", "//10.0.0.5/admin"])
def test_a_public_only_fetcher_never_requests_a_private_host(location):
    public = "http://93.184.216.34/a"
    responses.add(responses.GET, public, status=302, headers={"Location": location})
    fetcher = Fetcher(FetchConfig(public_only=True))
    hop = fetcher.fetch(public)
    direct = fetcher.fetch("http://127.0.0.1:1234/v1/models")

    assert (hop.status, direct.status) == (FetchStatus.REFUSED, FetchStatus.REFUSED)
    assert hop.error == (
        "not read: it redirects to http://10.0.0.5/admin, which is on a private network (10.0.0.5)"
    )
    assert [call.request.url for call in responses.calls] == [public]


def test_the_socket_itself_must_lead_to_the_public_internet(monkeypatch):
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        fetcher = Fetcher(FetchConfig(public_only=True, retries=0))
        ambiguous = fetcher.fetch(f"http://127.0.0.1:{port}\\@unresolvable.invalid/x")
        # As if DNS had said "public" to the check and "127.0.0.1" to the connection.
        monkeypatch.setattr("scout.web.fetch.private_address", lambda url: None)
        rebound = fetcher.fetch(f"http://127.0.0.1:{port}/admin")
    finally:
        server.shutdown()
    assert ambiguous.error == "not read: its address is ambiguous"
    assert (rebound.status, rebound.error) == (
        FetchStatus.REFUSED,
        "not read: it leads to a private network (127.0.0.1)",
    )
    assert seen == []


@responses.activate
def test_a_public_only_fetcher_skips_a_cached_page_that_landed_privately():
    public = "http://93.184.216.34/a"
    responses.add(responses.GET, public, body=b"<p>" + b"public text " * 40 + b"</p>")
    with Store(":memory:") as store:
        store.put_page(
            Document(
                url=public,
                status=FetchStatus.OK,
                fetched_at=NOW,
                final_url="http://169.254.169.254/latest/meta-data/",
                text="secret",
            )
        )
        doc = Fetcher(FetchConfig(public_only=True), cache=store, clock=Clock()).fetch(public)
    assert (doc.from_cache, "secret" in doc.text) == (False, False)
